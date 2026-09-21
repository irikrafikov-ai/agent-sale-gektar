"""Дашборд агента продаж — отдельный сайт (требование Ирика 21.09.2026).

Маленький веб-сервер: одна страница со статистикой и графиками, данные из
Битрикса через сервис/дашборд_данные.py. Модель не вызывается вовсе —
токенов дашборд не тратит.

Вход по паролю: переменная DASHBOARD_PASSWORD. Пароль подписывается в
cookie (HMAC), сам пароль в cookie не кладётся. Без переменной сервер
отказывает всем — публичным дашборд с клиентскими именами быть не должен.

Запуск (Railway, тот же образ, что у агента):
    python сервис/дашборд.py           # слушает $PORT
"""

from __future__ import annotations

import hmac
import hashlib
import os
import sys
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

sys.path.insert(0, str(Path(__file__).parent))

import дашборд_данные as данные  # noqa: E402

app = FastAPI(title="Дашборд агента продаж")

КУКА = "dash"
ПАРОЛЬ = os.environ.get("DASHBOARD_PASSWORD", "")
СТРАНИЦА = Path(__file__).parent / "дашборд.html"


def _подпись() -> str:
    return hmac.new(ПАРОЛЬ.encode(), b"dashboard-agent-sale", hashlib.sha256).hexdigest()


def впущен(request: Request) -> bool:
    if not ПАРОЛЬ:
        return False
    кука = request.cookies.get(КУКА) or ""
    return hmac.compare_digest(кука, _подпись())


ФОРМА_ВХОДА = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Дашборд агента продаж — вход</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#1e1c1a;color:#f3ead8;font:15px/1.4 -apple-system,Inter,system-ui,sans-serif}
form{background:#2a2725;padding:28px 32px;border-radius:14px;width:min(360px,90vw);box-shadow:0 20px 60px rgba(0,0,0,.4)}
h1{font-size:18px;margin:0 0 6px}p{margin:0 0 18px;color:#bfb5a3;font-size:13px}
input{width:100%;box-sizing:border-box;padding:11px 12px;border-radius:9px;border:1px solid #4a443f;background:#1e1c1a;color:#f3ead8;font-size:15px}
button{margin-top:12px;width:100%;padding:11px;border:0;border-radius:9px;background:#e4b654;color:#1e1c1a;font-weight:600;font-size:15px;cursor:pointer}
.err{color:#e66767;font-size:13px;margin-top:10px}</style></head><body>
<form method="post" action="/войти"><h1>Агент по продажам на Авито</h1><p>Дашборд · Щёкинские берега</p>
<input type="password" name="пароль" placeholder="Пароль" autofocus autocomplete="current-password">
<button>Войти</button>__ОШИБКА__</form></body></html>"""


@app.get("/", response_class=HTMLResponse)
async def главная(request: Request) -> HTMLResponse:
    if not впущен(request):
        return HTMLResponse(ФОРМА_ВХОДА.replace("__ОШИБКА__", ""))
    return HTMLResponse(СТРАНИЦА.read_text(encoding="utf-8"))


@app.post("/войти")
async def войти(request: Request) -> Response:
    форма = await request.form()
    пароль = str(форма.get("пароль") or "")
    # compare_digest со строками не любит кириллицу — сравниваем байты
    if not ПАРОЛЬ or not hmac.compare_digest(пароль.encode(), ПАРОЛЬ.encode()):
        return HTMLResponse(ФОРМА_ВХОДА.replace("__ОШИБКА__", '<div class="err">Неверный пароль</div>'), status_code=401)
    ответ = RedirectResponse("/", status_code=303)
    ответ.set_cookie(КУКА, _подпись(), max_age=60 * 60 * 24 * 90, httponly=True, samesite="lax",
                     secure=bool(os.environ.get("RAILWAY_ENVIRONMENT")))
    return ответ


@app.get("/выйти")
async def выйти() -> Response:
    ответ = RedirectResponse("/", status_code=303)
    ответ.delete_cookie(КУКА)
    return ответ


@app.get("/api/данные")
def api_данные(request: Request, дней: int = 30) -> Response:
    # Обычная def, не async: FastAPI уведёт её в пул потоков, и долгая
    # сборка (если кэш ещё пуст) не заблокирует остальные запросы.
    if not впущен(request):
        return JSONResponse({"ошибка": "нет доступа"}, status_code=403)
    дней = min(данные.ПЕРИОДЫ, key=lambda п: abs(п - int(дней)))
    if not данные.в_кэше(дней):
        # Первая сборка идёт в фоне ~40 с — страница подождёт и переспросит.
        return JSONResponse({"строится": True}, status_code=202, headers={"Cache-Control": "no-store"})
    try:
        return JSONResponse(данные.собрать(дней), headers={"Cache-Control": "no-store"})
    except Exception as ошибка:  # noqa: BLE001 — страница должна показать причину, а не 500 без слов
        return JSONResponse({"ошибка": f"{type(ошибка).__name__}: {ошибка}"}, status_code=502)


import threading  # noqa: E402

threading.Thread(target=данные.обновлять_в_фоне, daemon=True, name="дашборд-кэш").start()


@app.get("/здоровье")
async def здоровье() -> dict:
    return {"ок": True}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
