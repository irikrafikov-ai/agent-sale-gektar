"""Umnico — канал MAX «Ева ГектарЪ» для агента продаж.

Зачем агенту MAX. Правило Ирика 21.09.2026: пообещал клиенту «пришлю в MAX
живые фото и видео» — сам нашёл контакт в MAX и отправил, а не пишешь дальше
в Авито, будто ничего не обещал. 21.09 Ольга дала номер в 12:12, агент
пообещал «сегодня пришлю», и материалы в 14:10 отправил Ирик вручную.

Что умеет: найти диалог по номеру (в любом канале Umnico), написать в него;
если диалога нет — «написать первым» в MAX по номеру. Номер нормализуется
здесь же: клиенты пишут «9207938710», «8 920 793-87-10», «+7 (920)…» —
в Umnico всё это один и тот же 79207938710.

Ключ — UMNICO_TOKEN (тот же, что у менеджера по партнёрам; заголовок строго
`Authorization: Bearer …`, с большой буквы — со строчной 401).
"""

from __future__ import annotations

import os
import re
from typing import Any

import httpx

БАЗА = "https://api.umnico.com"
SA_MAX = int(os.environ.get("UMNICO_SA_MAX", "119876"))          # канал MAX «Ева ГектарЪ»
USER_ID = int(os.environ.get("UMNICO_USER_ID", "2511489"))        # владелец «Гектар»
НОМЕР_КАНАЛА = "+7 995 169-12-30"                                 # что называть клиенту


class UmnicoError(RuntimeError):
    pass


def нормализовать_номер(сырой: str) -> str | None:
    """«9207938710», «8 (920) 793-87-10», «+7 920…» → «79207938710»; иначе None.

    Правило Ирика 21.09.2026: не хватает «7» — добавляй сам, а не отказывайся.
    """
    цифры = re.sub(r"\D", "", сырой or "")
    if len(цифры) == 10 and цифры[0] == "9":
        return "7" + цифры
    if len(цифры) == 11 and цифры[0] in "78" and цифры[1] == "9":
        return "7" + цифры[1:]
    return None


class Umnico:
    def __init__(self, token: str | None = None, timeout: float = 45.0) -> None:
        self._http = httpx.Client(
            base_url=БАЗА,
            headers={
                "Authorization": "Bearer " + (token or os.environ["UMNICO_TOKEN"]),
                "User-Agent": "curl/8.7.1",
                "Accept": "application/json",
            },
            timeout=timeout,
        )

    def _ok(self, r: httpx.Response, что: str) -> Any:
        if r.status_code >= 400:
            raise UmnicoError(f"{что} → {r.status_code}: {r.text[:300]}")
        try:
            return r.json()
        except ValueError:
            return r.text

    # --- поиск диалога -----------------------------------------------------

    def лиды(self, sa: int | None = None, предел: int = 2000) -> list[dict]:
        out: list[dict] = []
        for off in range(0, предел, 200):
            params: dict[str, Any] = {"limit": 200, "offset": off}
            if sa:
                params["sa"] = sa
            d = self._ok(self._http.get("/v1.3/leads/all", params=params), "leads/all")
            часть = d if isinstance(d, list) else (d.get("data") or [])
            out += часть
            if len(часть) < 200:
                break
        return out

    def лид_по_номеру(self, номер: str) -> dict | None:
        """Диалог с этим номером в любом канале (MAX, Telegram, WhatsApp).

        У диалогов MAX, начатых «первым», Umnico телефон клиента не отдаёт
        (customer.phone = null — проверено 21.09 на Ольге, лид 67918694).
        Тогда пишем первым повторно: MAX сам кладёт сообщение в тот же чат.
        """
        for л in self.лиды():
            L = л.get("lead") or л
            тел = re.sub(r"\D", "", str((L.get("customer") or {}).get("phone") or ""))
            if тел and нормализовать_номер(тел) == номер:
                return L
        return None

    def источник(self, lead_id: int) -> int | None:
        s = self._ok(self._http.get(f"/v1.3/messaging/{lead_id}/sources"), "sources")
        return (s[0].get("realId") or s[0].get("id")) if s else None

    # --- отправка ------------------------------------------------------------

    def написать_в_диалог(self, lead_id: int, текст: str) -> dict:
        real = self.источник(lead_id)
        if not real:
            raise UmnicoError(f"у диалога {lead_id} нет источника")
        return self._ok(
            self._http.post(
                f"/v1.3/messaging/{lead_id}/send",
                json={"message": {"text": текст}, "source": str(real), "userId": USER_ID},
            ),
            "send",
        )

    def написать_первым(self, номер: str, текст: str, sa: int = SA_MAX) -> dict:
        return self._ok(
            self._http.post(
                "/v1.3/messaging/post",
                json={"message": {"text": текст}, "destination": номер, "saId": sa},
            ),
            "messaging/post",
        )

    def отправить(self, сырой_номер: str, текст: str) -> dict:
        """Одна точка входа: нормализовать номер → найти диалог → написать;
        диалога нет → написать первым в MAX."""
        номер = нормализовать_номер(сырой_номер)
        if not номер:
            raise UmnicoError(f"не похоже на российский мобильный: {сырой_номер!r}")
        лид = self.лид_по_номеру(номер)
        if лид and лид.get("id"):
            ответ = self.написать_в_диалог(int(лид["id"]), текст)
            return {"номер": номер, "как": "в существующий диалог", "лид": лид["id"], "ответ": ответ}
        ответ = self.написать_первым(номер, текст)
        return {"номер": номер, "как": "первым в MAX", "ответ": ответ}

    def close(self) -> None:
        self._http.close()
