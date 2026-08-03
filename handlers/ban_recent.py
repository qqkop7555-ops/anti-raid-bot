"""
/ban_recent <время> — забанить всех, кто вступил в ЭТОТ чат за последние N часов.

Работает по собственной базе бота (таблица watched_members, которая пополняется
в реальном времени через chat_member-события) — Bot API не даёт напрямую
запросить у Telegram список участников с датами вступления, поэтому нужен
именно такой, локальный учёт.

Поток:
  /ban_recent 14h   -> бот считает, сколько человек попадает под условие,
                        показывает кнопки Да/Нет
  [Да]              -> реально банит, с отчётом по ходу
  [Нет]             -> отмена, никого не трогаем
"""
from __future__ import annotations

import asyncio
import re
import time

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import db
import logic
import state

router = Router(name="ban_recent")

BAN_DELAY_SECONDS = 0.05
PROGRESS_EVERY = 50

_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([а-яa-zА-ЯA-Z]*)$")

_UNIT_TO_HOURS = {
    "": 1.0, "h": 1.0, "ч": 1.0, "hour": 1.0, "hours": 1.0, "час": 1.0, "часа": 1.0, "часов": 1.0,
    "m": 1 / 60, "м": 1 / 60, "min": 1 / 60, "minute": 1 / 60, "minutes": 1 / 60, "мин": 1 / 60,
    "d": 24.0, "д": 24.0, "day": 24.0, "days": 24.0, "дн": 24.0, "день": 24.0, "дня": 24.0, "дней": 24.0,
}


def parse_duration_hours(raw: str) -> float | None:
    """'14h' / '14ч' / '14' / '2d' / '90m' -> часы (float). None, если не распознано."""
    raw = raw.strip().lower().replace(" ", "")
    m = _DURATION_RE.match(raw)
    if not m:
        return None
    value = float(m.group(1))
    unit = m.group(2)
    if unit not in _UNIT_TO_HOURS:
        return None
    return value * _UNIT_TO_HOURS[unit]


def _confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Да", callback_data="ban_recent:yes"),
        InlineKeyboardButton(text="Нет", callback_data="ban_recent:no"),
    ]])


@router.message(Command("ban_recent"))
async def cmd_ban_recent(message: Message, command: CommandObject) -> None:
    if not await logic.require_admin(message):
        return

    arg = (command.args or "").strip()
    hours = parse_duration_hours(arg) if arg else None
    if hours is None:
        await message.reply(
            "Используй: /ban_recent <время>\n"
            "Например: /ban_recent 14h  или  /ban_recent 30m  или  /ban_recent 2d"
        )
        return

    chat_id = message.chat.id
    since_ts = int(time.time()) - int(hours * 3600)

    members = await db.list_members_since(chat_id, since_ts)
    candidate_ids = [m.user_id for m in members if not logic.is_admin(m.user_id)]

    if not candidate_ids:
        await message.reply(f"За последние {arg} никто не вступал (по данным бота) — банить некого.")
        return

    state.pending_ban_recent[message.from_user.id] = state.PendingBanRecent(
        chat_id=chat_id, since_ts=since_ts, hours=hours, user_ids=candidate_ids,
    )

    await message.reply(
        f"Будет забанено {len(candidate_ids)} человек, вступивших за последние {arg}.\n"
        f"Учтены только те, кого бот сам видел через события вступления — если бот подключили "
        f"недавно, более старые участники в эту статистику не попадут.",
        reply_markup=_confirm_keyboard(),
    )


@router.callback_query(F.data == "ban_recent:no")
async def cb_ban_recent_no(call: CallbackQuery) -> None:
    if not logic.is_admin(call.from_user.id):
        await call.answer("Только для владельца.", show_alert=True)
        return
    state.pending_ban_recent.pop(call.from_user.id, None)
    await call.answer("Отменено.")
    await call.message.edit_text(call.message.html_text + "\n\n❌ Отменено, никого не забанил.", reply_markup=None)


@router.callback_query(F.data == "ban_recent:yes")
async def cb_ban_recent_yes(call: CallbackQuery, bot: Bot) -> None:
    if not logic.is_admin(call.from_user.id):
        await call.answer("Только для владельца.", show_alert=True)
        return

    pending = state.pending_ban_recent.pop(call.from_user.id, None)
    if pending is None:
        await call.answer("Запрос устарел, начни заново через /ban_recent.", show_alert=True)
        return

    await call.answer("Начинаю.")
    await call.message.edit_text(call.message.html_text, reply_markup=None)

    total = len(pending.user_ids)
    progress_msg = await call.message.answer(f"Баню {total} человек… 0/{total}")

    banned = 0
    failed = 0

    for i, user_id in enumerate(pending.user_ids, start=1):
        member = await db.get_member(pending.chat_id, user_id)
        while True:
            try:
                await bot.ban_chat_member(pending.chat_id, user_id)
                banned += 1
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
                continue
            except TelegramAPIError:
                failed += 1
                break
            except Exception:
                failed += 1
                break

        await db.record_member_leave(pending.chat_id, user_id)
        await db.record_ban(
            pending.chat_id, user_id,
            member.username if member else "", member.first_name if member else "",
            str(call.from_user.id), f"вступил за последние {pending.hours:g}ч (массовый бан)",
        )

        if i % PROGRESS_EVERY == 0 or i == total:
            try:
                await progress_msg.edit_text(f"Баню… {i}/{total} (забанено: {banned}, ошибок: {failed})")
            except Exception:
                pass

        await asyncio.sleep(BAN_DELAY_SECONDS)

    await db.add_audit(pending.chat_id, call.from_user.id, "ban_recent", f"hours={pending.hours:g} total={total}")
    await call.message.answer(f"Готово: забанено {banned} из {total}, ошибок {failed}.")
