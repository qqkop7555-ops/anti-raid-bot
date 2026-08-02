"""
Массовый бан по списку ID из .txt файла — для чистки накрученных/раздовых
подписчиков в канале или группе.

Поток (всё в личке с владельцем, только он же в config.admin_ids):
  1. /mass_ban <@канал или chat_id>   — говорим боту, где чистим
  2. присылаем .txt файл, по одному ID на строке
  3. бот показывает, сколько ID нашёл, и просит /confirm_mass_ban или /cancel_mass_ban
  4. /confirm_mass_ban — реально банит, с отчётом по ходу и в конце
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramRetryAfter, TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

import logic
import state

router = Router(name="mass_ban")
logger = logging.getLogger("mass_ban")

# Пауза между банами — с запасом от лимита Telegram (~30 запросов/сек)
BAN_DELAY_SECONDS = 0.05
# Как часто обновлять сообщение с прогрессом (каждые N обработанных ID)
PROGRESS_EVERY = 50


def _parse_target(raw: str) -> int | str:
    """'@channel' или 'channel' -> '@channel'; число (в т.ч. отрицательное) -> int chat_id."""
    raw = raw.strip()
    if raw.lstrip("-").isdigit():
        return int(raw)
    if raw.startswith("@"):
        return raw
    return f"@{raw}"


def _parse_id_list(text: str) -> tuple[list[int], int]:
    """Возвращает (уникальные ID, кол-во нераспознанных строк)."""
    seen: set[int] = set()
    ids: list[int] = []
    invalid = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # на случай "123456789 - имя" или "123456789, @username" — берём первое число из строки
        token = line.split()[0].split(",")[0].strip()
        try:
            uid = int(token)
        except ValueError:
            invalid += 1
            continue
        if uid not in seen:
            seen.add(uid)
            ids.append(uid)
    return ids, invalid


@router.message(F.chat.type == ChatType.PRIVATE, Command("mass_ban"))
async def cmd_mass_ban(message: Message, command: CommandObject) -> None:
    if not await logic.require_admin(message):
        return

    arg = (command.args or "").strip()
    if not arg:
        await message.reply(
            "Используй: /mass_ban <@канал или chat_id>\n"
            "Например: /mass_ban @infoaboutqq  или  /mass_ban -1001234567890"
        )
        return

    target = _parse_target(arg)
    state.awaiting_mass_ban_file[message.from_user.id] = target
    state.pending_mass_ban.pop(message.from_user.id, None)  # сбрасываем незавершённый предыдущий запрос, если был

    await message.reply(
        f"Ок, чистим: <b>{target}</b>\n"
        f"Пришли .txt файл со списком ID — по одному на строке."
    )


def _is_awaiting_mass_ban_file(message: Message) -> bool:
    return (
        message.from_user is not None
        and message.from_user.id in state.awaiting_mass_ban_file
        and message.document is not None
    )


@router.message(F.chat.type == ChatType.PRIVATE, _is_awaiting_mass_ban_file)
async def handle_mass_ban_file(message: Message, bot: Bot) -> None:
    user_id = message.from_user.id
    target = state.awaiting_mass_ban_file.pop(user_id)

    try:
        file_info = await bot.get_file(message.document.file_id)
        file_bytes = await bot.download_file(file_info.file_path)
        text = file_bytes.read().decode("utf-8", errors="ignore")
    except Exception as e:
        await message.reply(f"Не смог прочитать файл: {e}")
        return

    ids, invalid = _parse_id_list(text)

    if not ids:
        await message.reply("В файле не нашлось ни одного числового ID. Проверь файл и пришли /mass_ban заново.")
        return

    state.pending_mass_ban[user_id] = state.PendingMassBan(chat_id=target, user_ids=ids, invalid_lines=invalid)

    warn = f"\n⚠️ {invalid} строк не распознано и пропущено." if invalid else ""
    await message.reply(
        f"Нашёл <b>{len(ids)}</b> уникальных ID для чата <b>{target}</b>.{warn}\n\n"
        f"Это забанит всех насовсем (не просто кикнет) — отменить придётся вручную по одному, если что.\n\n"
        f"Точно баним? Пришли /confirm_mass_ban для подтверждения или /cancel_mass_ban для отмены."
    )


@router.message(F.chat.type == ChatType.PRIVATE, Command("cancel_mass_ban"))
async def cmd_cancel_mass_ban(message: Message) -> None:
    if not await logic.require_admin(message):
        return
    user_id = message.from_user.id
    had_pending = state.pending_mass_ban.pop(user_id, None) is not None
    state.awaiting_mass_ban_file.pop(user_id, None)
    await message.reply("Отменено, ничего не забанено." if had_pending else "Нечего отменять — не было активного запроса.")


@router.message(F.chat.type == ChatType.PRIVATE, Command("confirm_mass_ban"))
async def cmd_confirm_mass_ban(message: Message, bot: Bot) -> None:
    if not await logic.require_admin(message):
        return

    user_id = message.from_user.id
    pending = state.pending_mass_ban.pop(user_id, None)
    if pending is None:
        await message.reply("Нет активного запроса на массовый бан. Начни с /mass_ban <чат>.")
        return

    total = len(pending.user_ids)
    progress_msg = await message.reply(f"Начинаю бан {total} ID в {pending.chat_id}… 0/{total}")

    banned = 0
    failed = 0
    failed_examples: list[str] = []

    for i, uid in enumerate(pending.user_ids, start=1):
        while True:
            try:
                await bot.ban_chat_member(chat_id=pending.chat_id, user_id=uid)
                banned += 1
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
                continue
            except TelegramAPIError as e:
                failed += 1
                if len(failed_examples) < 5:
                    failed_examples.append(f"{uid}: {e.__class__.__name__}")
                break
            except Exception as e:
                failed += 1
                if len(failed_examples) < 5:
                    failed_examples.append(f"{uid}: {e.__class__.__name__}")
                break

        if i % PROGRESS_EVERY == 0 or i == total:
            try:
                await progress_msg.edit_text(
                    f"Баню {pending.chat_id}… {i}/{total} (забанено: {banned}, ошибок: {failed})"
                )
            except Exception:
                pass  # сообщение могли удалить руками — не критично, просто не обновляем

        await asyncio.sleep(BAN_DELAY_SECONDS)

    summary = f"Готово: забанено {banned} из {total}, ошибок {failed}."
    if failed_examples:
        summary += "\nПримеры ошибок:\n" + "\n".join(failed_examples)
        if failed > len(failed_examples):
            summary += f"\n… и ещё {failed - len(failed_examples)}"
    await message.reply(summary)
    logger.info("mass_ban завершён: chat=%s total=%s banned=%s failed=%s", pending.chat_id, total, banned, failed)
