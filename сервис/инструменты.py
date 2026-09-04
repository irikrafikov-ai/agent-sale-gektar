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
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
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


# Одно уведомление о лиде на клиента в день.
#
# 24.08 Ирик получил четыре алерта про Александра за 32 минуты: 8:40, 9:03,
# 9:09, 9:12. Клиент всё это время был один и тот же и всё это время был
# тёплым — просто разбор чата запускался четырежды, по числу его сообщений.
#
# Память здесь обязана быть на диске, а не в процессе: вебхук поднимает на
# каждый чат ОТДЕЛЬНЫЙ процесс прогона, и переменная в памяти живёт ровно до
# конца одного разбора. Именно поэтому дубли и появились.
#
# Файл в /tmp переживает разборы, но не переживает передеплой контейнера. Это
# осознанный размен: после деплоя возможен один лишний алерт, зато нет базы,
# которую надо поднимать и обслуживать. Один дубль в сутки дешевле, чем
# ежедневные четыре.
МСК = timezone(timedelta(hours=3))
ПАМЯТЬ_ЛИДОВ = Path(os.environ.get("ПАМЯТЬ_ЛИДОВ", "/tmp/лиды-уведомлены.json"))


def _уже_уведомляли(chat_id: str) -> bool:
    """Писали ли Ирику про этот чат сегодня. Сбой чтения — считаем, что нет:
    пропущенный лид дороже лишнего уведомления."""
    сегодня = datetime.now(МСК).strftime("%Y-%m-%d")
    try:
        память = json.loads(ПАМЯТЬ_ЛИДОВ.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return False
    return память.get(chat_id) == сегодня


def _запомнить_уведомление(chat_id: str) -> None:
    сегодня = datetime.now(МСК).strftime("%Y-%m-%d")
    try:
        память = json.loads(ПАМЯТЬ_ЛИДОВ.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        память = {}
    # Вчерашние записи выбрасываем: файл не должен расти вечно, а «сегодня»
    # по определению не зависит от того, что было позавчера.
    память = {к: v for к, v in память.items() if v == сегодня}
    память[chat_id] = сегодня
    try:
        ПАМЯТЬ_ЛИДОВ.write_text(json.dumps(память, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001 — не смогли запомнить, но алерт уже ушёл
        pass


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


def служебное_авито(сообщение: dict) -> bool:
    """Уведомление площадки, а не реплика человека.

    Правило Ирика 03.09.2026: служебные сообщения Авито НИКОГДА не считаются
    общением с клиентом — ни за входящее, ни за исходящее. Они технические.

    Цена ошибки известна: 03.09 в 08:49:35 Авито прислало «Снимите объявление
    с публикации…» (author_id 1). Стоп-кран принял его за реплику клиента,
    сдвинул точку отсчёта на это время — и пять сообщений, которые Ирик писал
    вручную с 08:47, оказались «старыми». Чужое исходящее не нашлось, и агент
    отправил ответ поверх живой переписки Ирика.
    """
    if сообщение.get("type") == "system":
        return True
    return str(сообщение.get("author_id")) in ("0", "1")


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
    # Служебные Авито из расчёта ИСКЛЮЧЕНЫ: они не общение, а техника.
    последнее_входящее = max(
        (
            m.get("created", 0)
            for m in msgs
            if m.get("author_id") != me and not служебное_авито(m)
        ),
        default=0,
    )
    порог = time.time() - ОКНО_ЧУЖОГО

    чужие = [
        m for m in msgs
        if m.get("author_id") == me
        and not служебное_авито(m)
        and str(m.get("id")) not in _наши_id
        and m.get("created", 0) > последнее_входящее
        and m.get("created", 0) > порог
    ]
    return max(чужие, key=lambda m: m.get("created", 0)) if чужие else None


# Слова, которых не бывает в имени человека. Авито в поле имени часто держит
# название организации или ник — «Завод ЖБИ Аврора», «ТК КОННОР», «АН
# Эксклюзив», «Пользователь». Подставленное в приветствие, оно мгновенно
# выдаёт робота: живой человек так не пишет никогда.
НЕ_ИМЯ = (
    "ооо", "оао", "зао", "ип ", "завод", "компания", "компаний", "фирма",
    "агентство", "магазин", "студия", "салон", "центр", "групп", "group",
    "строй", "торг", "тк ", "ан ", "пользователь", "покупатель", "user",
    "продавец", "доставка", "почтой", "маркет", "склад", "сервис",
)


# Имена, по которым обращаться МОЖНО. Список разрешений, а не запретов —
# и это принципиально. 03.09 стоп-кран строился на списке запретных слов
# («завод», «ООО», «агентство»…), и 04.09 в 14:58 сквозь него прошло
# «Технологии, добрый день)»: аккаунт назывался «Технологии Безопасности»,
# агент взял первое слово, а слова «технологии» в запретах не оказалось.
# Список запретов неполон всегда — названий фирм бесконечно много, имён
# конечное число. Не нашли имя в списке — просто не обращаемся по имени.
# ⚠️ Имя переменной уникальное: ИМЕНА в этом файле уже занято списком
# инструментов агента. 04.09 первая версия назвалась ИМЕНА, молча
# затёрлась — и проверка пропускала вообще всё.
ЛИЧНЫЕ_ИМЕНА = set("""
александр алексей анатолий андрей антон арсений артём артем богдан борис вадим
валентин валерий василий виктор виталий владимир владислав вячеслав геннадий
георгий глеб григорий даниил данил денис дмитрий евгений егор иван игорь илья
кирилл константин лев леонид максим марк матвей михаил никита николай олег
павел пётр петр роман руслан сергей станислав степан тимофей тимур фёдор федор
филипп эдуард юрий ярослав арсен артур ринат рустам марат дамир ильдар айрат
азат ленар радик рафаэль амир булат камиль наиль тагир фарид шамиль эльдар
музаффар анушервон бахтиёр джамшед фаррух сухроб хуршед аллаберди
александра алёна алена алина алла анастасия ангелина анна антонина валентина
валерия вера вероника виктория галина дарья диана евгения екатерина елена
елизавета жанна зинаида инна ирина карина кира кристина ксения лариса лидия
любовь людмила маргарита марина мария надежда наталья наталия нелли нина оксана
ольга полина раиса регина светлана снежана софия софья тамара татьяна ульяна
юлия яна алсу гузель динара лейла лилия мадина рената эльвира эльмира
""".split())

# Латиницей клиенты пишутся часто — держим отдельный набор.
ЛИЧНЫЕ_ИМЕНА |= set("""
alex alexander aleksandr alexey aleksey andrey andrei anton artem artyom denis
dmitry dmitrii egor evgeny igor ilya ivan kirill konstantin maksim maxim mikhail
nikita nikolay oleg pavel roman ruslan sergey sergei stanislav timur vadim
valery victor viktor vladimir vladislav yuri yury anna alena alina anastasia
daria diana ekaterina elena elizaveta irina julia karina kira ksenia larisa
maria marina nadezhda natalia natalya olga polina svetlana tatiana tatyana vera
veronika victoria viktoria yana
""".split())


def обращение_к_не_человеку(текст: str) -> str | None:
    """Начинается ли сообщение обращением по НЕ-человеческому имени.

    Правило Ирика 03.09.2026, после «Завод ЖБИ Аврора, доброе утро 🙂».
    Смотрим только начало — там, где стоит обращение: «<Имя>, добрый день».
    Возвращаем найденное обращение, если оно явно не имя человека.
    """
    первая = (текст or "").strip().split("\n", 1)[0]
    if "," not in первая:
        return None
    обращение = первая.split(",", 1)[0].strip()
    if not обращение or len(обращение) > 60:
        return None
    низ = обращение.lower()

    # Быстрые отсечки: цифры и явные признаки организации.
    if any(с.isdigit() for с in обращение):
        return обращение
    if any(м in низ + " " for м in НЕ_ИМЯ):
        return обращение
    # Имя человека — одно слово или «Имя Фамилия». Три и больше слов подряд
    # перед запятой — почти всегда название или приписка к нику.
    if len(обращение.split()) >= 3:
        return обращение

    # ГЛАВНАЯ ПРОВЕРКА: первое слово должно быть известным именем.
    # Не нашли — считаем, что это не имя, и обращаться по нему нельзя.
    первое = низ.split()[0].strip("«»\"'()-").replace("ё", "е")
    if первое in ЛИЧНЫЕ_ИМЕНА or первое.replace("е", "ё") in ЛИЧНЫЕ_ИМЕНА:
        return None
    return обращение


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

    if (плохое := обращение_к_не_человеку(args.get("text", ""))):
        return _err(
            "СТОП-КРАН: обращение «" + плохое + "» — это не имя человека, "
            "а название или ник из профиля Авито. Сообщение НЕ отправлено.\n\n"
            "Так пишет только робот, и клиент это сразу видит. Убери обращение "
            "совсем («Добрый день 🙂 …») либо спроси «Как к вам обращаться?» "
            "и дальше зови человека так, как он ответит."
        )

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


@tool(
    "avito_calls",
    "Звонки через Авито за период: телефон покупателя, время, длительность разговора. "
    "Сверяй ПЕРЕД письмом клиенту, был ли с ним разговор (матч по телефону из Битрикса). "
    "Записей и содержания разговоров нет — только факт и длительность. "
    "days — за сколько последних дней (по умолчанию 3). account: gektar | dolina",
    {"days": int, "account": str},
)
async def avito_calls(args: dict) -> dict:
    try:
        from datetime import datetime, timedelta, timezone

        дней = min(int(args.get("days") or 3), 30)
        до = datetime.now(timezone.utc)
        от = до - timedelta(days=дней)
        cli = _avito(args)
        звонки = cli.calls(
            от.strftime("%Y-%m-%dT%H:%M:%SZ"), до.strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        out = [
            {
                "телефон_покупателя": з.get("buyerPhone"),
                "когда": з.get("callTime"),
                "разговор_сек": з.get("talkDuration"),
                "дозвонился": bool(з.get("talkDuration")),
            }
            for з in звонки
        ]
        return _ok({"звонков": len(out), "список": out})
    except Exception as e:
        return _err(str(e))


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
    "handoff_chat",
    "Передать чат Opus-редактору: отдельный разбор составит и отправит клиенту "
    "грамотное сообщение. Для полного прогона это ЕДИНСТВЕННЫЙ способ написать "
    "касание: сам текст клиенту не сочиняй. В поводе передай всё, что накопал: "
    "номер касания, что человек спрашивал, на чём затих, какой участок обсуждали.",
    {"chat_id": str, "account": str, "повод": str},
)
async def handoff_chat(args: dict) -> dict:
    """Двухъярусная схема, решение Ирика 25.08.2026: Sonnet сканирует и находит,
    кому писать, Opus составляет само сообщение.

    Дешёвая модель отлично решает «кому и зачем» — это перебор фактов. Дорогая
    нужна там, где рождается текст клиенту: одна корявая фраза в первом касании
    стоит больше, чем вся экономия на прогоне. Замер по 153 чатам: формулировка
    первого сообщения — это разница между 67% и 14% ответов.

    Технически: запускается отдельный чат-разбор (прогон.py чат …) — тот же
    процесс, что отвечает на живые события вебхука. Он берёт модель из
    AGENT_MODEL_ЧАТ (Opus с 25.08), сам читает переписку, Битрикс и базу знаний
    и сам решает, что и как написать. Замок чата защищает от параллельной
    работы с вебхуком.
    """
    if MODE != "send":
        return _err(f"режим {MODE}: передача чатов отключена")

    chat_id = (args.get("chat_id") or "").strip()
    if not chat_id:
        return _err("нужен chat_id")

    процесс = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parent / "прогон.py"),
            "чат",
            chat_id,
            args.get("account") or "gektar",
            (args.get("повод") or "касание по каденции")[:500],
        ],
        capture_output=True,
        text=True,
        timeout=480,
    )
    if процесс.returncode != 0:
        return _err(
            f"чат-разбор завершился с кодом {процесс.returncode}: "
            f"{(процесс.stderr or '')[-300:]}"
        )
    return _ok({"передано": True, "chat_id": chat_id})


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

    лид = _похоже_на_лид(args.get("text") or "")
    chat_id = (args.get("chat_id") or "").strip()

    if лид and chat_id and _уже_уведомляли(chat_id):
        return _err(
            f"СТОП-КРАН: про чат {chat_id} Ирику сегодня уже сообщали. "
            "Уведомление НЕ отправлено — и это не ошибка.\n\n"
            "Клиент не становится «более тёплым» оттого, что написал ещё раз: "
            "он тот же самый и уже взят на контроль. 24.08 про Александра ушло "
            "четыре алерта за 32 минуты, и каждый следующий обесценивал "
            "предыдущий.\n\n"
            "Веди диалог дальше как обычно. Новое из переписки — телефон, "
            "согласие на встречу, выбранный участок — записывай в таймлайн "
            "сделки и в вечерний отчёт: там это увидят в собранном виде.\n\n"
            "Отдельный алерт по этому чату уместен завтра или позже — если "
            "клиент пропадал и снова вышел в интерес."
        )

    try:
        telegram().send(args["text"], alert=True)
    except Exception as e:
        return _err(str(e))

    if лид and chat_id:
        _запомнить_уведомление(chat_id)
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
        handoff_chat,
        avito_calls,
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
    "mcp__gektar__handoff_chat",
    "mcp__gektar__avito_calls",
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
