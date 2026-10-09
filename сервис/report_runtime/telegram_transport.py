"""One attempted send per call. Durable delivery decisions belong to Outbox."""
from __future__ import annotations

import json
import re
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError

from .outbox import Receipt, RejectedDelivery


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError("telegram_redirect_blocked")


class TelegramTransport:
    def __init__(self, token, chat_id, *, thread_id=None, destination="director", opener=None):
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token or ""):
            raise ValueError("Invalid Telegram configuration")
        if not re.fullmatch(r"-?[0-9]+", str(chat_id)):
            raise ValueError("Invalid Telegram destination")
        if thread_id is not None and (not str(thread_id).isdigit() or int(thread_id) <= 0):
            raise ValueError("Invalid Telegram thread")
        self._token, self.chat_id, self.thread_id = token, str(chat_id), thread_id
        self.destination = destination
        self.opener = opener or build_opener(NoRedirect())

    def send(self, destination, text, *, idempotency_key):
        if destination != self.destination:
            raise RejectedDelivery("Unknown destination")
        payload = {"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True}
        if self.thread_id is not None:
            payload["message_thread_id"] = int(self.thread_id)
        request = Request("https://api.telegram.org/bot" + self._token + "/sendMessage",
                          data=json.dumps(payload, ensure_ascii=False).encode(), method="POST",
                          headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=30) as response:
                raw = response.read(65537)
            if len(raw) > 65536:
                raise RuntimeError("Telegram receipt too large")
            body = json.loads(raw)
        except HTTPError as exc:
            if exc.code in (400, 401, 403, 404, 429):
                raise RejectedDelivery("Telegram rejected request") from None
            raise RuntimeError("Telegram delivery outcome unknown") from None
        except Exception:
            raise RuntimeError("Telegram delivery outcome unknown") from None
        if body.get("ok") is False:
            raise RejectedDelivery("Telegram rejected request")
        result = body.get("result")
        if (body.get("ok") is not True or not isinstance(result, dict)
                or type(result.get("message_id")) is not int or result["message_id"] <= 0
                or str((result.get("chat") or {}).get("id")) != self.chat_id
                or (self.thread_id is not None and result.get("message_thread_id") != int(self.thread_id))):
            raise RuntimeError("Telegram receipt is not confirmed")
        return Receipt(destination, result["message_id"])
