"""Точка входа сервиса: один прогон агента.

Запускается по расписанию Railway. Порядок:
  1. подтянуть свежую базу знаний из отдельного репозитория
  2. отдать агенту регламент и дать ему отработать цикл
  3. отправить отчёт в Телеграм и записать журнал

База знаний тянется перед каждым прогоном намеренно: Ирик правит её
напрямую, и копия в образе устарела бы в первый же день.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, query

sys.path.insert(0, str(Path(__file__).parent))

import инструменты  # noqa: E402
from интеграции.telegram import Telegram  # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent
БАЗА_ЗНАНИЙ = Path(os.environ.get("KB_PATH", "/opt/база-знаний"))
МСК = timezone(timedelta(hours=3))


def обновить_базу_знаний() -> str:
    """Клонирует или обновляет репозиторий базы знаний. Возвращает статус для отчёта."""
    url = os.environ.get("KB_REPO_URL")
    if not url:
        return "⚠️ KB_REPO_URL не задан — база знаний недоступна"

    token = os.environ.get("KB_TOKEN")
    if token and url.startswith("https://"):
        url = url.replace("https://", f"https://x-access-token:{token}@", 1)

    try:
        if (БАЗА_ЗНАНИЙ / ".git").exists():
            subprocess.run(
                ["git", "-C", str(БАЗА_ЗНАНИЙ), "fetch", "--quiet", "origin"], check=True, timeout=120
            )
            subprocess.run(
                ["git", "-C", str(БАЗА_ЗНАНИЙ), "reset", "--hard", "--quiet", "origin/HEAD"],
                check=True,
                timeout=120,
            )
        else:
            БАЗА_ЗНАНИЙ.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "clone", "--quiet", "--depth", "1", url, str(БАЗА_ЗНАНИЙ)],
                check=True,
                timeout=180,
            )
        sha = subprocess.run(
            ["git", "-C", str(БАЗА_ЗНАНИЙ), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
        файлов = len(list(БАЗА_ЗНАНИЙ.glob("*.md")))
        return f"база знаний {sha}, файлов: {файлов}"
    except subprocess.CalledProcessError as e:
        return f"⚠️ база знаний не обновилась: {e}"
    except subprocess.TimeoutExpired:
        return "⚠️ база знаний не обновилась: таймаут"


def задание(вид: str, статус_базы: str) -> str:
    сейчас = datetime.now(МСК)
    режим = os.environ.get("AGENT_MODE", "send")

    предупреждение = (
        ""
        if режим == "send"
        else "\n⚠️ РЕЖИМ READ-ONLY: инструмент отправки клиентам заблокирован. "
        "Битрикс ведёшь как обычно, тексты сообщений включи в отчёт.\n"
    )

    return f"""Сейчас {сейчас.strftime('%d.%m.%Y %H:%M')} МСК, прогон: {вид}.
Ты работаешь автономно на сервере. Показывать тексты некому — отправляй сам,
в рамках регламента. Спорные случаи — стоп-краны, алерт через telegram_alert.
{предупреждение}
Состояние: {статус_базы}

ПОРЯДОК:
1. Прочитай {КОРЕНЬ}/AGENT.md — конституция, она отменяет всё остальное при конфликте.
2. Прочитай {КОРЕНЬ}/регламент/01-рабочий-цикл.md и иди по нему.
3. Продуктовую базу знаний читай из {БАЗА_ЗНАНИЙ} — там актуальные цены,
   скрипты, возражения, участки. Копий не делай.
4. Рабочая память — {КОРЕНЬ}/данные/реестр-клиентов.md, обнови её в конце.
5. Журнал прогона запиши в {КОРЕНЬ}/данные/журнал/{сейчас:%Y-%m-%d}-{вид}.md

ЗАВЕРШЕНИЕ: последним сообщением выдай отчёт по формату
{КОРЕНЬ}/регламент/04-отчёт.md. Он уйдёт Ирику в Телеграм как есть —
пиши его для человека, а не как лог. Раздел «Требует вас» ставь первым.
"""


async def прогон(вид: str) -> str:
    статус_базы = обновить_базу_знаний()

    options = ClaudeAgentOptions(
        model=os.environ.get("AGENT_MODEL", "claude-opus-5"),
        cwd=str(КОРЕНЬ),
        mcp_servers={"gektar": инструменты.сервер},
        allowed_tools=["Read", "Glob", "Grep", "Write", "Edit"] + инструменты.ИМЕНА,
        permission_mode="bypassPermissions",
        max_turns=int(os.environ.get("AGENT_MAX_TURNS", "200")),
        system_prompt={"type": "preset", "preset": "claude_code"},
    )

    последний = ""
    async for message in query(prompt=задание(вид, статус_базы), options=options):
        for block in getattr(message, "content", []) or []:
            if getattr(block, "type", None) == "text" and block.text.strip():
                последний = block.text
    return последний


ОБЯЗАТЕЛЬНЫЕ = {
    "ANTHROPIC_API_KEY": "ключ Anthropic (console.anthropic.com → API Keys)",
    "AVITO_CLIENT_ID": "Авито → Настройки → Клиенты и приложения",
    "AVITO_CLIENT_SECRET": "там же, рядом с client_id",
    "BITRIX_WEBHOOK": "Битрикс → Разработчикам → Входящий вебхук",
    "TELEGRAM_BOT_TOKEN": "@BotFather → /newbot",
    "TELEGRAM_CHAT_ID": "@userinfobot",
}


def проверить_переменные() -> list[str]:
    """Возвращает список недостающих переменных — все сразу, а не первую попавшуюся.

    Без этой проверки процесс падал сырым KeyError на первой же отсутствующей
    переменной: чинишь одну, деплоишь, узнаёшь про следующую.
    """
    return [имя for имя in ОБЯЗАТЕЛЬНЫЕ if not os.environ.get(имя)]


def main() -> int:
    вид = sys.argv[1] if len(sys.argv) > 1 else "вечер"

    недостаёт = проверить_переменные()
    if недостаёт:
        print("НЕ ХВАТАЕТ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ — прогон не начат.\n", file=sys.stderr)
        for имя in недостаёт:
            print(f"  {имя:22} — {ОБЯЗАТЕЛЬНЫЕ[имя]}", file=sys.stderr)
        print(
            "\nЗадайте их в Railway → сервис → Variables. Шаблон: сервис/.env.example",
            file=sys.stderr,
        )
        # Если Телеграм настроен, а отвалилось что-то другое — сообщаем туда:
        # иначе про нерабочий прогон никто не узнает до вечернего отчёта.
        if not {"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"} & set(недостаёт):
            try:
                Telegram().send(
                    "Прогон не начат: не заданы переменные окружения — "
                    + ", ".join(недостаёт),
                    alert=True,
                )
            except Exception:
                pass  # алерт не критичен, сообщение уже в логах Railway
        return 1

    telegram = Telegram()

    try:
        отчёт = asyncio.run(прогон(вид))
    except Exception as e:
        # Алерт отправляем best-effort. Если Телеграм тоже недоступен, его
        # ошибка не должна подменять настоящую причину падения: иначе в логах
        # видно «TelegramError», а из-за чего упал прогон — уже нет.
        try:
            telegram.send(f"Прогон *{вид}* упал: `{type(e).__name__}: {e}`", alert=True)
        except Exception as ошибка_алерта:
            print(f"алерт в Телеграм не ушёл: {ошибка_алерта}", file=sys.stderr)
        raise

    отправлено = инструменты.отправленные()
    подпись = f"\n\n---\n_Прогон {вид}, отправлено сообщений: {len(отправлено)}_"

    telegram.send((отчёт or "Прогон завершён, но отчёт пуст — проверьте логи.") + подпись)
    telegram.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
