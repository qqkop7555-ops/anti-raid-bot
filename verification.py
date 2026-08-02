from __future__ import annotations

import hashlib
import hmac
import random
import secrets
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl

from config import config

# flow: "group" (снять ограничение в группе), "channel_link" (выдать одноразовую ссылку),
# "channel_request" (одобрить уже поданную заявку на вступление)
FlowType = str


@dataclass
class VerificationSession:
    token: str
    user_id: int
    chat_id: int
    flow: FlowType
    question: str
    answer: str
    options: list[str]
    chat_title: str = ""
    username: str = ""
    first_name: str = ""
    expires_at: float = 0.0
    used: bool = False


sessions: dict[str, VerificationSession] = {}


def _build_question() -> tuple[str, str, list[str]]:
    a, b = random.randint(2, 15), random.randint(2, 15)
    op = random.choice(["+", "−"])
    if op == "−" and b > a:
        a, b = b, a
    answer = a + b if op == "+" else a - b

    options = {answer}
    while len(options) < 4:
        options.add(answer + random.randint(-6, 6))
    options_list = [str(o) for o in options]
    random.shuffle(options_list)

    return f"{a} {op} {b}", str(answer), options_list


def create_session(
    user_id: int,
    chat_id: int,
    flow: FlowType,
    chat_title: str = "",
    username: str = "",
    first_name: str = "",
) -> VerificationSession:
    question, answer, options = _build_question()
    token = secrets.token_urlsafe(16)
    session = VerificationSession(
        token=token,
        user_id=user_id,
        chat_id=chat_id,
        flow=flow,
        question=question,
        answer=answer,
        options=options,
        chat_title=chat_title,
        username=username,
        first_name=first_name,
        expires_at=time.time() + config.captcha_timeout_seconds,
    )
    sessions[token] = session
    return session


def get_session(token: str) -> VerificationSession | None:
    session = sessions.get(token)
    if session is None:
        return None
    if session.used or session.expires_at < time.time():
        sessions.pop(token, None)
        return None
    return session


def pop_session(token: str) -> VerificationSession | None:
    session = get_session(token)
    if session:
        sessions.pop(token, None)
    return session


def cleanup_expired() -> None:
    now = time.time()
    for token in [t for t, s in sessions.items() if s.expires_at < now]:
        sessions.pop(token, None)


def validate_init_data(init_data: str) -> dict | None:
    """
    Проверяет подпись Telegram WebApp initData (см. https://core.telegram.org/bots/webapps#validating-data-received-via-the-web-app).
    Возвращает распарсенные поля, если подпись верна, иначе None.
    """
    if not init_data:
        return None
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", config.bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        return None

    # опционально: не принимать слишком старые initData (Telegram обновляет auth_date при каждом открытии)
    auth_date = pairs.get("auth_date")
    if auth_date and time.time() - int(auth_date) > 3600:
        return None

    return pairs
