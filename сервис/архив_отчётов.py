"""Архив ежедневных отчётов агента — история того, что уходило Ирику.

Распоряжение Ирика 21.09.2026: отчёты, которые агент отправляет в Телеграм,
сохранять, чтобы (1) на дашборде был раздел «Отчёты ежедневные» с историей и
(2) агент учился на накопленном — вечерний вывод дня читает прошлые выводы и
сверяет с ними гипотезы, а не начинает каждый вечер с чистого листа.

Хранилище — таймлайн служебной сделки Битрикса 3627 (как журнал расхода и
журнал обучения): контейнеры агента общей памяти не имеют, Битрикс есть у
всех и переживает передеплой. Одна запись = один отчёт:

    ОТЧЁТ|<unix>|<ГГГГ-ММ-ДД>
    <текст отчёта целиком, как ушёл в Телеграм>

Повтор за ту же дату (перезапуск вечера) заменяет прежнюю запись.
"""

from __future__ import annotations

import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from интеграции.bitrix import Bitrix  # noqa: E402

МСК = timezone(timedelta(hours=3))
СДЕЛКА_АРХИВА = os.environ.get("СДЕЛКА_АРХИВА_ОТЧЁТОВ", "3627")
МЕТКА = "ОТЧЁТ|"


def _дата(unix: float) -> str:
    return datetime.fromtimestamp(unix, МСК).strftime("%Y-%m-%d")


def запись(текст: str, когда: float | None = None) -> str:
    момент = когда or time.time()
    return f"{МЕТКА}{int(момент)}|{_дата(момент)}\n{(текст or '').strip()}"


_ЭМОДЗИ_КОД = re.compile(r":([0-9a-f]{6,16}):")


def _раскодировать(текст: str) -> str:
    """Битрикс хранит эмодзи в комментариях как :f09f938c: — возвращаем символ."""
    def _з(м: re.Match) -> str:
        try:
            return bytes.fromhex(м.group(1)).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return м.group(0)
    return _ЭМОДЗИ_КОД.sub(_з, текст or "")


def разобрать(комментарий: str) -> dict | None:
    т = _раскодировать((комментарий or "").lstrip())
    if not т.startswith(МЕТКА):
        return None
    шапка, _, тело = т.partition("\n")
    поля = шапка.split("|")
    if len(поля) < 3:
        return None
    try:
        return {"когда": float(поля[1]), "дата": поля[2].strip(), "текст": тело.strip()}
    except ValueError:
        return None


def _комментарии(b: Bitrix, предел: int = 400) -> list[dict]:
    out: list[dict] = []
    старт = 0
    while len(out) < предел:
        порция = b.call(
            "crm.timeline.comment.list",
            {"filter": {"ENTITY_ID": int(СДЕЛКА_АРХИВА), "ENTITY_TYPE": "deal"},
             "order": {"ID": "DESC"}, "start": старт},
        ) or []
        out.extend(порция)
        if len(порция) < 50:
            break
        старт += 50
    return out


def сохранить(текст: str, когда: float | None = None) -> bool:
    """Положить отчёт в архив. Сбой не роняет отправку — только stderr."""
    if not (текст or "").strip():
        return False
    b = None
    try:
        b = Bitrix()
        дата = _дата(когда or time.time())
        for к in _комментарии(b, предел=100):
            з = разобрать(к.get("COMMENT") or "")
            if з and з["дата"] == дата:
                try:
                    b.call("crm.timeline.comment.delete", {"id": к.get("ID")})
                except Exception:  # noqa: BLE001
                    pass
        b.timeline_comment_add(СДЕЛКА_АРХИВА, запись(текст, когда))
        return True
    except Exception as ошибка:  # noqa: BLE001
        print(f"[архив отчётов] не сохранён: {type(ошибка).__name__}: {ошибка}", file=sys.stderr)
        return False
    finally:
        if b is not None:
            try:
                b.close()
            except Exception:  # noqa: BLE001
                pass


def прочитать(дней: int = 60) -> list[dict]:
    """Отчёты за период, новые первыми."""
    порог = time.time() - дней * 86400
    b = None
    try:
        b = Bitrix()
        отчёты = [
            з for к in _комментарии(b)
            if (з := разобрать(к.get("COMMENT") or "")) and з["когда"] >= порог
        ]
    except Exception as ошибка:  # noqa: BLE001
        print(f"[архив отчётов] не прочитан: {type(ошибка).__name__}: {ошибка}", file=sys.stderr)
        return []
    finally:
        if b is not None:
            try:
                b.close()
            except Exception:  # noqa: BLE001
                pass
    return sorted(отчёты, key=lambda з: з["когда"], reverse=True)


_ВЫВОД = re.compile(r"##\s*📌\s*Вывод дня.*?(?=\n##\s|\n---|\Z)", re.S)


def вывод_из_отчёта(текст: str) -> str:
    """Раздел «Вывод дня» из текста отчёта — без цифр и списков сверху."""
    м = _ВЫВОД.search(текст or "")
    return м.group(0).strip() if м else ""


def прошлые_выводы(сколько: int = 2, предел_знаков: int = 2500) -> str:
    """Выводы последних дней — для вечернего разбора (экономно: по умолчанию два).

    Так гипотеза, поставленная позавчера, встречается с фактами сегодня, а
    «завтра меняю» вчерашнего вечера можно проверить, изменил ли.
    """
    куски = []
    for з in прочитать(дней=14)[:сколько]:
        в = вывод_из_отчёта(з["текст"])
        if в:
            д = f"{з['дата'][8:10]}.{з['дата'][5:7]}"
            куски.append(f"— {д} —\n{в[:предел_знаков]}")
    return "\n\n".join(куски)
