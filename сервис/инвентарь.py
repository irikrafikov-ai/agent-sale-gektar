"""Живая шахматка и проверка конкретных предложений; без копий базы на диске."""
from __future__ import annotations

import csv
import io
import re
import time
from urllib.request import urlopen

_кэш: dict[str, tuple[float, dict[int, dict]]] = {}
TTL = 60
ДОСТУПНЫ = {"свободен", "акция"}
СТАТУСЫ = ДОСТУПНЫ | {"продан", "забронирован"}


class ОшибкаШахматки(RuntimeError):
    pass


def разобрать_csv(текст: str) -> dict[int, dict]:
    reader = csv.DictReader(io.StringIO(текст.lstrip("\ufeff")))
    if not {"Участок", "Статус", "Площадь", "Цена"}.issubset(reader.fieldnames or []):
        raise ОшибкаШахматки("CSV не содержит обязательные колонки")
    участки = {}
    for row in reader:
        сырой = (row.get("Участок") or "").strip()
        if not сырой:
            continue
        try:
            номер = int(сырой)
        except ValueError as exc:
            raise ОшибкаШахматки("Некорректный номер участка") from exc
        статус = (row.get("Статус") or "").strip()
        if номер in участки or статус.lower() not in СТАТУСЫ:
            raise ОшибкаШахматки("Дубликат участка или неизвестный статус")
        участки[номер] = {"номер": номер, "статус": статус,
                          "площадь": row["Площадь"], "цена": row["Цена"],
                          "доступен": статус.lower() in ДОСТУПНЫ}
    if not участки:
        raise ОшибкаШахматки("Пустая шахматка")
    return участки


def загрузить(каб: dict, *, свежая: bool = False) -> dict[int, dict]:
    url = каб.get("шахматка")
    if not url:
        raise ОшибкаШахматки("Для проекта не настроена живая шахматка")
    cached = _кэш.get(url)
    if not свежая and cached and time.monotonic() - cached[0] < TTL:
        return cached[1]
    try:
        with urlopen(url, timeout=15) as response:
            data = response.read(1_000_001)
        if len(data) > 1_000_000:
            raise ОшибкаШахматки("CSV превышает допустимый размер")
        участки = разобрать_csv(data.decode("utf-8-sig"))
    except Exception as exc:
        # Устаревший кэш не разрешает продажу после ошибки свежего запроса.
        raise ОшибкаШахматки("Живая шахматка недоступна или некорректна") from exc
    _кэш[url] = (time.monotonic(), участки)
    return участки


_НОМЕРА = re.compile(
    r"(?P<prefix>№\s*|\bучаст(?:ок|ка|ки|ков|ку|ке|ком|кам|ками|ках)\s*(?:с[о]?\s+)?(?:№\s*|номер(?:а|ом)?\s*)?)"
    r"(?P<numbers>\d{1,4}(?:\s*(?:,|/|[-–—]|\bи\b|\bили\b|\bдо\b|\bпо\b)\s*(?:№\s*)?\d{1,4})*)(?!\d)", re.I)
_ЕДИНИЦЫ = re.compile(r"^\s*(?:[,.]\d+\s*)?(?:сот|га\b|гектар|руб|₽|тыс|млн|кв\.?\s*м)", re.I)
_ПРЕДЛОЖЕНИЕ = re.compile(
    r"(?<!не )\b(?:свобод\w*|доступ\w*|предлага\w*|предлож\w*|подойд\w*|подобрал\w*|"
    r"берите|выбира\w*|забронировать|бронируем|оформим|покажу|посмотреть|по акции)\b", re.I)
_ПРОДАН = re.compile(r"(?<!не )\bпродан\w*\b", re.I)
_БРОНЬ = re.compile(r"\b(?:забронирован\w*|на брон[ье])\b", re.I)
_НЕДОСТУПЕН = re.compile(r"\b(?:недоступ\w*|не доступ\w*|не свобод\w*|занят\w*|не предлага\w*|не прода[её]тся)\b", re.I)


def упоминания(текст: str) -> list[tuple[list[int], str]]:
    """Номер участка, а не цена/площадь; контекст ограничен своей репликой."""
    out = []
    for clause in re.split(r"[!?;\n]|\.(?!\d)", текст):
        matches = list(_НОМЕРА.finditer(clause))
        for pos, match in enumerate(matches):
            if "№" not in match.group("prefix") and _ЕДИНИЦЫ.match(clause[match.end():]):
                continue
            # Правый контекст относится к этому номеру, не к следующему.
            end = matches[pos + 1].start() if pos + 1 < len(matches) else len(clause)
            right = clause[match.end():end]
            if pos + 1 < len(matches) and "," in right:
                right = right.rsplit(",", 1)[0]
            left = clause[:match.start()].rsplit(",", 1)[-1]
            # В конструкции «№3 продан, №4 свободен» не переносим «продан»
            # с предыдущего участка на следующий.
            if pos:
                left = clause[matches[pos - 1].end():match.start()].rsplit(",", 1)[-1]
            number_text = match.group("numbers")
            numbers = {int(n) for n in re.findall(r"\d+", number_text)}
            for start, finish in re.findall(r"(\d+)\s*(?:[-–—]|\bдо\b|\bпо\b)\s*(?:№\s*)?(\d+)", number_text):
                numbers.update(range(min(int(start), int(finish)), max(int(start), int(finish)) + 1))
            out.append((sorted(numbers), left + " " + right))
    return out


def проверить_предложение(текст: str, каб: dict) -> str | None:
    mentions = упоминания(текст)
    if not mentions:
        return None
    try:
        участки = загрузить(каб, свежая=True)
    except ОшибкаШахматки:
        # Уведомление о недоступности не является предложением купить.
        if all((_ПРОДАН.search(ctx) or _БРОНЬ.search(ctx) or _НЕДОСТУПЕН.search(ctx))
               and not _ПРЕДЛОЖЕНИЕ.search(_НЕДОСТУПЕН.sub("", ctx)) for _, ctx in mentions):
            return None
        return "Живая шахматка недоступна. Конкретные участки не предлагай до повторной проверки plot_inventory."
    for numbers, context in mentions:
        for number in numbers:
            row = участки.get(number)
            if row is None:
                return f"Участок №{number} не найден в живой шахматке; наличие не подтверждено."
            if row["доступен"]:
                continue
            status = row["статус"].lower()
            truthful = bool(_НЕДОСТУПЕН.search(context) or
                            (status == "продан" and _ПРОДАН.search(context)) or
                            (status == "забронирован" and _БРОНЬ.search(context)))
            positive = _ПРЕДЛОЖЕНИЕ.search(_НЕДОСТУПЕН.sub("", context))
            if not truthful or positive:
                return (f"Участок №{number}: {row['статус']} по живой шахматке. "
                        "Нельзя предлагать его как доступный; выбери свободный/акционный через plot_inventory "
                        "либо честно сообщи текущий статус.")
    return None
