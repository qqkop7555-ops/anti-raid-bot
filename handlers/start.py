from __future__ import annotations

from aiogram import Bot, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo

import cas
import dashboard
import db
import logic
import verification
from config import config

router = Router(name="start")

VERIFY_PAYLOAD_PREFIX = "verify_"   # групповая капча — токен уже создан при вступлении
JOIN_PAYLOAD_PREFIX = "join_"       # канальная капча — токен создаём на лету по chat_id


def _webapp_button(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="🔐 Пройти проверку",
            web_app=WebAppInfo(url=f"{config.webapp_url}/?token={token}"),
        )
    ]])


@router.message(Command("start"))
async def cmd_start(message: Message, bot: Bot, command: CommandObject) -> None:
    payload = (command.args or "").strip()

    if message.chat.type == ChatType.PRIVATE and payload.startswith(VERIFY_PAYLOAD_PREFIX):
        await _open_group_captcha(message, payload[len(VERIFY_PAYLOAD_PREFIX):])
        return

    if message.chat.type == ChatType.PRIVATE and payload.startswith(JOIN_PAYLOAD_PREFIX):
        await _open_channel_captcha(message, bot, payload[len(JOIN_PAYLOAD_PREFIX):])
        return

    if message.chat.type == ChatType.PRIVATE:
        if not logic.is_admin(message.from_user and message.from_user.id):
            await message.answer(f"Этот бот принадлежит {config.owner_contact}.")
            return
        text, kb = await dashboard.render_root()
        await message.answer(text, reply_markup=kb)
        return

    # /start прямо в группе/канале — сразу открываем карточку этого чата
    if not logic.is_admin(message.from_user and message.from_user.id):
        return
    result = await dashboard.render_chat(message.chat.id)
    if result is None:
        await message.answer("Ещё не видел ни одного события в этом чате — напиши /start ещё раз чуть позже.")
        return
    text, kb = result
    await message.answer(text, reply_markup=kb)


async def _open_group_captcha(message: Message, token: str) -> None:
    session = verification.get_session(token)
    if session is None:
        await message.answer(
            "⌛ Эта проверка уже устарела. Вернись в группу и, если тебя ещё не приняли, "
            "попробуй зайти в чат заново — бот пришлёт новую кнопку."
        )
        return
    await message.answer(
        f"Проверка для чата «{session.chat_title or session.chat_id}» готова, жми кнопку:",
        reply_markup=_webapp_button(token),
    )


async def _open_channel_captcha(message: Message, bot: Bot, chat_id_str: str) -> None:
    try:
        target_chat_id = int(chat_id_str)
    except ValueError:
        await message.answer("Ссылка повреждена, попроси у администратора канала новую.")
        return

    settings = await logic.get_settings_snapshot(target_chat_id)
    if settings.join_blocked:
        await message.answer("Вход в этот канал сейчас полностью закрыт администратором.")
        return

    chat_title = settings.title
    if not chat_title:
        try:
            chat = await bot.get_chat(target_chat_id)
            chat_title = chat.title or ""
        except TelegramBadRequest:
            pass

    user = message.from_user

    if await cas.is_banned(user.id):
        await db.record_ban(target_chat_id, user.id, user.username or "", user.first_name or "", "CAS", "в базе CAS")
        await db.add_audit(target_chat_id, 0, "cas_ban", f"user={user.id}")
        await logic.notify_admins(bot, f"⛔️ {user.mention_html()} — попытка входа в «{chat_title}», но он в базе CAS.")
        await message.answer("Не получилось выдать ссылку — обратись к администратору канала.")
        return

    if await db.is_trusted(user.id):
        try:
            link = await bot.create_chat_invite_link(target_chat_id, member_limit=1, name=f"trusted-{user.id}")
            await message.answer(f"Ты в доверенных — вот ссылка без капчи:\n{link.invite_link}")
        except TelegramBadRequest:
            await message.answer("У бота нет прав приглашать людей в этот канал.")
        return

    vsession = verification.create_session(
        user.id, target_chat_id, flow="channel_link", chat_title=chat_title,
        username=user.username or "", first_name=user.first_name or "",
    )
    await message.answer(
        f"Проверка для канала «{chat_title or target_chat_id}» готова. Реши пример, "
        f"и я выдам тебе персональную одноразовую ссылку на вступление:",
        reply_markup=_webapp_button(vsession.token),
    )
