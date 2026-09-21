"""Дашборд агента продаж — расчёты без Битрикса (Ирик, 21.09.2026)."""

from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
for имя in ("интеграции", "интеграции.avito", "интеграции.bitrix", "интеграции.telegram"):
    sys.modules.setdefault(имя, types.ModuleType(имя))
sys.modules["интеграции.bitrix"].Bitrix = object
sys.modules["интеграции.avito"].Avito = object
sys.modules["интеграции.avito"].AvitoError = Exception
sys.modules["интеграции.telegram"].Telegram = object

import дашборд_данные as д  # noqa: E402
import расход  # noqa: E402

провалов = 0


def проверить(что, получили, ждали):
    global провалов
    ок = получили == ждали
    провалов += 0 if ок else 1
    print(f"  {'✅' if ок else '❌'} {что}: {получили!r} (ждали {ждали!r})")


МСК = timezone(timedelta(hours=3))
сейчас = datetime(2026, 9, 21, 12, 0, tzinfo=МСК)
t = lambda д, ч=10: datetime(2026, 9, д, ч, tzinfo=МСК).isoformat()  # noqa: E731

сделки = [
    {"ID": "1", "TITLE": "Иван (Авито)", "STAGE_ID": "UC_CONTACT", "OPPORTUNITY": "300000", "DATE_CREATE": t(18), "MOVED_TIME": t(19)},
    {"ID": "2", "TITLE": "Пётр (Авито)", "STAGE_ID": "WON", "OPPORTUNITY": "268000", "DATE_CREATE": t(10), "CLOSEDATE": t(20), "MOVED_TIME": t(20)},
    {"ID": "3", "TITLE": "Олег — ДУБЛЬ", "STAGE_ID": "LOSE", "OPPORTUNITY": "0", "DATE_CREATE": t(19), "UF_CRM_FAIL_REASON": "93", "MOVED_TIME": t(19)},
    {"ID": "4", "TITLE": "⚙️ ТЕХУЧЁТ", "STAGE_ID": "LOSE", "OPPORTUNITY": "0", "DATE_CREATE": t(9), "UF_CRM_FAIL_REASON": "65", "MOVED_TIME": t(9)},
    {"ID": "5", "TITLE": "Анна (Авито)", "STAGE_ID": "LOSE", "OPPORTUNITY": "0", "DATE_CREATE": t(19), "UF_CRM_FAIL_REASON": "59", "MOVED_TIME": t(20)},
    {"ID": "6", "TITLE": "Юля (Авито)", "STAGE_ID": "NEW", "OPPORTUNITY": "0", "DATE_CREATE": t(21, 9), "MOVED_TIME": t(21, 9)},
    {"ID": "7", "TITLE": "Ирина · РАССРОЧКА", "STAGE_ID": "UC_RASSROCHKA", "OPPORTUNITY": "295000", "DATE_CREATE": t(4), "BEGINDATE": t(4), "CLOSEDATE": "2027-09-03T00:00:00+03:00", "MOVED_TIME": t(19)},
]
история = [
    {"OWNER_ID": 1, "STAGE_ID": "NEW", "CREATED_TIME": t(18)},
    {"OWNER_ID": 1, "STAGE_ID": "PREPARATION", "CREATED_TIME": t(18, 12)},
    {"OWNER_ID": 1, "STAGE_ID": "UC_CONTACT", "CREATED_TIME": t(19)},
    {"OWNER_ID": 1, "STAGE_ID": "PREPARATION", "CREATED_TIME": t(19, 11)},   # откат — не должен считаться
    {"OWNER_ID": 1, "STAGE_ID": "UC_CONTACT", "CREATED_TIME": t(20)},        # повторно — не считается
    {"OWNER_ID": 2, "STAGE_ID": "UC_DOGOVOR", "CREATED_TIME": t(19)},
    {"OWNER_ID": 2, "STAGE_ID": "WON", "CREATED_TIME": t(20)},
    {"OWNER_ID": 3, "STAGE_ID": "LOSE", "CREATED_TIME": t(19)},              # дубль — не считается
    {"OWNER_ID": 5, "STAGE_ID": "LOSE", "CREATED_TIME": t(20)},
    {"OWNER_ID": 7, "STAGE_ID": "UC_RASSROCHKA", "CREATED_TIME": t(19)},
]
u = lambda д, ч=12: int(datetime(2026, 9, д, ч, tzinfo=МСК).timestamp())  # noqa: E731
журнал = [
    {"COMMENT": f"ИТОГ_ДНЯ|{u(20)}|2026-09-19|14.2200|20"},
    {"COMMENT": f"РАСХОД|{u(20, 9)}|утро|gektar|claude-sonnet-5|2.0000|1|2|3|4"},
    {"COMMENT": f"РАСХОД|{u(20, 15)}|чат|gektar|claude-opus-5|0.5000|1|2|3|4"},
    {"COMMENT": f"МОЛЧАНИЕ|{u(20, 16)}|gektar|u2i-x|клиент написал сообщение"},
    {"COMMENT": racход if False else расход.строка_метрик({"диалогов": 30, "с_ответом": 9, "отправлено": 20, "новых_лидов": 5, "новые_тёплые": [1, 2]}, u(20, 18))},
    {"COMMENT": "📞 Звонок"},
]

print("\n1. Воронка")
в = {x["стадия"]: x for x in д.воронка(сделки)}
проверить("контакт — 1 сделка на 300k", (в["UC_CONTACT"]["сделок"], в["UC_CONTACT"]["сумма"]), (1, 300000.0))
проверить("дубль и техучёт в LOSE не считаются", в["LOSE"]["сделок"], 1)
проверить("рассрочка видна", в["UC_RASSROCHKA"]["сделок"], 1)

print("\n2. Вехи по дням из истории стадий")
вехи = д.вехи_по_дням(история, сделки)
проверить("квалификация 18.09 — сделка 1 впервые", вехи["2026-09-18"], {"квалификация": 1})
проверить("тёплый 19.09 — сделки 1, 2, 7; квалификация 19.09 — 2 и 7; продажа рассрочки — по BEGINDATE 04.09, не здесь",
          вехи["2026-09-19"], {"тёплые": 3, "квалификация": 2})
проверить("продажа рассрочки датирована ДКП 04.09", вехи["2026-09-04"], {"продажи": 1})
проверить("20.09: продажа 2 (CLOSEDATE), отказ 5; повтор контакта сделки 1 не считается", вехи["2026-09-20"], {"продажи": 1, "отказы": 1})

print("\n3. Динамика и итоги")
ряд = д.динамика(сделки, история, журнал, сейчас, 4)
проверить("4 дня по порядку", [r["дата"][-2:] for r in ряд], ["18", "19", "20", "21"])
r20 = next(r for r in ряд if r["дата"].endswith("20"))
проверить("расход 20.09 из сырых строк", (r20["расход_usd"], r20["запусков"]), (2.5, 2))
r19 = next(r for r in ряд if r["дата"].endswith("19"))
проверить("расход 19.09 из итога дня", (r19["расход_usd"], r19["запусков"]), (14.22, 20))
проверить("молчание и метрики 20.09", (r20["молчаний"], r20["диалогов"], r20["с_ответом"]), (1, 30, 9))
проверить("новых 21.09 — Юля", next(r for r in ряд if r["дата"].endswith("21"))["новых"], 1)
и = д.итоги(ряд)
проверить("новых за период: 1, 3(дубль — нет), 5, 6 → 3", и["новых"], 3)
проверить("конверсия тёплые → продажа считается (в окне одна продажа)", и["конверсия_тёплые_в_продажу"], round(100 * 1 / 3, 1))
проверить("расход на лид", и["расход_на_лид"], round(16.72 / 3, 2))

print("\n4. Списки")
проверить("продажи — WON и рассрочка, по дате ДКП (рассрочка — BEGINDATE)", [p["id"] for p in д.продажи(сделки)], [2, 7])
тёпл = д.тёплые_в_работе(сделки, сейчас)
проверить("тёплые в работе — сделка 1, 2 дня на стадии", (тёпл[0]["id"], тёпл[0]["дней_в_стадии"]), (1, 2))
проверить("причины отказов за период (без дубля)", д.причины_отказов(сделки, история, "2026-09-18"), {"не интересно": 1})

print(f"\n{'ВСЁ ЗЕЛЁНОЕ' if not провалов else f'ПРОВАЛОВ: {провалов}'}")
sys.exit(1 if провалов else 0)
