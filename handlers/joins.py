from __future__ import annotations

import asyncio
import time

from aiogram import Bot, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import JOIN_TRANSITION, LEAVE_TRANSITION, ChatMemberUpdatedFilter
from aiogram.types import ChatMemberUpdated, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup

import cas
import db
import heuristics
import logic
import verification
from config import config
from db import Session, get_or_create_settings
from state import PendingVerification, join_tracker, pending_verifications

router = Router(name="joins")


def _chat_type_label(tg_type: str) -> str:
    if tg_type in ("group", "supergroup"):
        return "group"
    if tg_type == "channel":
        return "channel"
    return "other"


async def _restrict(bot: Bot, chat_id: int, user_id: int) -> None:
    try:
        await bot.restrict_chat_member(
            chat_id, user_id, permissions=ChatPermissions(can_send_messages=False)
        )
    except TelegramBadRequest:
        pass  # например, уже админ — ограничить нельзя, и не нужно


async def _kick(bot: Bot, chat_id: int, user_id: int) -> None:
    try:
        await bot.ban_chat_member(chat_id, user_id)
        await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
    except TelegramBadRequest:
        pass


async def _timeout_kick(bot: Bot, chat_id: int, user_id: int, message_id: int | None, token: str) -> None:
    try:
        await asyncio.sleep(config.captcha_timeout_seconds)
    except asyncio.CancelledError:
        return  # прошёл капчу раньше — таймер отменили

    pending_verifications.pop((chat_id, user_id), None)
    verification.sessions.pop(token, None)
    await _kick(bot, chat_id, user_id)
    await db.record_member_leave(chat_id, user_id)
    if message_id:
        try:
            await bot.delete_message(chat_id, message_id)
        except TelegramBadRequest:
            pass


# ---------- бот сам добавлен/повышен/удалён — регистрируем чат сразу ----------

@router.my_chat_member()
async def on_bot_status_change(event: ChatMemberUpdated) -> None:
    chat_type = _chat_type_label(event.chat.type)
    await db.upsert_chat_info(event.chat.id, chat_type, event.chat.title or "", event.chat.username or "")


# ---------- обычный участник вступил ----------

@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION))
async def on_member_join(event: ChatMemberUpdated, bot: Bot) -> None:
    chat_id = event.chat.id
    user = event.new_chat_member.user
    chat_type = _chat_type_label(event.chat.type)

    await db.upsert_chat_info(chat_id, chat_type, event.chat.title or "", event.chat.username or "")

    if user.is_bot:
        return

    settings_now = await logic.get_settings_snapshot(chat_id)

    # Вход закрыт всем — кикаем не глядя на капчу
    if settings_now.join_blocked:
        await _kick(bot, chat_id, user.id)
        return

    # Доверенный пользователь — пускаем сразу, без капчи и проверок
    if await db.is_trusted(user.id):
        await db.record_member_join(chat_id, user.id, user.username or "", user.first_name or "", int(time.time()))
        if settings_now.notify_subs:
            await logic.notify_admins(bot, f"➕ {user.mention_html()} (доверенный) в «{logic.chat_display_name(settings_now)}»")
        return

    # Проверка по базе известных спам-ботов CAS — баним сразу, до капчи
    if await cas.is_banned(user.id):
        await _kick(bot, chat_id, user.id)
        await db.record_ban(chat_id, user.id, user.username or "", user.first_name or "", "CAS", "в базе CAS")
        await db.add_audit(chat_id, 0, "cas_ban", f"user={user.id}")
        await logic.notify_admins(
            bot, f"⛔️ {user.mention_html()} забанен в «{logic.chat_display_name(settings_now)}» — числится в базе CAS."
        )
        return

    await db.record_member_join(chat_id, user.id, user.username or "", user.first_name or "", int(time.time()))

    if settings_now.notify_subs:
        tag = f" (@{user.username})" if user.username else ""
        await logic.notify_admins(bot, f"➕ Новый участник в «{logic.chat_display_name(settings_now)}»: {user.mention_html()}{tag}")

    if chat_type != "group":
        return  # в канале капча уже прошла на этапе заявки/ссылки — тут больше нечего делать

    async with Session() as session:
        settings = await get_or_create_settings(session, chat_id)
        count_in_window = join_tracker.register_join(chat_id, config.raid_window_seconds)

        if (
            count_in_window >= config.raid_join_threshold
            and not settings.raid_active
            and not settings.captcha_manual
        ):
            settings.captcha_before_raid = settings.captcha_enabled
            settings.captcha_enabled = True
            settings.raid_active = True
            settings.raid_expires_at = int(time.time()) + config.raid_captcha_duration
            await session.commit()
            await logic.notify_admins(
                bot,
                f"⚠️ Похоже на накрутку в «{logic.chat_display_name(settings_now)}»: "
                f"{count_in_window} вступлений за {config.raid_window_seconds} сек.\n"
                f"Автоматически включил капчу на {config.raid_captcha_duration // 60} мин.",
            )

        captcha_needed = settings.captcha_enabled

    # Даже если капча выключена глобально — подозрительным (шаблонный юзернейм + нет фото)
    # всё равно покажем капчу выборочно
    if not captcha_needed and await heuristics.is_suspicious(bot, user):
        captcha_needed = True

    if not captcha_needed:
        return

    await _restrict(bot, chat_id, user.id)

    vsession = verification.create_session(
        user.id, chat_id, flow="group", chat_title=event.chat.title or ""
    )
    me = await bot.get_me()
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="🔓 Пройти проверку",
            url=f"https://t.me/{me.username}?start=verify_{vsession.token}",
        )
    ]])

    try:
        msg = await bot.send_message(
            chat_id,
            f"👋 {user.mention_html()}, чтобы писать в чат, пройди быструю проверку — "
            f"жми кнопку ниже (откроется в личке с ботом).\n"
            f"У тебя {config.captcha_timeout_seconds} сек., иначе — кик (можно зайти заново).",
            reply_markup=kb,
        )
        message_id = msg.message_id
    except TelegramBadRequest:
        message_id = None

    task = asyncio.create_task(_timeout_kick(bot, chat_id, user.id, message_id, vsession.token))
    pending_verifications[(chat_id, user.id)] = PendingVerification(
        chat_id=chat_id,
        user_id=user.id,
        captcha_message_id=message_id,
        task=task,
    )


# ---------- участник вышел/был удалён ----------

@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=LEAVE_TRANSITION))
async def on_member_leave(event: ChatMemberUpdated, bot: Bot) -> None:
    chat_id = event.chat.id
    user = event.old_chat_member.user
    if user.is_bot:
        return

    await db.record_member_leave(chat_id, user.id)

    settings = await logic.get_settings_snapshot(chat_id)
    if settings.notify_subs:
        tag = f"@{user.username}" if user.username else (user.first_name or str(user.id))
        await logic.notify_admins(bot, f"➖ {tag} покинул(а) «{logic.chat_display_name(settings)}».")
