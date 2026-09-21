"""Данные для дашборда агента продаж — из Битрикса, без модели.

Требование Ирика 21.09.2026: отдельный сайт, где видно текущее положение дел
агента продаж на Авито и как показатели менялись во времени.

Откуда что берётся (всё — воронка «Прямые покупатели», CATEGORY_ID=0):
  · воронка сейчас        — crm.deal.list по стадиям;
  · динамика по дням      — crm.stagehistory.list: первый раз, когда сделка
                            дошла до вехи (квалификация / тёплый / продажа /
                            отказ), — по одному разу на сделку;
  · новые лиды по дням    — DATE_CREATE сделок, без дублей и служебных;
  · диалоги Авито по дням — строки МЕТРИКИ_ДНЯ из журнала (сделка 3139),
                            их пишет вечерний прогон с 21.09.2026;
  · расход по дням        — ИТОГ_ДНЯ + сырые РАСХОД оттуда же;
  · молчаливые разборы    — МОЛЧАНИЕ оттуда же;
  · уроки и гипотезы      — журнал обучения (сделка 3397).

Цифры считает код, чистыми функциями от списков — их можно проверить
тестом без Битрикса. Обращение к Битриксу — только в собрать(), с кэшем.
"""

from __future__ import annotations

import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import обучение  # noqa: E402
import расход  # noqa: E402
from интеграции.bitrix import Bitrix  # noqa: E402

МСК = timezone(timedelta(hours=3))

СТАДИИ = [
    ("NEW", "Новый лид"),
    ("EXECUTING", "Прогрев"),
    ("PREPARATION", "Квалификация"),
    ("UC_CONTACT", "Контакт получен"),
    ("UC_CALL_DONE", "Созвон"),
    ("UC_MEET_SET", "Встреча назначена"),
    ("UC_MEET_DONE", "Встреча проведена"),
    ("FINAL_INVOICE", "Бронь"),
    ("UC_DOGOVOR", "Договор и оплата"),
    ("UC_RASSROCHKA", "Рассрочка"),
    ("WON", "Продано"),
    ("LOSE", "Отказ"),
]
НАЗВАНИЕ_СТАДИИ = dict(СТАДИИ)
ПОРЯДОК = {к: i for i, (к, _) in enumerate(СТАДИИ)}

ТЁПЛЫЕ = ("UC_CONTACT", "UC_CALL_DONE", "UC_MEET_SET", "UC_MEET_DONE", "FINAL_INVOICE", "UC_DOGOVOR")
ПРОДАЖА = ("WON", "UC_RASSROCHKA")
КВАЛИФИКАЦИЯ_ОТ = ПОРЯДОК["PREPARATION"]

ПРИЧИНЫ = {
    "55": "не отвечает", "57": "дорого", "59": "не интересно",
    "61": "купил в другом месте", "63": "локация", "65": "нецелевой",
    "93": "дубль", "95": "не выяснена",
}

КЭШ_СЕКУНД = int(os.environ.get("ДАШБОРД_КЭШ_СЕКУНД", "300"))


def служебная(сделка: dict) -> bool:
    """Техучёт, журнал, дубли — не лиды."""
    т = (сделка.get("TITLE") or "").upper()
    return (
        "ТЕХУЧЁТ" in т or "ЖУРНАЛ ОБУЧЕНИЯ" in т or "ДУБЛЬ" in т
        or str(сделка.get("UF_CRM_FAIL_REASON") or "") in ("93",)
    )


def _дата(строка: str) -> str:
    """ISO Битрикса → ГГГГ-ММ-ДД по Москве."""
    try:
        return datetime.fromisoformat(строка).astimezone(МСК).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return ""


def дни(конец: datetime, сколько: int) -> list[str]:
    return [(конец - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(сколько - 1, -1, -1)]


# --- чистые расчёты ------------------------------------------------------------


def воронка(сделки: list[dict]) -> list[dict]:
    """Сколько карточек и денег на каждой стадии сейчас."""
    счёт: dict[str, dict] = {к: {"стадия": к, "название": н, "сделок": 0, "сумма": 0.0} for к, н in СТАДИИ}
    for с in сделки:
        if служебная(с):
            continue
        ст = с.get("STAGE_ID") or ""
        if ст in счёт:
            счёт[ст]["сделок"] += 1
            счёт[ст]["сумма"] += float(с.get("OPPORTUNITY") or 0)
    return list(счёт.values())


def вехи_по_дням(история: list[dict], сделки: list[dict]) -> dict[str, dict[str, int]]:
    """Из истории стадий — по дням: сколько сделок ВПЕРВЫЕ дошло до вехи.

    Одна сделка считается на вехе один раз (первое попадание), даже если её
    гоняли туда-сюда. Служебные и дубли исключены.
    """
    служебные = {str(с["ID"]) for с in сделки if служебная(с)}
    первое: dict[tuple[str, str], str] = {}  # (сделка, веха) → дата
    for з in sorted(история, key=lambda з: з.get("CREATED_TIME") or ""):
        ид = str(з.get("OWNER_ID"))
        if ид in служебные:
            continue
        ст = з.get("STAGE_ID") or ""
        д = _дата(з.get("CREATED_TIME") or "")
        if not д:
            continue
        вехи = []
        if ПОРЯДОК.get(ст, -1) >= КВАЛИФИКАЦИЯ_ОТ and ст not in ("LOSE",):
            вехи.append("квалификация")
        if ст in ТЁПЛЫЕ or ст in ПРОДАЖА:
            вехи.append("тёплые")
        if ст in ПРОДАЖА:
            вехи.append("продажи")
        if ст == "LOSE":
            вехи.append("отказы")
        for в in вехи:
            первое.setdefault((ид, в), д)
    # Дата ПРОДАЖИ — из карточки, а не из истории стадий: продажи №8, №3,
    # №2 внесены в Битрикс на ревизии 17–19.09, а состоялись в августе–
    # сентябре. В карточке стоит дата ДКП: CLOSEDATE у закрытых, BEGINDATE
    # у рассрочки (её CLOSEDATE — плановый конец платежей).
    for с in сделки:
        ст = с.get("STAGE_ID") or ""
        if служебная(с) or ст not in ПРОДАЖА:
            continue
        д = _дата((с.get("BEGINDATE") if ст == "UC_RASSROCHKA" else с.get("CLOSEDATE")) or "")
        if д:
            первое[(str(с["ID"]), "продажи")] = д
    итог: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for (_, в), д in первое.items():
        итог[д][в] += 1
    return {д: dict(v) for д, v in итог.items()}


def новые_по_дням(сделки: list[dict]) -> dict[str, int]:
    итог: dict[str, int] = defaultdict(int)
    for с in сделки:
        if служебная(с):
            continue
        д = _дата(с.get("DATE_CREATE") or "")
        if д:
            итог[д] += 1
    return dict(итог)


def журнал_по_дням(комментарии: list[dict]) -> dict[str, dict]:
    """Строки журнала расхода → по дням: usd, запусков, молчаний, метрики."""
    итог: dict[str, dict] = defaultdict(lambda: {"usd": 0.0, "запусков": 0, "молчаний": 0, "метрики": None})
    закрытые: set[str] = set()
    for к in комментарии:
        т = к.get("COMMENT") or ""
        if и := расход.разобрать_итог_дня(т):
            итог[и["дата"]]["usd"] = и["usd"]
            итог[и["дата"]]["запусков"] = и["запусков"]
            закрытые.add(и["дата"])
        elif м := расход.разобрать_метрики_дня(т):
            итог[м["дата"]]["метрики"] = м
        elif мл := расход.разобрать_молчание(т):
            итог[расход._дата_мск(мл["когда"])]["молчаний"] += 1
    for к in комментарии:
        з = расход.разобрать(к.get("COMMENT") or "")
        if not з:
            continue
        д = расход._дата_мск(з["когда"])
        if д in закрытые:
            continue
        итог[д]["usd"] = round(итог[д]["usd"] + з["usd"], 4)
        итог[д]["запусков"] += 1
    return dict(итог)


def динамика(
    сделки: list[dict], история: list[dict], журнал: list[dict], конец: datetime, дней: int
) -> list[dict]:
    вехи = вехи_по_дням(история, сделки)
    новые = новые_по_дням(сделки)
    жур = журнал_по_дням(журнал)
    ряд = []
    for д in дни(конец, дней):
        в = вехи.get(д, {})
        ж = жур.get(д, {})
        м = ж.get("метрики") or {}
        ряд.append({
            "дата": д,
            "новых": новые.get(д, 0),
            "квалификация": в.get("квалификация", 0),
            "тёплых": в.get("тёплые", 0),
            "продаж": в.get("продажи", 0),
            "отказов": в.get("отказы", 0),
            "расход_usd": round(ж.get("usd", 0.0), 2),
            "запусков": ж.get("запусков", 0),
            "молчаний": ж.get("молчаний", 0),
            "диалогов": м.get("диалогов"),
            "с_ответом": м.get("с_ответом"),
            "отправлено": м.get("отправлено"),
        })
    return ряд


def итоги(ряд: list[dict]) -> dict:
    s = lambda к: sum((r.get(к) or 0) for r in ряд)  # noqa: E731
    новых, тёплых, продаж, отказов, расход_usd = s("новых"), s("тёплых"), s("продаж"), s("отказов"), s("расход_usd")
    пр = lambda а, б: round(100 * а / б, 1) if б else 0.0  # noqa: E731
    return {
        "дней": len(ряд),
        "новых": новых,
        "квалификация": s("квалификация"),
        "тёплых": тёплых,
        "продаж": продаж,
        "отказов": отказов,
        "расход_usd": round(расход_usd, 2),
        "конверсия_в_тёплые": пр(тёплых, новых),
        "конверсия_в_продажу": пр(продаж, новых),
        "конверсия_тёплые_в_продажу": пр(продаж, тёплых),
        "расход_на_лид": round(расход_usd / новых, 2) if новых else 0.0,
        "расход_на_тёплого": round(расход_usd / тёплых, 2) if тёплых else 0.0,
        "диалогов": s("диалогов"),
        "с_ответом": s("с_ответом"),
        "отправлено": s("отправлено"),
        "молчаний": s("молчаний"),
    }


def продажи(сделки: list[dict]) -> list[dict]:
    out = []
    for с in сделки:
        if служебная(с) or (с.get("STAGE_ID") or "") not in ПРОДАЖА:
            continue
        out.append({
            "id": int(с["ID"]),
            "название": с.get("TITLE") or "",
            "стадия": НАЗВАНИЕ_СТАДИИ.get(с.get("STAGE_ID"), с.get("STAGE_ID")),
            "сумма": float(с.get("OPPORTUNITY") or 0),
            # дата ДКП: у рассрочки CLOSEDATE — плановый конец платежей
            "дата": _дата((с.get("BEGINDATE") if с.get("STAGE_ID") == "UC_RASSROCHKA" else с.get("CLOSEDATE")) or ""),
        })
    return sorted(out, key=lambda x: x["дата"], reverse=True)


def тёплые_в_работе(сделки: list[dict], сейчас: datetime) -> list[dict]:
    out = []
    for с in сделки:
        if служебная(с) or (с.get("STAGE_ID") or "") not in ТЁПЛЫЕ:
            continue
        try:
            с_момента = datetime.fromisoformat(с.get("MOVED_TIME") or "").astimezone(МСК)
            дней = (сейчас - с_момента).days
        except (TypeError, ValueError):
            дней = None
        out.append({
            "id": int(с["ID"]),
            "название": с.get("TITLE") or "",
            "стадия": НАЗВАНИЕ_СТАДИИ.get(с.get("STAGE_ID"), с.get("STAGE_ID")),
            "порядок": ПОРЯДОК.get(с.get("STAGE_ID"), 0),
            "дней_в_стадии": дней,
            "сумма": float(с.get("OPPORTUNITY") or 0),
        })
    return sorted(out, key=lambda x: (-x["порядок"], -(x["дней_в_стадии"] or 0)))


def причины_отказов(сделки: list[dict], история: list[dict], с_даты: str) -> dict[str, int]:
    """Причины отказов у сделок, ушедших в LOSE не раньше с_даты."""
    отказавшиеся = {
        str(з.get("OWNER_ID"))
        for з in история
        if (з.get("STAGE_ID") == "LOSE") and _дата(з.get("CREATED_TIME") or "") >= с_даты
    }
    итог: dict[str, int] = defaultdict(int)
    for с in сделки:
        if str(с.get("ID")) in отказавшиеся and not служебная(с):
            код = str(с.get("UF_CRM_FAIL_REASON") or "")
            итог[ПРИЧИНЫ.get(код, "не указана")] += 1
    return dict(sorted(итог.items(), key=lambda x: -x[1]))


# --- сборка с Битриксом --------------------------------------------------------

_кэш: dict[str, tuple[float, dict]] = {}


def _все_сделки(b: Bitrix) -> list[dict]:
    return b.crm_list(
        "deal",
        filter={"CATEGORY_ID": 0},
        select=["ID", "TITLE", "STAGE_ID", "OPPORTUNITY", "DATE_CREATE", "CLOSEDATE", "BEGINDATE",
                "MOVED_TIME", "UF_CRM_FAIL_REASON", "ORIGIN_ID"],
        limit=5000,
    )


def _история(b: Bitrix, с_даты: str) -> list[dict]:
    out: list[dict] = []
    старт = 0
    while True:
        r = b.call("crm.stagehistory.list", {
            "entityTypeId": 2,
            "filter": {"CATEGORY_ID": 0, ">=CREATED_TIME": f"{с_даты}T00:00:00"},
            "select": ["ID", "OWNER_ID", "CREATED_TIME", "STAGE_ID"],
            "order": {"ID": "ASC"},
            "start": старт,
        }) or {}
        порция = r.get("items") or []
        out.extend(порция)
        if len(порция) < 50:
            break
        старт += 50
        if старт > 20000:
            break
    return out


def _журнал(b: Bitrix, сделка: str, предел: int = 2000) -> list[dict]:
    out: list[dict] = []
    старт = 0
    while len(out) < предел:
        порция = b.call("crm.timeline.comment.list", {
            "filter": {"ENTITY_ID": int(сделка), "ENTITY_TYPE": "deal"},
            "order": {"ID": "DESC"},
            "start": старт,
        }) or []
        out.extend(порция)
        if len(порция) < 50:
            break
        старт += 50
    return out


ПЕРИОДЫ = (7, 30, 90)


def в_кэше(дней: int) -> bool:
    return f"{дней}" in _кэш


def собрать(дней: int = 30, сейчас: datetime | None = None, свежие: bool = False) -> dict:
    """Данные за период. Сборка — ~40 с (70 запросов к Битриксу), поэтому
    страница отдаёт кэш, а обновляет его фоновый поток (см. дашборд.py);
    свежие=True заставляет пересобрать."""
    сейчас = сейчас or datetime.now(МСК)
    ключ = f"{дней}"
    if not свежие and ключ in _кэш:
        return _кэш[ключ][1]

    # История и журнал нужны с запасом — для сравнения с прошлым периодом.
    с_даты = (сейчас - timedelta(days=дней * 2 + 1)).strftime("%Y-%m-%d")
    b = Bitrix()
    try:
        сделки = _все_сделки(b)
        история = _история(b, с_даты)
        журнал = _журнал(b, расход.СДЕЛКА_УЧЁТА)
    finally:
        b.close()

    ряд = динамика(сделки, история, журнал, сейчас, дней * 2)
    текущий, прошлый = ряд[дней:], ряд[:дней]
    ж = обучение.прочитать(дней=60)

    данные = {
        "обновлено": сейчас.isoformat(),
        "дней": дней,
        "воронка": воронка(сделки),
        "динамика": текущий,
        "итоги": итоги(текущий),
        "итоги_прошлого_периода": итоги(прошлый),
        "продажи": продажи(сделки),
        "тёплые": тёплые_в_работе(сделки, сейчас),
        "причины_отказов": причины_отказов(сделки, история, текущий[0]["дата"] if текущий else с_даты),
        "расход_месяц": расход.за_месяц(сейчас.timestamp()),
        "обучение": {
            "уроки": [{"дата": расход._дата_мск(у["когда"]), "текст": у["текст"]} for у in ж["уроки"][:20]],
            "гипотезы": [
                {"номер": г["номер"], "статус": г["статус"], "текст": г["текст"],
                 "проверка": г["проверка"], "метрика": г["метрика"], "дата": расход._дата_мск(г["когда"])}
                for г in ж["гипотезы"]
            ],
        },
        "в_работе_всего": sum(в["сделок"] for в in воронка(сделки) if в["стадия"] not in ("WON", "LOSE")),
    }
    _кэш[ключ] = (time.time(), данные)
    return данные


def обновлять_в_фоне() -> None:
    """Бесконечный цикл для фонового потока: пересобирать все периоды."""
    while True:
        for дней in ПЕРИОДЫ:
            try:
                собрать(дней, свежие=True)
            except Exception as ошибка:  # noqa: BLE001 — старый кэш лучше пустого
                print(f"[дашборд] период {дней}: {type(ошибка).__name__}: {ошибка}", file=sys.stderr, flush=True)
        time.sleep(КЭШ_СЕКУНД)
