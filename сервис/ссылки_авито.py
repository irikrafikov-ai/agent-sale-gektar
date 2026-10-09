"""Ссылки на собеседника из метаданных Авито, без угадывания по имени/ID."""

from urllib.parse import urlsplit, urlunsplit


def профиль_клиента(чат: dict, наш_id: object) -> str:
    """Только единственный собеседник, не профиль нашего кабинета."""
    if not str(наш_id).isdecimal() or int(str(наш_id)) <= 0:
        return ""
    users = чат.get("users") or []
    if not isinstance(users, list):
        return ""
    others = [u for u in users if isinstance(u, dict)
              and str(u.get("id")).isdecimal() and int(str(u["id"])) > 0
              and int(str(u["id"])) != int(str(наш_id))]
    if len(others) != 1:
        return ""
    user = others[0]
    profile = user.get("public_user_profile") or {}
    if not isinstance(profile, dict):
        return ""
    if profile.get("user_id") is not None and str(profile["user_id"]) != str(user["id"]):
        return ""
    url = profile.get("url")
    if not isinstance(url, str) or any(c.isspace() or c in "()<>\\" for c in url):
        return ""
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname not in ("avito.ru", "www.avito.ru", "m.avito.ru")
                or parsed.username or parsed.password or parsed.port is not None
                or not parsed.path.startswith(("/user/", "/brands/"))):
            return ""
    except ValueError:
        return ""
    # iid/src/page_from — параметры объявления, для профиля не нужны.
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def строка_профиля(url: str) -> str:
    return f"[Профиль Авито]({url})" if url else "профиль Авито не получен"


def профиль_для_алерта(клиент: object, chat_id: str) -> str:
    """Сбой чтения ссылки не отменяет уведомление о тёплом лиде."""
    try:
        url = профиль_клиента(клиент.chat_info(chat_id), клиент.user_id)
    except Exception:  # ошибки/секреты интеграции в текст уведомления не попадают
        url = ""
    return строка_профиля(url)
