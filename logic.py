from __future__ import annotations

from config import config
from db import Session, get_or_create_settings
from state import join_tracker


def is_admin(user_id: int | None) -> bool:
    return user_id is not None and user_id in config.admin_ids


async def require_admin(message) -> bool:
    """Проверяет права и, если их нет, объясняет причину (вместо тихого игнора).
    Возвращает True, если можно продолжать выполнение команды."""
    user_id = message.from_user and message.from_user.id
    if is_admin(user_id):
        return True

    if not config.admin_ids:
        await message.reply(
            "⛔ Команда только для админа, но переменная ADMIN_IDS в Railway сейчас пустая — "
            "поэтому админом не считается вообще никто.\n"
            f"Твой Telegram ID: <code>{user_id}</code>\n"
            "Добавь его в ADMIN_IDS (Settings → Variables на Railway) и сделай Redeploy."
        )
    else:
        await message.reply(
            f"⛔ Эта команда только для админа. Твой Telegram ID: <code>{user_id}</code>\n"
            "Если это должен быть ты — проверь, что этот ID указан в ADMIN_IDS на Railway "
            "(без лишних пробелов), и сделай Redeploy."
        )
    return False


async def set_captcha(chat_id: int, enabled: bool) -> None:
    async with Session() as session:
        settings = await get_or_create_settings(session, chat_id)
        settings.captcha_enabled = enabled
        settings.captcha_manual = True
        settings.raid_active = False
        await session.commit()


async def set_captcha_auto(chat_id: int) -> None:
    async with Session() as session:
        settings = await get_or_create_settings(session, chat_id)
        settings.captcha_manual = False
        await session.commit()


async def turn_off_raid(chat_id: int) -> bool:
    """Возвращает True, если режим рейда был активен и его сняли."""
    async with Session() as session:
        settings = await get_or_create_settings(session, chat_id)
        if not settings.raid_active:
            return False
        settings.raid_active = False
        settings.captcha_enabled = settings.captcha_before_raid
        await session.commit()
        return True


async def set_join_blocked(chat_id: int, blocked: bool) -> None:
    async with Session() as session:
        settings = await get_or_create_settings(session, chat_id)
        settings.join_blocked = blocked
        await session.commit()


async def set_notify_subs(chat_id: int, enabled: bool) -> None:
    async with Session() as session:
        settings = await get_or_create_settings(session, chat_id)
        settings.notify_subs = enabled
        await session.commit()


async def set_antiflood(chat_id: int, enabled: bool) -> None:
    async with Session() as session:
        settings = await get_or_create_settings(session, chat_id)
        settings.antiflood_enabled = enabled
        await session.commit()


def status_emoji(chat_type: str, settings) -> str:
    if settings.join_blocked:
        return "⛔️"
    if settings.raid_active:
        return "⚠️"
    if chat_type == "group":
        return "✅" if settings.captcha_enabled else "❌"
    return "✅"


def chat_display_name(settings) -> str:
    if settings.username:
        return f"@{settings.username}"
    return settings.title or str(settings.chat_id)


async def notify_admins(bot, text: str) -> None:
    for admin_id in config.admin_ids:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            pass


async def status_text(chat_id: int) -> str:
    async with Session() as session:
        settings = await get_or_create_settings(session, chat_id)

    count = join_tracker.count(chat_id, config.raid_window_seconds)
    return "\n".join([
        f"Капча: {'включена' if settings.captcha_enabled else 'выключена'}"
        f" ({'вручную' if settings.captcha_manual else 'авто'})",
        f"Режим рейда: {'АКТИВЕН 🔴' if settings.raid_active else 'спокойно 🟢'}",
        f"Вступлений за последние {config.raid_window_seconds} сек: {count}"
        f" (порог тревоги: {config.raid_join_threshold})",
    ])


async def get_settings_snapshot(chat_id: int):
    async with Session() as session:
        return await get_or_create_settings(session, chat_id)
