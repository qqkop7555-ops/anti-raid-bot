import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _int_env(name: str, default: int) -> int:
    val = os.getenv(name)
    return int(val) if val else default


@dataclass
class Config:
    bot_token: str = os.getenv("BOT_TOKEN", "")
    admin_ids: set[int] = field(
        default_factory=lambda: {
            int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x
        }
    )
    db_path: str = os.getenv("DB_PATH", "bot.db")

    raid_join_threshold: int = _int_env("RAID_JOIN_THRESHOLD", 5)
    raid_window_seconds: int = _int_env("RAID_WINDOW_SECONDS", 30)
    raid_captcha_duration: int = _int_env("RAID_CAPTCHA_DURATION", 900)

    captcha_timeout_seconds: int = _int_env("CAPTCHA_TIMEOUT_SECONDS", 90)

    # Через сколько секунд авто-отклонять заявку на вступление, если капча так и не пройдена
    # (защита от того, чтобы заявки от накрутки просто копились без дела)
    request_decline_seconds: int = _int_env("REQUEST_DECLINE_SECONDS", 3600)

    # Публичный HTTPS-адрес, на котором крутится веб-версия капчи (Mini App).
    # На Railway это домен сервиса, например https://your-app.up.railway.app
    webapp_url: str = os.getenv("WEBAPP_URL", "").rstrip("/")

    # Порт, на котором поднимаем aiohttp-сервер с мини-аппом (Railway сам подставит $PORT)
    port: int = _int_env("PORT", 8080)

    # Бот закрытый — им управляет только владелец (первый/единственный ID в ADMIN_IDS).
    # Всем остальным в личке вместо меню показываем эту подпись.
    owner_contact: str = os.getenv("OWNER_CONTACT", "@infoaboutqq")

    # Проверка по базе известных спам-ботов CAS (Combot Anti-Spam System, cas.chat).
    # При вступлении сверяем ID, и если он там — баним сразу, до капчи.
    cas_enabled: bool = os.getenv("CAS_ENABLED", "true").lower() not in ("0", "false", "no")
    cas_api_url: str = os.getenv("CAS_API_URL", "https://api.cas.chat/check")
    cas_timeout_seconds: int = _int_env("CAS_TIMEOUT_SECONDS", 5)

    # Эвристики подозрительности: если капча выключена для чата, но новый участник похож
    # на бота (нет фото профиля + шаблонный юзернейм из букв и цифр), капчу всё равно покажем.
    heuristics_enabled: bool = os.getenv("HEURISTICS_ENABLED", "true").lower() not in ("0", "false", "no")

    # Антифлуд по сообщениям в группах
    flood_window_seconds: int = _int_env("FLOOD_WINDOW_SECONDS", 10)
    flood_message_threshold: int = _int_env("FLOOD_MESSAGE_THRESHOLD", 6)
    flood_repeat_threshold: int = _int_env("FLOOD_REPEAT_THRESHOLD", 3)
    flood_mute_seconds: int = _int_env("FLOOD_MUTE_SECONDS", 300)

    # Секрет для пути вебхука (часть URL, чтобы не принимать чужие POST-запросы).
    # Если пусто — сгенерируется случайный при старте (тогда после рестарта поменяется,
    # это не проблема, просто бот сам себе установит новый вебхук).
    webhook_secret: str = os.getenv("WEBHOOK_SECRET", "")


config = Config()

if not config.bot_token:
    raise RuntimeError("BOT_TOKEN не задан. Заполни .env (см. .env.example)")

if not config.webapp_url:
    raise RuntimeError(
        "WEBAPP_URL не задан. Веб-капче и вебхуку нужен публичный HTTPS-адрес — "
        "включи публичный домен в Railway и укажи его в .env (см. .env.example)"
    )

if not config.webhook_secret:
    import secrets as _secrets
    config.webhook_secret = _secrets.token_urlsafe(24)
