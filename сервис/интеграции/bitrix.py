"""Клиент Битрикс24 через входящий вебхук.

Заменяет MCP-коннектор. Вебхук уже несёт в себе авторизацию, отдельного
токена не нужно — но по той же причине его URL является секретом целиком.

Имена методов повторяют имена MCP-инструментов из регламента.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

CATEGORY_ID = 0  # воронка «Прямые покупатели»


class BitrixError(RuntimeError):
    pass


class Bitrix:
    def __init__(self, webhook: str | None = None, timeout: float = 30.0) -> None:
        raw = webhook or os.environ["BITRIX_WEBHOOK"]
        self._base = raw.rstrip("/")
        self._http = httpx.Client(timeout=timeout)

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Произвольный метод REST. Запасной путь, когда готового нет."""
        r = self._http.post(f"{self._base}/{method}.json", json=params or {})
        if r.status_code >= 400:
            raise BitrixError(f"{method} → {r.status_code}: {r.text[:300]}")
        data = r.json()
        if "error" in data:
            raise BitrixError(f"{method} → {data['error']}: {data.get('error_description', '')}")
        return data.get("result")

    # --- сделки ----------------------------------------------------------

    def crm_list(
        self,
        entity: str = "deal",
        filter: dict[str, Any] | None = None,
        select: list[str] | None = None,
        order: dict[str, str] | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Список сущностей. Страницы тянутся автоматически до limit.

        Битрикс отдаёт по 50 записей за раз и игнорирует любой другой размер
        страницы — поэтому limit режется здесь, а не передаётся в API.
        """
        out: list[dict] = []
        start = 0
        while len(out) < limit:
            params: dict[str, Any] = {"start": start}
            if filter:
                params["filter"] = filter
            if select:
                params["select"] = select
            if order:
                params["order"] = order
            batch = self.call(f"crm.{entity}.list", params) or []
            if not batch:
                break
            out.extend(batch)
            if len(batch) < 50:
                break
            start += 50
        return out[:limit]

    def crm_get(self, entity: str, id: int | str) -> dict:
        return self.call(f"crm.{entity}.get", {"id": id})

    def crm_add(self, entity: str, fields: dict[str, Any]) -> int:
        return self.call(f"crm.{entity}.add", {"fields": fields})

    def crm_update(self, entity: str, id: int | str, fields: dict[str, Any]) -> bool:
        return self.call(f"crm.{entity}.update", {"id": id, "fields": fields})

    def crm_stages(self, category_id: int = CATEGORY_ID) -> list[dict]:
        return self.call("crm.dealcategory.stage.list", {"id": category_id})

    def crm_fields(self, entity: str = "deal") -> dict:
        return self.call(f"crm.{entity}.fields")

    # --- таймлайн --------------------------------------------------------

    def timeline_comment_add(self, deal_id: int | str, comment: str) -> int:
        return self.call(
            "crm.timeline.comment.add",
            {"fields": {"ENTITY_ID": deal_id, "ENTITY_TYPE": "deal", "COMMENT": comment}},
        )

    # --- задачи ----------------------------------------------------------

    def task_add(
        self,
        title: str,
        responsible_id: int = 1,
        description: str = "",
        deadline: str | None = None,
        priority: str = "1",
    ) -> dict:
        fields: dict[str, Any] = {
            "TITLE": title,
            "RESPONSIBLE_ID": responsible_id,
            "DESCRIPTION": description,
            "PRIORITY": priority,
        }
        if deadline:
            fields["DEADLINE"] = deadline
        return self.call("tasks.task.add", {"fields": fields})

    def task_list(self, filter: dict[str, Any] | None = None, limit: int = 50) -> list[dict]:
        result = self.call("tasks.task.list", {"filter": filter or {}, "start": 0})
        tasks = (result or {}).get("tasks", []) if isinstance(result, dict) else []
        return tasks[:limit]

    def user_list(self) -> list[dict]:
        return self.call("user.get", {"FILTER": {"ACTIVE": True}}) or []

    def close(self) -> None:
        self._http.close()
