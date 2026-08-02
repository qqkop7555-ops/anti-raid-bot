from __future__ import annotations

import logging

import aiohttp

from config import config

logger = logging.getLogger(__name__)


async def is_banned(user_id: int) -> bool:
    """True, если ID числится в базе известных спам-ботов CAS (cas.chat)."""
    if not config.cas_enabled:
        return False
    try:
        timeout = aiohttp.ClientTimeout(total=config.cas_timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(config.cas_api_url, params={"user_id": user_id}) as resp:
                if resp.status != 200:
                    return False
                data = await resp.json(content_type=None)
                return bool(data.get("ok"))
    except Exception:
        logger.warning("CAS check failed for user %s", user_id, exc_info=True)
        return False  # сеть недоступна/таймаут — не блокируем человека из-за нашей ошибки
