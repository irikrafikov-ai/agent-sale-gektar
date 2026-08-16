"""Диспетчер: один образ — три роли.

Railway запускает из одного репозитория три сервиса, и различаются они ровно
первым аргументом:

    утро | вечер     → прогон по расписанию (cron-сервис)
    чат <chat_id>    → разбор одного чата (его запускает вебхук)
    вебхук           → постоянно работающий HTTP-эндпоинт

Диспетчер появился, чтобы к вебхуку не пришлось трогать ENTRYPOINT образа:
cron-сервисы продолжают передавать «утро» и «вечер» ровно как раньше, а новый
веб-сервис передаёт «вебхук». Ни одну работающую настройку менять не нужно.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


def main() -> int:
    роль = sys.argv[1] if len(sys.argv) > 1 else "вечер"

    if роль == "вебхук":
        import uvicorn

        import вебхук

        порт = int(os.environ.get("PORT", "8080"))
        вебхук.лог(f"вебхук поднимается на порту {порт}, режим {вебхук.РЕЖИМ}")
        if not вебхук.СЕКРЕТ:
            вебхук.лог("⚠️ WEBHOOK_SECRET не задан — эндпоинт открыт всем, кто угадает путь")
        uvicorn.run(вебхук.app, host="0.0.0.0", port=порт, log_level="info")
        return 0

    import прогон

    return прогон.main()


if __name__ == "__main__":
    raise SystemExit(main())
