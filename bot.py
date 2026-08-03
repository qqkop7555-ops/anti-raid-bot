import asyncio
import logging
import time
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from sqlalchemy import select

import logic
import verification
from config import config
from db import ChatSettings, Session, init_db
from handlers import admin, antiflood, ban_recent, gatekeeper, joins, mass_ban, requests, start
from dashboard import router as dashboard_router
from server import create_app

logging.basicConfig(level=logging.INFO)

WEBAPP_DIR = Path(__file__).parent / "webapp"
WEBHOOK_PATH = f"/webhook/{config.webhook_secret}"


async def raid_expiry_watcher(bot: Bot) -> None:
    """Раз в минуту: чистит протухшие капчи, снимает истёкший авто-рейд и напоминает о текущем."""
    while True:
        await asyncio.sleep(60)
        verification.cleanup_expired()

        now = int(time.time())
        async with Session() as session:
            result = await session.execute(
                select(ChatSettings).where(ChatSettings.raid_active.is_(True))
            )
            for settings in result.scalars():
                if settings.raid_expires_at and settings.raid_expires_at <= now:
                    settings.raid_active = False
                    settings.captcha_enabled = settings.captcha_before_raid
                    await logic.notify_admins(
                        bot,
                        f"Авто-режим рейда в «{logic.chat_display_name(settings)}» истёк, "
                        f"капча возвращена к прежнему состоянию.",
                    )
                else:
                    await logic.notify_admins(
                        bot,
                        f"⚠️ Рейд всё ещё активен в «{logic.chat_display_name(settings)}». "
                        f"Капча включена. Снять раньше — кнопка «🟢 Снять режим рейда» в карточке чата.",
                    )
                    settings.raid_last_reminder_at = now
            await session.commit()


async def main() -> None:
    await init_db()

    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(start.router)
    dp.include_router(dashboard_router)
    dp.include_router(admin.router)
    dp.include_router(joins.router)
    dp.include_router(requests.router)
    dp.include_router(antiflood.router)
    dp.include_router(gatekeeper.router)
    dp.include_router(mass_ban.router)
    dp.include_router(ban_recent.router)

    async def _on_startup(**_kwargs) -> None:
        asyncio.create_task(raid_expiry_watcher(bot))

    dp.startup.register(_on_startup)

    app = create_app(bot, WEBAPP_DIR)
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=config.webhook_secret).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.port)
    await site.start()
    logging.info("Server started on 0.0.0.0:%s (webapp + webhook at %s)", config.port, WEBHOOK_PATH)

    webhook_url = f"{config.webapp_url}{WEBHOOK_PATH}"
    await bot.set_webhook(
        url=webhook_url,
        secret_token=config.webhook_secret,
        drop_pending_updates=True,
        allowed_updates=[
            "message", "chat_member", "my_chat_member", "callback_query", "chat_join_request",
        ],
    )
    logging.info("Webhook set to %s", webhook_url)

    await asyncio.Event().wait()  # держим процесс живым — обновления теперь приходят через вебхук


if __name__ == "__main__":
    asyncio.run(main())
