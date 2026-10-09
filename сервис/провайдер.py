"""Explicit provider selection. A future cutover is not an error fallback."""
from datetime import date, datetime, timedelta, timezone
import os

МСК = timezone(timedelta(hours=3))
MIN_CODEX_DATE = date(2026, 10, 7)


def выбран(now=None, env=None):
    env = os.environ if env is None else env
    provider = env.get("AGENT_SDK_PROVIDER", "claude").strip().lower()
    if provider not in {"claude", "openai", "codex"}:
        raise RuntimeError("unknown_agent_provider")
    if provider != "codex":
        return provider
    try:
        cutover = date.fromisoformat(env["CODEX_START_DATE"])
        if cutover < MIN_CODEX_DATE:
            raise ValueError()
    except (KeyError, ValueError):
        raise RuntimeError("invalid_codex_start_date") from None
    moment = now or datetime.now(МСК)
    if moment.tzinfo is None:
        raise RuntimeError("timezone_required")
    if moment.astimezone(МСК).date() < cutover:
        legacy = env.get("CODEX_LEGACY_PROVIDER", "claude").strip().lower()
        if legacy not in {"claude", "openai"}:
            raise RuntimeError("invalid_legacy_provider")
        return legacy
    return "codex"


def codex_model(kind="run", *, simple=False, env=None):
    env = os.environ if env is None else env
    key = "CODEX_MODEL_ВЫВОД" if kind == "review" else "CODEX_MODEL_ПРОСТОЙ" if simple else "CODEX_MODEL_ЧАТ" if kind == "chat" else "CODEX_MODEL"
    # Omission deliberately selects the backend's explicitly configured model.
    return env.get(key) or env.get("CODEX_MODEL") or None
