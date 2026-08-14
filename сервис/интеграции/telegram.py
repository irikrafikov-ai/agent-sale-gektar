"""Отправка отчётов и алертов в Телеграм.

На сервере это единственный канал к Ирику: показывать тексты в чате некому,
поэтому отчёт по прогону и срабатывания стоп-кранов уходят сюда.
"""

from __future__ import annotations

import os

import httpx

LIMIT = 4096  # предел одного сообщения в Telegram


class TelegramError(RuntimeError):
    pass


class Telegram:
    def __init__(
        self,
        token: str | None = None,
        chat_id: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._token = token or os.environ["TELEGRAM_BOT_TOKEN"]
        self._chat_id = chat_id or os.environ["TELEGRAM_CHAT_ID"]
        self._http = httpx.Client(timeout=timeout)

    def send(self, text: str, alert: bool = False) -> None:
        for chunk in _split(text.strip()):
            body = chunk if not alert else f"🚨 {chunk}"
            r = self._http.post(
                f"https://api.telegram.org/bot{self._token}/sendMessage",
                json={
                    "chat_id": self._chat_id,
                    "text": body,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                },
            )
            if r.status_code >= 400:
                # Markdown ломается на незакрытых `*` и `_` в текстах клиентов —
                # повторяем без разметки, чтобы отчёт дошёл в любом случае.
                r = self._http.post(
                    f"https://api.telegram.org/bot{self._token}/sendMessage",
                    json={"chat_id": self._chat_id, "text": body},
                )
                if r.status_code >= 400:
                    raise TelegramError(f"{r.status_code}: {r.text[:200]}")

    def close(self) -> None:
        self._http.close()


def _split(text: str) -> list[str]:
    """Режет по строкам, не по символам — иначе таблицы отчёта рвутся посреди строки."""
    if len(text) <= LIMIT:
        return [text]

    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > LIMIT:
            if current:
                parts.append(current)
            # Одна строка длиннее лимита — режем её жёстко, вариантов нет.
            while len(line) > LIMIT:
                parts.append(line[:LIMIT])
                line = line[LIMIT:]
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        parts.append(current)
    return parts
