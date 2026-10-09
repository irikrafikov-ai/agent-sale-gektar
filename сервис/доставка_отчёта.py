"""Durable archive before delivery. Retry never runs sales or reflection."""
from datetime import date, datetime, time, timedelta, timezone
import os
from pathlib import Path

from report_runtime.outbox import Outbox, ReportKey
from report_runtime.telegram_transport import TelegramTransport

МСК = timezone(timedelta(hours=3))


def хранилище():
    root = os.environ.get("SALES_RUNTIME_DIR")
    if not root or not Path(root).is_absolute():
        raise RuntimeError("persistent_sales_report_runtime_required")
    return Outbox(Path(root) / "report-outbox.sqlite3")


def ключ(day):
    return ReportKey("sales", day, "daily")


def доставить(day, *, box=None, transport=None, archive=None, retry_rejected=False):
    box = box or хранилище()
    key = ключ(day)
    saved = box.get(key)
    if saved["status"] in {"sent", "needs_review", "sending"}:
        if saved["status"] == "sending":
            box.recover_inflight()
        return box.get(key)
    if archive is None:
        import архив_отчётов
        archive = архив_отчётов.сохранить
    # Original report day, even when the delivery retry is tomorrow.
    moment = datetime.combine(date.fromisoformat(day), time(18, 30), tzinfo=МСК).timestamp()
    if not archive(saved["payload"]["text"], когда=moment):
        return dict(saved, crm_archive_pending=True)
    if saved["status"] == "rejected":
        if not retry_rejected:
            return saved
        box.retry_rejected(key)
    transport = transport or TelegramTransport(os.environ["TELEGRAM_BOT_TOKEN"],
                                               os.environ["TELEGRAM_CHAT_ID"], destination="sales")
    return box.deliver(key, transport)


def сохранить_и_доставить(text, *, day=None, reflection=None, box=None, transport=None, archive=None):
    day = day or datetime.now(МСК).date().isoformat()
    box = box or хранилище()
    box.archive(ключ(day), destination="sales", text=text, reflection=reflection)
    return доставить(day, box=box, transport=transport, archive=archive)


def уже_сохранён(day):
    if not os.environ.get("SALES_RUNTIME_DIR"):
        return False
    try:
        хранилище().get(ключ(day))
        return True
    except KeyError:
        return False


def main(argv):
    day = argv[0] if argv else datetime.now(МСК).date().isoformat()
    result = доставить(day, retry_rejected="--retry-rejected" in argv[1:])
    print("[доставка отчёта] " + result["status"] + ("; CRM archive pending" if result.get("crm_archive_pending") else ""))
    return 0 if result["status"] == "sent" else 2
