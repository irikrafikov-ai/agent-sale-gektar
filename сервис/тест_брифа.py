"""Бриф должен СОБИРАТЬСЯ, а не только компилироваться.

25.08 в 17:26 агент упал на живом клиенте Долины: в бриф добавили пример
фильтра Битрикса с фигурными скобками, а бриф — f-строка. Python принял
{"PHONE_NUMBER": ...} за спецификатор формата:

    ValueError: Invalid format specifier ... for object of type 'str'

Файл при этом компилировался и импортировался без единой жалобы: ошибка
живёт внутри f-строки и всплывает только в момент сборки. Прежние тесты
проверяли импорт — и пропустили поломку, которая остановила ответы КЛИЕНТАМ
в обоих кабинетах.

Этот тест собирает все четыре брифа по-настоящему. Он ловит не только скобки:
любую опечатку в подстановке, пропавший ключ кабинета, неверное имя поля.

    python сервис/тест_брифа.py
"""

import sys
import types
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent
sys.path.insert(0, str(КОРЕНЬ))

# Заглушки: проверяем сборку текста, а не сеть и не SDK.
_sdk = types.ModuleType("claude_agent_sdk")
_sdk.ClaudeAgentOptions = object
_sdk.query = lambda **k: None
sys.modules.setdefault("claude_agent_sdk", _sdk)

for имя in ("интеграции", "интеграции.avito", "интеграции.bitrix", "интеграции.telegram"):
    sys.modules.setdefault(имя, types.ModuleType(имя))
sys.modules["интеграции.avito"].Avito = object
sys.modules["интеграции.avito"].AvitoError = Exception
sys.modules["интеграции.bitrix"].Bitrix = object
sys.modules["интеграции.telegram"].Telegram = object

_инстр = types.ModuleType("инструменты")
_инстр.сервер = None
_инстр.ИМЕНА = []
_инстр.отправленные = lambda: []
sys.modules.setdefault("инструменты", _инстр)

import кабинеты  # noqa: E402
import прогон  # noqa: E402

провалов = 0


def проверь(что, условие):
    global провалов
    провалов += not условие
    print(f"  {'✅' if условие else '❌'} {что}")


for ключ in кабинеты.КАБИНЕТЫ:
    каб = кабинеты.кабинет(ключ)
    print(f"\n{каб['название']}")

    try:
        чат = прогон.задание_чат("u2i-TEST", "клиент написал сообщение", "база ок", каб)
        проверь(f"бриф чата собран ({len(чат)} символов)", len(чат) > 1000)
        проверь("chat_id подставлен", "u2i-TEST" in чат)
        проверь("повод подставлен", "клиент написал сообщение" in чат)
    except Exception as ошибка:  # noqa: BLE001
        проверь(f"бриф чата собран — УПАЛ: {type(ошибка).__name__}: {ошибка}", False)

    try:
        полный = прогон.задание("вечер", "база ок", каб)
        проверь(f"бриф прогона собран ({len(полный)} символов)", len(полный) > 500)
        проверь("вид прогона подставлен", "вечер" in полный)
    except Exception as ошибка:  # noqa: BLE001
        проверь(f"бриф прогона собран — УПАЛ: {type(ошибка).__name__}: {ошибка}", False)

print("\n" + ("ВСЁ ЗЕЛЁНОЕ" if not провалов else f"ПРОВАЛОВ: {провалов}"))
sys.exit(1 if провалов else 0)
