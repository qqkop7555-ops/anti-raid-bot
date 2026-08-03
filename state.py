"""
Рантайм-состояние, которое не обязательно хранить в БД (переживать рестарт не нужно).
Сами вопросы капчи и токены веб-версии — в verification.py, здесь только служебные
задачи (кик по таймауту, авто-отклонение заявок) и счётчик вступлений для антирейда.
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass


@dataclass
class PendingVerification:
    """Ждём, пока участник группы пройдёт капчу в веб-версии; если не успеет — кикаем."""

    chat_id: int
    user_id: int
    captcha_message_id: int | None
    task: asyncio.Task | None = None


class JoinTracker:
    """Хранит временные метки вступлений по чатам для детекта рейда."""

    def __init__(self) -> None:
        self._joins: dict[int, list[float]] = defaultdict(list)

    def register_join(self, chat_id: int, window_seconds: int) -> int:
        """Добавляет вступление и возвращает кол-во вступлений в текущем окне."""
        now = time.time()
        bucket = self._joins[chat_id]
        bucket.append(now)
        cutoff = now - window_seconds
        while bucket and bucket[0] < cutoff:
            bucket.pop(0)
        return len(bucket)

    def count(self, chat_id: int, window_seconds: int) -> int:
        now = time.time()
        cutoff = now - window_seconds
        bucket = self._joins.get(chat_id, [])
        return len([t for t in bucket if t >= cutoff])


join_tracker = JoinTracker()

# (chat_id, user_id) -> PendingVerification — активные капчи новичков в группах
pending_verifications: dict[tuple[int, int], PendingVerification] = {}

# (chat_id, user_id) -> задача авто-отклонения зависшей заявки на вступление в канал
decline_tasks: dict[tuple[int, int], asyncio.Task] = {}

# (chat_id, user_id) — заявки, подтверждённые капчой до того, как approve успел отработать
verified_requests: set[tuple[int, int]] = set()

# user_id -> "group" | "channel" | "trusted" — владелец сейчас должен прислать @username/ID/пересланное
# сообщение, чтобы вручную подключить уже существующий чат или добавить доверенного (см. dashboard.py)
awaiting_registration: dict[int, str] = {}

# user_id -> chat_id — владелец сейчас должен прислать текст для поиска по участникам этого чата
awaiting_search: dict[int, int] = {}


@dataclass
class PendingMassBan:
    """Список ID распарсен из .txt, ждём /confirm_mass_ban или /cancel_mass_ban от владельца."""

    chat_id: int | str
    user_ids: list[int]
    invalid_lines: int


# user_id -> chat_id/@username — владелец прислал /mass_ban <чат>, ждём .txt файл со списком ID
awaiting_mass_ban_file: dict[int, int | str] = {}

# user_id -> PendingMassBan — файл распознан, ждём подтверждения (или отмены) перед реальным баном
pending_mass_ban: dict[int, PendingMassBan] = {}


@dataclass
class PendingBanRecent:
    """Ждём подтверждения /да /нет на бан всех, кто вступил за последние N часов."""

    chat_id: int
    since_ts: int
    hours: float
    user_ids: list[int]  # кого именно баним (зафиксировано на момент запроса, не пересчитывается)


# admin_user_id -> PendingBanRecent
pending_ban_recent: dict[int, PendingBanRecent] = {}
