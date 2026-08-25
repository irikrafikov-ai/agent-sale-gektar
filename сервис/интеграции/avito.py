"""Клиент Avito API.

Заменяет MCP-коннектор, который на сервере недоступен: коннектор привязан
к аккаунту Claude, URL и токена для контейнера не существует.

Имена методов повторяют имена MCP-инструментов, на которые ссылается регламент
(avito_chats_list, avito_send_message и т.д.), чтобы регламент не пришлось
переписывать под другой словарь.
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

BASE = "https://api.avito.ru"


class AvitoError(RuntimeError):
    pass


class Avito:
    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._id = client_id or os.environ["AVITO_CLIENT_ID"]
        self._secret = client_secret or os.environ["AVITO_CLIENT_SECRET"]
        self._http = httpx.Client(timeout=timeout)
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._user_id: int | None = None

    # --- авторизация -----------------------------------------------------

    def _access_token(self) -> str:
        # Обновляем за минуту до истечения: запрос, стартовавший на границе,
        # иначе успевает получить 401 уже в полёте.
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token

        r = self._http.post(
            f"{BASE}/token/",
            data={
                "grant_type": "client_credentials",
                "client_id": self._id,
                "client_secret": self._secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if r.status_code != 200:
            raise AvitoError(f"не получен токен Avito: {r.status_code} {r.text[:200]}")

        payload = r.json()
        self._token = payload["access_token"]
        self._token_expires_at = time.time() + float(payload.get("expires_in", 3600))
        return self._token

    def _request(self, method: str, path: str, **kw: Any) -> Any:
        headers = kw.pop("headers", {}) | {"Authorization": f"Bearer {self._access_token()}"}
        r = self._http.request(method, f"{BASE}{path}", headers=headers, **kw)

        if r.status_code == 401:
            # Токен мог протухнуть раньше срока — один повтор с новым.
            self._token = None
            headers["Authorization"] = f"Bearer {self._access_token()}"
            r = self._http.request(method, f"{BASE}{path}", headers=headers, **kw)

        if r.status_code >= 400:
            raise AvitoError(f"{method} {path} → {r.status_code}: {r.text[:300]}")
        return r.json()

    # --- аккаунт ---------------------------------------------------------

    def whoami(self) -> dict:
        return self._request("GET", "/core/v1/accounts/self")

    @property
    def user_id(self) -> int:
        if self._user_id is None:
            self._user_id = int(self.whoami()["id"])
        return self._user_id

    # --- чаты ------------------------------------------------------------

    def chats_list(
        self,
        limit: int = 100,
        offset: int = 0,
        unread_only: bool = False,
        item_ids: list[int] | None = None,
    ) -> list[dict]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if unread_only:
            params["unread_only"] = "true"
        if item_ids:
            params["item_ids"] = ",".join(str(i) for i in item_ids)
        data = self._request(
            "GET", f"/messenger/v2/accounts/{self.user_id}/chats", params=params
        )
        return data.get("chats", [])

    def chat_messages(self, chat_id: str, limit: int = 50, offset: int = 0) -> list[dict]:
        data = self._request(
            "GET",
            f"/messenger/v3/accounts/{self.user_id}/chats/{chat_id}/messages/",
            params={"limit": limit, "offset": offset},
        )
        return data.get("messages", data if isinstance(data, list) else [])

    def send_message(self, chat_id: str, text: str) -> dict:
        """ЗАПИСЬ. Отправляет сообщение реальному клиенту.

        Подтверждения здесь нет намеренно: на сервере подтверждать некому.
        Защита — стоп-краны регламента, они проверяются до вызова.
        """
        if not text.strip():
            raise AvitoError("пустой текст сообщения")
        return self._request(
            "POST",
            f"/messenger/v1/accounts/{self.user_id}/chats/{chat_id}/messages",
            json={"message": {"text": text}, "type": "text"},
        )

    def mark_read(self, chat_id: str) -> None:
        self._request("POST", f"/messenger/v1/accounts/{self.user_id}/chats/{chat_id}/read")

    # --- объявления ------------------------------------------------------

    def items_list(self, page: int = 1, per_page: int = 25, status: str = "active") -> list[dict]:
        data = self._request(
            "GET",
            "/core/v1/items",
            params={"page": page, "per_page": per_page, "status": status},
        )
        return data.get("resources", [])

    def item_info(self, item_id: int) -> dict:
        return self._request("GET", f"/core/v1/accounts/{self.user_id}/items/{item_id}/")

    # --- звонки (коллтрекинг) --------------------------------------------

    def calls(self, date_from: str, date_to: str, limit: int = 100, offset: int = 0) -> list[dict]:
        """Метаданные звонков Авито: телефон покупателя, время, длительность.

        Записей разговоров API v1 не отдаёт (проверено 25.08.2026: методов
        get­CallRecord* нет, в ответе getCalls нет ссылок) — только факты
        звонков. Даты — ISO, например «2026-08-25T00:00:00Z».
        """
        data = self._request(
            "POST",
            "/calltracking/v1/getCalls/",
            json={
                "dateTimeFrom": date_from,
                "dateTimeTo": date_to,
                "limit": limit,
                "offset": offset,
            },
        )
        return (data.get("data") or {}).get("calls", [])

    def close(self) -> None:
        self._http.close()
