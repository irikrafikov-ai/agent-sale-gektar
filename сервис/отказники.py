"""Реестр людей, которые просили больше не писать — по user_id Авито.

Зачем отдельный реестр. Запрет «просил не писать» держался на привязке к
ЧАТУ: карточка с пометкой НЕ ПИСАТЬ ссылается на chat_id, и стоп-кран
читает историю именно этого чата. Но у человека бывает несколько чатов —
по разным объявлениям, — и во второй такой чат мы продолжаем писать.

29.09.2026 так и вышло: Андрей Гореликов (Avito id 110670914) получил
пометку НЕ ПИСАТЬ ещё 12.08 и 06.09 сказал «Не нужно больше информации!»,
а вечером 29.09 ему ушло очередное сообщение — во второй его чат. Старый
чат Авито уже не отдаёт в списке, поэтому и сверка «вторая ветка того же
человека» его не увидела.

Ник в Авито человек меняет, id — нет. Поэтому запрет живёт по user_id.

Хранилище — таймлайн служебной сделки (как журнал расхода и обучения):
контейнеры агента общей памяти не имеют, Битрикс есть у всех.

    НЕПИСАТЬ|<unix>|<user_id>|<имя>|<причина>
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from интеграции.bitrix import Bitrix  # noqa: E402

СДЕЛКА_РЕЕСТРА = os.environ.get("СДЕЛКА_РЕЕСТРА_ОТКАЗНИКОВ", "4209")
МЕТКА = "НЕПИСАТЬ|"
ЖИЗНЬ_КЭША = 600  # секунд: реестр меняется редко, а читается в каждой отправке

_кэш: dict[str, object] = {"когда": 0.0, "люди": {}}


def _чисто(т: str) -> str:
    return " ".join(str(т or "").replace("|", "/").split())


def разобрать(комментарий: str) -> dict | None:
    т = (комментарий or "").lstrip()
    if not т.startswith(МЕТКА):
        return None
    поля = т.split("\n")[0].split("|")
    if len(поля) < 3:
        return None
    try:
        return {"когда": float(поля[1]), "user_id": поля[2].strip(),
                "имя": поля[3].strip() if len(поля) > 3 else "",
                "причина": поля[4].strip() if len(поля) > 4 else ""}
    except ValueError:
        return None


def _комментарии(b: Bitrix, предел: int = 600) -> list[dict]:
    out: list[dict] = []
    старт = 0
    while len(out) < предел:
        порция = b.call(
            "crm.timeline.comment.list",
            {"filter": {"ENTITY_ID": int(СДЕЛКА_РЕЕСТРА), "ENTITY_TYPE": "deal"},
             "order": {"ID": "DESC"}, "start": старт},
        ) or []
        out.extend(порция)
        if len(порция) < 50:
            break
        старт += 50
    return out


def список(обновить: bool = False) -> dict[str, dict]:
    """user_id → запись. Кэш на 10 минут: реестр читается в каждой отправке."""
    if not СДЕЛКА_РЕЕСТРА:
        return {}
    свежесть = time.time() - float(_кэш["когда"])  # type: ignore[arg-type]
    if not обновить and свежесть < ЖИЗНЬ_КЭША and _кэш["люди"]:
        return _кэш["люди"]  # type: ignore[return-value]
    люди: dict[str, dict] = {}
    b = None
    try:
        b = Bitrix()
        for к in _комментарии(b):
            з = разобрать(к.get("COMMENT") or "")
            if з:
                люди.setdefault(з["user_id"], з)
    except Exception as ошибка:  # noqa: BLE001 — реестр не важнее отправки
        print(f"[отказники] реестр не прочитан: {type(ошибка).__name__}: {ошибка}", file=sys.stderr)
        return _кэш["люди"]  # type: ignore[return-value]
    finally:
        if b is not None:
            try:
                b.close()
            except Exception:  # noqa: BLE001
                pass
    _кэш["когда"], _кэш["люди"] = time.time(), люди
    return люди


def запрещён(user_id: str | int | None) -> dict | None:
    """Этот человек просил не писать — в любом своём чате."""
    if not user_id:
        return None
    return список().get(str(user_id))


def добавить(user_id: str | int, имя: str = "", причина: str = "") -> bool:
    """Запомнить отказ по человеку. Повтор не страшен — читаем по первому."""
    if not СДЕЛКА_РЕЕСТРА or not user_id:
        return False
    if запрещён(user_id):
        return True
    b = None
    try:
        b = Bitrix()
        b.timeline_comment_add(
            СДЕЛКА_РЕЕСТРА,
            f"{МЕТКА}{int(time.time())}|{user_id}|{_чисто(имя)}|{_чисто(причина)}",
        )
    except Exception as ошибка:  # noqa: BLE001
        print(f"[отказники] не записан {user_id}: {type(ошибка).__name__}: {ошибка}", file=sys.stderr)
        return False
    finally:
        if b is not None:
            try:
                b.close()
            except Exception:  # noqa: BLE001
                pass
    список(обновить=True)
    return True


_НАШИ = ("0", "1")


def собеседник_из_истории(сообщения: list[dict], наш_id) -> str | None:
    """user_id клиента из истории чата — без лишнего запроса к Авито.

    В каждом входящем сообщении author_id и есть id человека; служебные
    сообщения площадки приходят от 0 и 1, их пропускаем.
    """
    for м in сообщения or []:
        автор = str(м.get("author_id") or "")
        if автор and автор != str(наш_id) and автор not in _НАШИ:
            return автор
    return None
