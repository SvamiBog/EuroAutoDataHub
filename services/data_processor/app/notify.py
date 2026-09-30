"""Отправка алертов и отчётов: Telegram, если заданы TELEGRAM_BOT_TOKEN и чат, иначе только лог."""
import logging
from typing import Awaitable, Callable, Optional

import httpx

logger = logging.getLogger(__name__)

# Лимит длины сообщения Telegram
MAX_MESSAGE_LENGTH = 4096


async def send_telegram(token: str, chat_id: str, text: str) -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                     json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True})
        response.raise_for_status()


def truncate(text: str, limit: int = MAX_MESSAGE_LENGTH) -> str:
    return text if len(text) <= limit else text[:limit - 2] + " …"


class Notifier:
    """send() возвращает статус доставки: sent, disabled (канал не настроен) или error: ..."""

    def __init__(self, token: Optional[str] = None, chat_id: Optional[str] = None,
                 sender: Optional[Callable[[str, str, str], Awaitable[None]]] = None):
        self.token = token
        self.chat_id = chat_id
        self.sender = sender or send_telegram

    @classmethod
    def from_settings(cls, config) -> "Notifier":
        return cls(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID)

    async def send(self, text: str, chat_id: Optional[str] = None) -> str:
        chat_id = chat_id or self.chat_id
        if not (self.token and chat_id):
            logger.info("Алерт (Telegram не настроен):\n" + text)
            return "disabled"
        try:
            await self.sender(self.token, chat_id, truncate(text))
            return "sent"
        except httpx.HTTPError as exc:
            logger.error(f"Не удалось отправить сообщение в Telegram: {exc}")
            return f"error: {exc}"
