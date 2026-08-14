"""Инструменты агента — обёртка интеграций в MCP-сервер внутри процесса.

Имена инструментов совпадают с теми, на которые ссылается регламент, поэтому
`регламент/*.md` работает на сервере дословно, без переписывания под другой словарь.

Сложные аргументы (filter, fields, select) принимаются JSON-строкой: схема
инструментов описывает простые типы, и строка — единственный способ передать
вложенную структуру, не гадая о формате схемы.
"""

from __future__ import annotations

import json
import os
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from интеграции.avito import Avito
from интеграции.bitrix import Bitrix
from интеграции.telegram import Telegram

avito = Avito()
bitrix = Bitrix()
telegram = Telegram()

# read-only | send. В режиме read-only отправка клиенту блокируется на уровне
# инструмента, а не промпта: инструкцию модель может интерпретировать, отказ
# инструмента — нет.
MODE = os.environ.get("AGENT_MODE", "send")

_sent: list[dict] = []  # факты отправки за прогон, для сверки в отчёте


def _ok(payload: Any) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, default=str)}]}


def _err(message: str) -> dict:
    return {"content": [{"type": "text", "text": f"ОШИБКА: {message}"}], "is_error": True}


def _json_arg(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"аргумент не разобран как JSON: {e}") from e


# --- Авито ---------------------------------------------------------------


@tool("avito_chats_list", "Список чатов аккаунта Авито", {"limit": int, "offset": int})
async def avito_chats_list(args: dict) -> dict:
    try:
        chats = avito.chats_list(limit=args.get("limit", 100), offset=args.get("offset", 0))
    except Exception as e:
        return _err(str(e))

    # Отдаём срез, а не сырой ответ: в полном виде 100 чатов не помещаются
    # в контекст, и агент теряет обзор именно там, где он нужен.
    me = avito.user_id
    out = []
    for c in chats:
        ctx = (c.get("context", {}).get("value") or {})
        last = c.get("last_message") or {}
        other = next((u for u in c.get("users", []) if u.get("id") != me), {})
        out.append(
            {
                "chat_id": c.get("id"),
                "name": other.get("name"),
                "user_id": other.get("id"),
                "location": (ctx.get("location") or {}).get("title"),
                "item_id": ctx.get("id"),
                "item_title": ctx.get("title"),
                "price": ctx.get("price_string"),
                "last_from": "клиент" if last.get("author_id") != me else "наше",
                "last_at": last.get("created"),
                "last_text": ((last.get("content") or {}).get("text") or last.get("type", ""))[:120],
            }
        )
    return _ok(out)


@tool("avito_chat_messages", "История сообщений чата Авито", {"chat_id": str, "limit": int})
async def avito_chat_messages(args: dict) -> dict:
    try:
        msgs = avito.chat_messages(args["chat_id"], limit=args.get("limit", 30))
    except Exception as e:
        return _err(str(e))

    me = avito.user_id
    return _ok(
        [
            {
                "from": "клиент" if m.get("author_id") != me else "наше",
                "at": m.get("created"),
                "type": m.get("type"),
                "text": (m.get("content") or {}).get("text", ""),
            }
            for m in msgs
        ]
    )


@tool(
    "avito_send_message",
    "ЗАПИСЬ. Отправляет сообщение клиенту в чат Авито. Перед вызовом проверь стоп-краны AGENT.md §2 и сверься с Битриксом: отказникам не пишем.",
    {"chat_id": str, "text": str},
)
async def avito_send_message(args: dict) -> dict:
    if MODE != "send":
        return _err(f"режим {MODE}: отправка клиентам отключена, сообщение НЕ ушло")
    try:
        result = avito.send_message(args["chat_id"], args["text"])
    except Exception as e:
        return _err(str(e))
    _sent.append({"chat_id": args["chat_id"], "text": args["text"]})
    return _ok({"отправлено": True, "id": result.get("id")})


@tool("avito_item_info", "Карточка объявления Авито: площадь, цена, адрес", {"item_id": int})
async def avito_item_info(args: dict) -> dict:
    try:
        return _ok(avito.item_info(args["item_id"]))
    except Exception as e:
        return _err(str(e))


# --- Битрикс -------------------------------------------------------------


@tool(
    "b24_crm_list",
    "Список сделок/контактов. filter и select — JSON-строки, например {\"CATEGORY_ID\":0,\"STAGE_ID\":\"NEW\"}",
    {"entity": str, "filter": str, "select": str, "limit": int},
)
async def b24_crm_list(args: dict) -> dict:
    try:
        return _ok(
            bitrix.crm_list(
                entity=args.get("entity", "deal"),
                filter=_json_arg(args.get("filter"), {}),
                select=_json_arg(args.get("select"), None),
                limit=args.get("limit", 50),
            )
        )
    except Exception as e:
        return _err(str(e))


@tool("b24_crm_get", "Одна сущность CRM по ID", {"entity": str, "id": str})
async def b24_crm_get(args: dict) -> dict:
    try:
        return _ok(bitrix.crm_get(args.get("entity", "deal"), args["id"]))
    except Exception as e:
        return _err(str(e))


@tool("b24_crm_add", "Создать сущность CRM. fields — JSON-строка", {"entity": str, "fields": str})
async def b24_crm_add(args: dict) -> dict:
    try:
        return _ok({"id": bitrix.crm_add(args.get("entity", "deal"), _json_arg(args["fields"], {}))})
    except Exception as e:
        return _err(str(e))


@tool(
    "b24_crm_update",
    "Обновить сущность CRM. fields — JSON-строка, например {\"STAGE_ID\":\"PREPARATION\"}",
    {"entity": str, "id": str, "fields": str},
)
async def b24_crm_update(args: dict) -> dict:
    try:
        return _ok({"обновлено": bitrix.crm_update(args.get("entity", "deal"), args["id"], _json_arg(args["fields"], {}))})
    except Exception as e:
        return _err(str(e))


@tool("b24_timeline_comment", "Комментарий в таймлайн сделки", {"deal_id": str, "comment": str})
async def b24_timeline_comment(args: dict) -> dict:
    try:
        return _ok({"id": bitrix.timeline_comment_add(args["deal_id"], args["comment"])})
    except Exception as e:
        return _err(str(e))


@tool(
    "b24_task_add",
    "Задача в Битриксе. Ответственный по умолчанию — Ирик (ID 1)",
    {"title": str, "description": str, "deadline": str, "responsible_id": int, "priority": str},
)
async def b24_task_add(args: dict) -> dict:
    try:
        return _ok(
            bitrix.task_add(
                title=args["title"],
                description=args.get("description", ""),
                deadline=args.get("deadline") or None,
                responsible_id=args.get("responsible_id", 1),
                priority=args.get("priority", "1"),
            )
        )
    except Exception as e:
        return _err(str(e))


@tool("b24_task_list", "Незакрытые задачи. filter — JSON-строка", {"filter": str, "limit": int})
async def b24_task_list(args: dict) -> dict:
    try:
        return _ok(bitrix.task_list(_json_arg(args.get("filter"), {"!STATUS": 5}), args.get("limit", 50)))
    except Exception as e:
        return _err(str(e))


@tool("b24_call", "Произвольный метод REST Битрикса. params — JSON-строка", {"method": str, "params": str})
async def b24_call(args: dict) -> dict:
    try:
        return _ok(bitrix.call(args["method"], _json_arg(args.get("params"), {})))
    except Exception as e:
        return _err(str(e))


# --- Телеграм ------------------------------------------------------------


@tool(
    "telegram_alert",
    "Срочное сообщение Ирику: сработал стоп-кран, конфликт, горячий клиент. Не для отчёта — отчёт уходит сам в конце прогона.",
    {"text": str},
)
async def telegram_alert(args: dict) -> dict:
    try:
        telegram.send(args["text"], alert=True)
    except Exception as e:
        return _err(str(e))
    return _ok({"отправлено": True})


def отправленные() -> list[dict]:
    """Факты отправки за прогон — для сверки отчёта с данными (код O01)."""
    return list(_sent)


сервер = create_sdk_mcp_server(
    name="gektar",
    version="1.0.0",
    tools=[
        avito_chats_list,
        avito_chat_messages,
        avito_send_message,
        avito_item_info,
        b24_crm_list,
        b24_crm_get,
        b24_crm_add,
        b24_crm_update,
        b24_timeline_comment,
        b24_task_add,
        b24_task_list,
        b24_call,
        telegram_alert,
    ],
)

ИМЕНА = [
    "mcp__gektar__avito_chats_list",
    "mcp__gektar__avito_chat_messages",
    "mcp__gektar__avito_send_message",
    "mcp__gektar__avito_item_info",
    "mcp__gektar__b24_crm_list",
    "mcp__gektar__b24_crm_get",
    "mcp__gektar__b24_crm_add",
    "mcp__gektar__b24_crm_update",
    "mcp__gektar__b24_timeline_comment",
    "mcp__gektar__b24_task_add",
    "mcp__gektar__b24_task_list",
    "mcp__gektar__b24_call",
    "mcp__gektar__telegram_alert",
]
