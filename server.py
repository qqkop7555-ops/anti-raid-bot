from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import ChatPermissions
from aiohttp import web

import db
import logic
import verification
from state import decline_tasks, pending_verifications, verified_requests

logger = logging.getLogger(__name__)
routes = web.RouteTableDef()

FULL_PERMISSIONS = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
)


@routes.get("/")
async def index(request: web.Request) -> web.Response:
    return web.FileResponse(request.app["webapp_dir"] / "index.html")


@routes.get("/api/challenge")
async def get_challenge(request: web.Request) -> web.Response:
    token = request.query.get("token", "")
    session = verification.get_session(token)
    if session is None:
        return web.json_response({"error": "expired"}, status=404)
    return web.json_response({
        "question": session.question,
        "options": session.options,
        "chat_title": session.chat_title,
    })


@routes.post("/api/verify")
async def post_verify(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "message": "Битый запрос"}, status=400)

    token = str(body.get("token", ""))
    answer = str(body.get("answer", "")).strip()
    init_data = str(body.get("init_data", ""))

    session = verification.get_session(token)
    if session is None:
        return web.json_response(
            {"ok": False, "message": "Проверка устарела, вернись в бота и запроси заново."},
            status=400,
        )

    parsed = verification.validate_init_data(init_data)
    if parsed is None:
        return web.json_response(
            {"ok": False, "message": "Не удалось подтвердить личность через Telegram."},
            status=400,
        )

    try:
        tg_user = json.loads(parsed.get("user", "{}"))
    except ValueError:
        tg_user = {}

    if tg_user.get("id") != session.user_id:
        return web.json_response(
            {"ok": False, "message": "Эта проверка предназначена другому пользователю."},
            status=403,
        )

    if answer != session.answer:
        return web.json_response({"ok": False, "reason": "wrong_answer"}, status=400)

    verification.pop_session(token)
    bot: Bot = request.app["bot"]

    if session.flow == "group":
        return await _resolve_group(bot, session)
    if session.flow == "channel_link":
        return await _resolve_channel_link(bot, session)
    if session.flow == "channel_request":
        return await _resolve_channel_request(bot, session)

    return web.json_response({"ok": False, "message": "Неизвестный сценарий проверки."}, status=400)


async def _resolve_group(bot: Bot, session: verification.VerificationSession) -> web.Response:
    settings = await logic.get_settings_snapshot(session.chat_id)
    key = (session.chat_id, session.user_id)

    if settings.join_blocked:
        pending = pending_verifications.pop(key, None)
        if pending and pending.task:
            pending.task.cancel()
        try:
            await bot.ban_chat_member(session.chat_id, session.user_id)
            await bot.unban_chat_member(session.chat_id, session.user_id, only_if_banned=True)
        except TelegramBadRequest:
            pass
        await db.record_member_leave(session.chat_id, session.user_id)
        return web.json_response(
            {"ok": False, "message": "Вход в этот чат сейчас полностью закрыт администратором."},
            status=403,
        )

    try:
        await bot.restrict_chat_member(session.chat_id, session.user_id, permissions=FULL_PERMISSIONS)
    except TelegramBadRequest:
        pass

    pending = pending_verifications.pop(key, None)
    if pending:
        if pending.task:
            pending.task.cancel()
        if pending.captcha_message_id:
            try:
                await bot.edit_message_text(
                    "✅ Проверка пройдена, добро пожаловать в чат!",
                    chat_id=session.chat_id,
                    message_id=pending.captcha_message_id,
                )
            except TelegramBadRequest:
                pass

    return web.json_response({"ok": True, "message": "Готово, теперь можно писать в чат!"})


async def _resolve_channel_link(bot: Bot, session: verification.VerificationSession) -> web.Response:
    settings = await logic.get_settings_snapshot(session.chat_id)
    if settings.join_blocked:
        return web.json_response(
            {"ok": False, "message": "Вход в этот канал сейчас полностью закрыт администратором."},
            status=403,
        )
    try:
        link = await bot.create_chat_invite_link(
            session.chat_id, member_limit=1, name=f"verified-{session.user_id}"
        )
    except TelegramBadRequest:
        return web.json_response(
            {"ok": False, "message": "У бота нет прав приглашать людей в этот канал."},
            status=500,
        )
    return web.json_response({
        "ok": True,
        "message": "Проверка пройдена! Держи персональную ссылку:",
        "invite_link": link.invite_link,
    })


async def _resolve_channel_request(bot: Bot, session: verification.VerificationSession) -> web.Response:
    key = (session.chat_id, session.user_id)
    settings = await logic.get_settings_snapshot(session.chat_id)

    if settings.join_blocked:
        task = decline_tasks.pop(key, None)
        if task:
            task.cancel()
        try:
            await bot.decline_chat_join_request(session.chat_id, session.user_id)
        except TelegramBadRequest:
            pass
        return web.json_response(
            {"ok": False, "message": "Вход в этот канал сейчас полностью закрыт администратором."},
            status=403,
        )

    task = decline_tasks.pop(key, None)
    if task:
        task.cancel()
    try:
        await bot.approve_chat_join_request(session.chat_id, session.user_id)
        await db.record_member_join(
            session.chat_id, session.user_id, session.username, session.first_name, int(time.time())
        )
    except TelegramBadRequest:
        verified_requests.add(key)
    return web.json_response({"ok": True, "message": "Заявка одобрена, добро пожаловать!"})


def create_app(bot: Bot, webapp_dir: Path) -> web.Application:
    app = web.Application()
    app["bot"] = bot
    app["webapp_dir"] = webapp_dir
    app.add_routes(routes)
    app.router.add_static("/static/", path=webapp_dir, show_index=False)
    return app
