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
import time
from collections import deque
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from интеграции.avito import Avito
from интеграции.bitrix import Bitrix
from интеграции.telegram import Telegram

# Клиенты создаются лениво, при первом обращении.
#
# Раньше они создавались прямо здесь, при импорте модуля — и отсутствие одной
# переменной роняло процесс сырым KeyError ещё до старта прогона: без понятного
# сообщения и без алерта в Телеграм. Проверка переменных теперь в прогон.py,
# а сюда клиент приходит уже тогда, когда он действительно нужен.

_клиенты: dict[str, object] = {}


def avito() -> Avito:
    if "avito" not in _клиенты:
        _клиенты["avito"] = Avito()
    return _клиенты["avito"]  # type: ignore[return-value]


def avito2() -> Avito:
    """Второй кабинет Авито («Светлая долина»).

    Отдельная пара ключей AVITO2_* — у Авито токен выдаётся на пару
    client_id/secret, одним клиентом два кабинета не обслужить.
    Если переменные не заданы, кабинет считается не подключённым: инструменты
    второго кабинета не регистрируются, и агент его в упор не видит —
    это штатный режим до момента, когда Ирик заведёт ключи в Variables.
    """
    if "avito2" not in _клиенты:
        _клиенты["avito2"] = Avito(
            client_id=os.environ["AVITO2_CLIENT_ID"],
            client_secret=os.environ["AVITO2_CLIENT_SECRET"],
        )
    return _клиенты["avito2"]  # type: ignore[return-value]


def avito2_подключён() -> bool:
    return bool(os.environ.get("AVITO2_CLIENT_ID") and os.environ.get("AVITO2_CLIENT_SECRET"))


def bitrix() -> Bitrix:
    if "bitrix" not in _клиенты:
        _клиенты["bitrix"] = Bitrix()
    return _клиенты["bitrix"]  # type: ignore[return-value]


def telegram() -> Telegram:
    if "telegram" not in _клиенты:
        _клиенты["telegram"] = Telegram()
    return _клиенты["telegram"]  # type: ignore[return-value]

# read-only | send. В режиме read-only отправка клиенту блокируется на уровне
# инструмента, а не промпта: инструкцию модель может интерпретировать, отказ
# инструмента — нет.
MODE = os.environ.get("AGENT_MODE", "send")

_sent: list[dict] = []  # факты отправки за прогон, для сверки в отчёте

# Стоп-кран темпа из рабочего цикла, шаг 4: не больше 30 уникальных сообщений
# в час. До 16.08 правило жило только в тексте регламента и в тот день было
# нарушено — 29 сообщений за семь минут. Теперь его держит код.
ПРЕДЕЛ_В_ЧАС = int(os.environ.get("ПРЕДЕЛ_В_ЧАС", "30"))
_отправки: deque[float] = deque(maxlen=500)

# Стоп-кран «Ирик пишет параллельно». Между тем, как агент прочитал чат, и тем,
# как он отправил ответ, проходит полторы минуты — и это ровно то окно, в
# которое Ирик отвечает клиенту сам с телефона. 21.08 так и вышло: агент писал
# Т.Ю. про воскресенье, пока Ирик отвечал ей же, и одно сообщение пришлось
# удалять вручную. Проверять чат при пробуждении недостаточно — проверяем в
# момент отправки.
_наши_id: set[str] = set()          # id сообщений, отправленных этим процессом
ОКНО_ЧУЖОГО = int(os.environ.get("ОКНО_ЧУЖОГО", "1800"))  # сек


# Стоп-кран «тёплый лид без единого слова клиента».
#
# 21.08 в 23:43 Ирику ушло уведомление о горящем лиде по Вячеславу, который за
# четыре месяца не написал НИ ОДНОГО сообщения: все восемь реплик в чате наши,
# плюс два системных от Авито. Агент принял системное «Покупателя
# заинтересовало ваше предложение» за действие человека.
#
# Регламент это уже запрещал, но правило, которое проверяет только сам агент,
# рано или поздно нарушается. А цена здесь высокая и незаметная: обесцененный
# канал уведомлений не виден никак, и на нём теряется настоящий горячий лид.
# Стоп-кран «отказ без причины».
#
# 24.08 замер по базе: из 23 настоящих отказов 14 закрыты как «не интересно /
# передумал» — 61%. Это не причина, а пустая графа: за ней может быть локация,
# деньги, семья, сроки. Пока она есть, мы не знаем, почему у нас не покупают,
# и любая правка скриптов остаётся угадыванием.
#
# Показательно рядом: «дорого» — один отказ из 23. Весь скрипт построен вокруг
# цен и рассрочки, а цена почти никого не останавливает.
#
# Поэтому «не интересно» теперь требует дословной цитаты клиента. Не потому,
# что цитата что-то доказывает, а потому что её негде взять, если человек
# ничего не сказал, — и тогда честный ответ другой: «причина не выяснена».
ОТКАЗ_НЕ_ИНТЕРЕСНО = "59"
ОТКАЗ_НЕ_ВЫЯСНЕНА = "95"
МИН_ЦИТАТА = 10


def _отказ_без_причины(поля: dict, цитата: str | None) -> str | None:
    if str(поля.get("UF_CRM_FAIL_REASON") or "") != ОТКАЗ_НЕ_ИНТЕРЕСНО:
        return None

    цитата = (цитата or "").strip()
    if len(цитата) >= МИН_ЦИТАТА:
        return None

    return (
        "СТОП-КРАН: «Не интересно / передумал» без слов клиента не ставится. "
        "Сделка НЕ обновлена.\n\n"
        "Это самая частая причина в базе (61% отказов) и самая бесполезная: "
        "по ней нельзя понять, что менять в работе.\n\n"
        "Выбери одно из двух:\n"
        "1. Клиент ОБЪЯСНИЛ, почему отказывается — повтори вызов и передай его "
        "слова дословно в поле причина_словами_клиента. Если объяснение "
        "укладывается в готовую причину (локация 63, дорого 57, купил в другом "
        "месте 61) — ставь её, она точнее.\n"
        f"2. Клиент НИЧЕГО не объяснил — ставь причину {ОТКАЗ_НЕ_ВЫЯСНЕНА} "
        "«Причина не выяснена (молчание)». Это честно и отделяет «сказал нет» от "
        "«мы не узнали».\n\n"
        "И прежде чем закрывать — спроси прямо, одним сообщением: «скажите "
        "честно, что не подошло: место, цена или сроки?». Приём работает: так "
        "Юлия назвала Грецовку и 15 соток, а Наталья — Москву. Обе до этого "
        "выглядели как «не интересно»."
    )


СЛОВА_ЛИДА = ("тёплый лид", "теплый лид", "горячий лид", "горящий лид", "горящий клиент", "горячий клиент")


def _похоже_на_лид(текст: str) -> bool:
    т = текст.lower()
    return any(с in т for с in СЛОВА_ЛИДА)


def _лид_без_клиента(args: dict) -> str | None:
    """Пустая строка ошибки, если уведомление о лиде слать нельзя."""
    текст = args.get("text") or ""
    if not _похоже_на_лид(текст):
        return None  # обычный алерт: стоп-кран не вмешивается

    chat_id = (args.get("chat_id") or "").strip()
    if not chat_id:
        return (
            "СТОП-КРАН: это уведомление о лиде, но не передан chat_id. "
            "Передай chat_id и account — перед отправкой проверяется, что клиент "
            "вообще писал в этот чат."
        )

    try:
        cli = _avito(args)
        msgs = cli.chat_messages(chat_id, limit=100)
    except Exception:
        return None  # сеть подвела — не глушим уведомление, лид дороже проверки

    me = cli.user_id
    # Автор 1 — это Авито: «ознакомился», «заинтересовало», «создал чат»,
    # «посмотрел номер». Активность площадки, а не человека.
    свои_слова = [m for m in msgs if m.get("author_id") not in (me, 1)]
    if свои_слова:
        return None

    return (
        f"СТОП-КРАН: в чате {chat_id} клиент не написал НИ ОДНОГО сообщения — "
        "все реплики наши или системные от Авито. Уведомление НЕ отправлено.\n\n"
        "Системные сигналы Авито («ознакомился со спецпредложением», «покупателя "
        "заинтересовало», «создал чат», «посмотрел номер») — это активность "
        "площадки, а не действие человека. Тёплым лидом они не делают.\n\n"
        "Тёплый лид — это то, что человек СКАЗАЛ САМ: назвал участок, спросил про "
        "бронь или документы, согласился на встречу, дал телефон. Ответь клиенту "
        "и отметь событие в отчёте, а Ирика не дёргай: ложное уведомление вреднее "
        "пропущенного — пропущенный виден в отчёте, обесцененный канал не виден никак."
    )


def _чужое_исходящее(cli, chat_id: str) -> dict | None:
    """Исходящее сообщение, которого этот процесс не отправлял.

    Ищем не «последнее наше вообще», а такое, которое новее последнего слова
    клиента и написано только что. Старое касание трёхдневной давности тоже
    новее входящего — но оно не признак параллельной переписки, и блокировать
    из-за него вечерний прогрев нельзя.
    """
    try:
        msgs = cli.chat_messages(chat_id, limit=10)
    except Exception:
        return None  # сеть подвела — не мешаем отправке, это не наша забота

    me = cli.user_id
    последнее_входящее = max(
        (m.get("created", 0) for m in msgs if m.get("author_id") != me), default=0
    )
    порог = time.time() - ОКНО_ЧУЖОГО

    чужие = [
        m for m in msgs
        if m.get("author_id") == me
        and str(m.get("id")) not in _наши_id
        and m.get("created", 0) > последнее_входящее
        and m.get("created", 0) > порог
    ]
    return max(чужие, key=lambda m: m.get("created", 0)) if чужие else None


def _темп_позволяет() -> bool:
    порог = time.time() - 3600
    while _отправки and _отправки[0] < порог:
        _отправки.popleft()
    return len(_отправки) < ПРЕДЕЛ_В_ЧАС


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

# Кабинета два: "gektar" (Щёкинские берега, по умолчанию) и "dolina"
# (Светлая долина). Инструменты общие, кабинет выбирается аргументом account —
# так регламент и наработанные привычки агента работают на оба без правок.


def _avito(args: dict) -> Avito:
    account = args.get("account") or "gektar"
    if account == "gektar":
        return avito()
    if account == "dolina":
        if not avito2_подключён():
            raise AvitoНеПодключён(
                "кабинет «Светлая долина» не подключён: не заданы AVITO2_CLIENT_ID/AVITO2_CLIENT_SECRET"
            )
        return avito2()
    raise AvitoНеПодключён(f"неизвестный account={account!r}, допустимо: gektar | dolina")


class AvitoНеПодключён(RuntimeError):
    pass


@tool("avito_chats_list", "Список чатов аккаунта Авито. account: gektar (по умолчанию) | dolina", {"limit": int, "offset": int, "account": str})
async def avito_chats_list(args: dict) -> dict:
    try:
        cli = _avito(args)
        chats = cli.chats_list(limit=args.get("limit", 100), offset=args.get("offset", 0))
    except Exception as e:
        return _err(str(e))

    # Отдаём срез, а не сырой ответ: в полном виде 100 чатов не помещаются
    # в контекст, и агент теряет обзор именно там, где он нужен.
    me = cli.user_id
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


@tool("avito_chat_messages", "История сообщений чата Авито. account: gektar | dolina", {"chat_id": str, "limit": int, "account": str})
async def avito_chat_messages(args: dict) -> dict:
    try:
        cli = _avito(args)
        msgs = cli.chat_messages(args["chat_id"], limit=args.get("limit", 30))
    except Exception as e:
        return _err(str(e))

    me = cli.user_id
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
    {"chat_id": str, "text": str, "account": str},
)
async def avito_send_message(args: dict) -> dict:
    if MODE != "send":
        return _err(f"режим {MODE}: отправка клиентам отключена, сообщение НЕ ушло")

    # Стоп-кран темпа. До 16.08 он существовал только в тексте регламента —
    # и в тот день был превышен: 29 сообщений за семь минут, не меньше 33 за
    # час при потолке 30. Правило, которое проверяет только сам агент, рано или
    # поздно нарушается: в разгаре рассылки считать отправленные некому.
    # Больше 30 в час — риск блокировки аккаунта, а это вся база разом.
    if not _темп_позволяет():
        return _err(
            f"СТОП-КРАН ТЕМПА: за последний час уже отправлено {ПРЕДЕЛ_В_ЧАС} сообщений. "
            f"Отправка заблокирована. Останови рассылку, оставшихся вынеси в отчёт "
            f"списком и предупреди Ирика — решение о превышении принимает он."
        )

    cli = _avito(args)

    чужое = _чужое_исходящее(cli, args["chat_id"])
    if чужое is not None:
        текст = (чужое.get("content") or {}).get("text", "")
        return _err(
            "СТОП-КРАН: в этом чате уже ответили без тебя — скорее всего Ирик "
            "с телефона. Сообщение НЕ отправлено.\n\n"
            f"Что там написано: «{текст[:400]}»\n\n"
            "Перечитай переписку целиком (avito_chat_messages) и реши заново: "
            "если вопрос клиента уже закрыт — не пиши ничего, отметь это в "
            "отчёте. Если твой ответ добавляет то, чего в чате нет, — перепиши "
            "его так, чтобы он продолжал сказанное Ириком, а не повторял и не "
            "противоречил ему."
        )

    try:
        result = cli.send_message(args["chat_id"], args["text"])
    except Exception as e:
        return _err(str(e))
    if result.get("id"):
        _наши_id.add(str(result["id"]))
    _отправки.append(time.time())
    _sent.append({"chat_id": args["chat_id"], "text": args["text"], "account": args.get("account") or "gektar"})
    return _ok({"отправлено": True, "id": result.get("id")})


@tool("avito_item_info", "Карточка объявления Авито: площадь, цена, адрес. account: gektar | dolina", {"item_id": int, "account": str})
async def avito_item_info(args: dict) -> dict:
    try:
        return _ok(_avito(args).item_info(args["item_id"]))
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
            bitrix().crm_list(
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
        return _ok(bitrix().crm_get(args.get("entity", "deal"), args["id"]))
    except Exception as e:
        return _err(str(e))


@tool("b24_crm_add", "Создать сущность CRM. fields — JSON-строка", {"entity": str, "fields": str})
async def b24_crm_add(args: dict) -> dict:
    try:
        return _ok({"id": bitrix().crm_add(args.get("entity", "deal"), _json_arg(args["fields"], {}))})
    except Exception as e:
        return _err(str(e))


@tool(
    "b24_crm_update",
    "Обновить сущность CRM. fields — JSON-строка, например {\"STAGE_ID\":\"PREPARATION\"}. "
    "Причина отказа «не интересно» (59) требует поля причина_словами_клиента — "
    "дословной цитаты того, что человек ответил.",
    {"entity": str, "id": str, "fields": str, "причина_словами_клиента": str},
)
async def b24_crm_update(args: dict) -> dict:
    поля = _json_arg(args["fields"], {})

    беда = _отказ_без_причины(поля, args.get("причина_словами_клиента"))
    if беда:
        return _err(беда)

    try:
        итог = bitrix().crm_update(args.get("entity", "deal"), args["id"], поля)
    except Exception as e:
        return _err(str(e))

    # Цитату кладём в таймлайн: причина в поле — для отчётов, слова клиента —
    # для того, чтобы через месяц можно было перечитать и понять, что менять.
    цитата = (args.get("причина_словами_клиента") or "").strip()
    if цитата and str(поля.get("UF_CRM_FAIL_REASON")) == ОТКАЗ_НЕ_ИНТЕРЕСНО:
        try:
            bitrix().timeline_comment_add(
                args["id"], f"[B]Причина отказа, словами клиента:[/B] «{цитата}»"
            )
        except Exception:  # noqa: BLE001 — комментарий не должен ронять обновление
            pass

    return _ok({"обновлено": итог})


@tool("b24_timeline_comment", "Комментарий в таймлайн сделки", {"deal_id": str, "comment": str})
async def b24_timeline_comment(args: dict) -> dict:
    try:
        return _ok({"id": bitrix().timeline_comment_add(args["deal_id"], args["comment"])})
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
            bitrix().task_add(
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
        return _ok(bitrix().task_list(_json_arg(args.get("filter"), {"!STATUS": 5}), args.get("limit", 50)))
    except Exception as e:
        return _err(str(e))


@tool("b24_call", "Произвольный метод REST Битрикса. params — JSON-строка", {"method": str, "params": str})
async def b24_call(args: dict) -> dict:
    try:
        return _ok(bitrix().call(args["method"], _json_arg(args.get("params"), {})))
    except Exception as e:
        return _err(str(e))


# --- Телеграм ------------------------------------------------------------


@tool(
    "telegram_alert",
    "Срочное сообщение Ирику: сработал стоп-кран, конфликт, горячий клиент. "
    "Не для отчёта — отчёт уходит сам в конце прогона. "
    "Для уведомления о тёплом/горящем лиде ОБЯЗАТЕЛЬНО передай chat_id и account: "
    "перед отправкой проверяется, что клиент вообще писал в этот чат.",
    {"text": str, "chat_id": str, "account": str},
)
async def telegram_alert(args: dict) -> dict:
    беда = _лид_без_клиента(args)
    if беда:
        return _err(беда)
    try:
        telegram().send(args["text"], alert=True)
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
