"""Private, durable report archive and outbox; never performs network I/O itself.

Use a private runtime directory, e.g. /data/cloud-control/outbox.sqlite3, NOT Git.
Exclude *.sqlite3, *.sqlite3-*, and *.delivery.lock from repository/deploy input.
Tokens are supplied to the injected transport by its owner, never to this class.
SQLite/WAL and the delivery lock must reside on one persistent local filesystem.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
from typing import Protocol


class OutboxError(Exception):
    pass


class ImmutableReport(OutboxError):
    pass


class DeliveryBusy(OutboxError):
    pass


class RejectedDelivery(OutboxError):
    """Transport guarantees the destination did NOT accept this part.

    Timeouts, connection resets, 5xx and malformed responses are NOT rejections.
    An explicit retry_rejected() is required even for deterministic rejection.
    """


@dataclass(frozen=True)
class ReportKey:
    agent: str
    day: str
    report_type: str = "daily"

    def values(self) -> tuple[str, str, str]:
        if date.fromisoformat(self.day).isoformat() != self.day:
            raise ValueError("Day must be canonical YYYY-MM-DD")
        for value in (self.agent, self.report_type):
            if not isinstance(value, str) or not value.strip() or len(value) > 128 or any(ord(c) < 32 for c in value) or _TOKEN.search(value):
                raise ValueError("Invalid report identity")
        return self.agent, self.day, self.report_type


@dataclass(frozen=True)
class Receipt:
    destination: str
    message_id: str | int


class Transport(Protocol):
    def send(self, destination: str, text: str, *, idempotency_key: str) -> Receipt:
        """Return a checked destination/message ID, or raise RejectedDelivery.

        The key is informational for transports without server-side idempotency;
        it does NOT make an ambiguous Telegram send safe to retry.
        """
        ...


_TOKEN = re.compile(r"(?:https?://api\.telegram\.org/bot|\b\d{5,}:[A-Za-z0-9_-]{20,})")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
 agent TEXT NOT NULL, day TEXT NOT NULL, report_type TEXT NOT NULL,
 current_revision INTEGER NOT NULL, PRIMARY KEY(agent, day, report_type));
CREATE TABLE IF NOT EXISTS revisions (
 agent TEXT NOT NULL, day TEXT NOT NULL, report_type TEXT NOT NULL, revision INTEGER NOT NULL,
 destination TEXT NOT NULL, payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('ready','sending','needs_review','rejected','sent','superseded')),
 created_at TEXT NOT NULL, sent_at TEXT, error_type TEXT,
 memory_committed INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(agent, day, report_type, revision),
 FOREIGN KEY(agent,day,report_type) REFERENCES reports(agent,day,report_type));
CREATE TABLE IF NOT EXISTS chunks (
 agent TEXT NOT NULL, day TEXT NOT NULL, report_type TEXT NOT NULL, revision INTEGER NOT NULL,
 part INTEGER NOT NULL, text TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','sending','needs_review','rejected','sent')),
 destination TEXT NOT NULL, message_id TEXT,
 PRIMARY KEY(agent,day,report_type,revision,part), UNIQUE(destination,message_id),
 FOREIGN KEY(agent,day,report_type,revision) REFERENCES revisions(agent,day,report_type,revision));
CREATE TABLE IF NOT EXISTS memory (
 agent TEXT NOT NULL, name TEXT NOT NULL, value_json TEXT NOT NULL,
 PRIMARY KEY(agent,name));
CREATE TABLE IF NOT EXISTS memory_versions (
 agent TEXT NOT NULL, name TEXT NOT NULL, version_json TEXT NOT NULL,
 PRIMARY KEY(agent,name), FOREIGN KEY(agent,name) REFERENCES memory(agent,name));
CREATE TABLE IF NOT EXISTS memory_events (
 agent TEXT NOT NULL, day TEXT NOT NULL, report_type TEXT NOT NULL, revision INTEGER NOT NULL,
 updates_json TEXT NOT NULL, committed_at TEXT NOT NULL,
 PRIMARY KEY(agent,day,report_type,revision),
 FOREIGN KEY(agent,day,report_type,revision) REFERENCES revisions(agent,day,report_type,revision));
CREATE TRIGGER IF NOT EXISTS immutable_payload
 BEFORE UPDATE OF agent,day,report_type,revision,destination,payload_json,payload_hash ON revisions
 BEGIN SELECT RAISE(ABORT, 'immutable archived payload'); END;
CREATE TRIGGER IF NOT EXISTS immutable_chunk
 BEFORE UPDATE OF agent,day,report_type,revision,part,text,destination ON chunks
 BEGIN SELECT RAISE(ABORT, 'immutable archived chunk'); END;
"""
_WHERE = "agent=? AND day=? AND report_type=?"
_REV = _WHERE + " AND revision=?"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value) -> str:
    result = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if _TOKEN.search(result):
        raise ValueError("Telegram credentials must not be archived")
    return result


def _chunks(text: str, limit: int) -> list[str]:
    if not isinstance(text, str) or not text.strip() or not isinstance(limit, int) or isinstance(limit, bool) or not 2 <= limit <= 4096:
        raise ValueError("Nonempty text and UTF-16 chunk limit 2..4096 required")
    text.encode("utf-8")  # Reject unpaired surrogates before archiving.
    result, current, units = [], [], 0
    for char in text:
        size = 2 if ord(char) > 0xFFFF else 1
        if units + size > limit:
            result.append("".join(current))
            current, units = [], 0
        current.append(char)
        units += size
    result.append("".join(current))
    return result


class Outbox:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path).absolute()
        self._private_parent(self.path)
        self._private_file(self.path)
        self.lock_path = self.path.with_name(self.path.name + ".delivery.lock")
        self._private_file(self.lock_path)
        with self._connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(_SCHEMA)

    @staticmethod
    def _private_parent(path: Path):
        for ancestor in (path, *path.parents):
            if ancestor.is_symlink():
                raise ValueError("Runtime path must not contain symlinks")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if stat.S_IMODE(path.parent.stat().st_mode) & 0o077:
            raise ValueError("Runtime directory must have private permissions (0700)")

    @staticmethod
    def _private_file(path: Path):
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
                raise ValueError("Runtime files must be private regular files (0600)")
        finally:
            os.close(descriptor)

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def _transaction(self):
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @contextmanager
    def _delivery_lock(self):
        descriptor = os.open(self.lock_path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise DeliveryBusy("Another delivery/reconciliation is active") from exc
            yield
        finally:
            os.close(descriptor)

    @staticmethod
    def _current(db, key: ReportKey):
        row = db.execute("SELECT r.* FROM revisions r JOIN reports p USING(agent,day,report_type) "
                         "WHERE r.agent=? AND r.day=? AND r.report_type=? AND r.revision=p.current_revision", key.values()).fetchone()
        if row is None:
            raise KeyError("Report not archived")
        return row

    def archive(self, key: ReportKey, *, destination: str, text: str, reflection=None,
                memory_updates: dict | None = None, revision: int = 1,
                expected_revision: int | None = None, chunk_units: int = 3500) -> dict:
        """Atomically archive the entire generated package before any send.

        Repeated identical archive is a no-op. An explicit next revision plus
        expected_revision may replace ONLY an unsent, uncommitted package.
        Corrections after delivery require a distinct explicit report_type.
        """
        identity = key.values()
        if not isinstance(destination, str) or not destination.strip() or len(destination) > 512 or any(ord(c) < 32 for c in destination):
            raise ValueError("Destination must be an identifier, never a token or URL")
        if "://" in destination or _TOKEN.search(destination):
            raise ValueError("Destination must be an identifier, never a token or URL")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise ValueError("Invalid revision")
        if expected_revision is not None and (not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 1):
            raise ValueError("Invalid expected revision")
        updates = {} if memory_updates is None else memory_updates
        if not isinstance(updates, dict) or any(not isinstance(k, str) or not k for k in updates):
            raise ValueError("Memory updates must map nonempty names to JSON values")
        parts = _chunks(text, chunk_units)
        payload = _json({"destination": destination, "text": text, "reflection": reflection,
                         "memory_updates": updates, "chunk_units": chunk_units})
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        with self._delivery_lock(), self._transaction() as db:
            old = db.execute("SELECT current_revision FROM reports WHERE " + _WHERE, identity).fetchone()
            if old:
                current = self._current(db, key)
                if revision == current["revision"] and digest == current["payload_hash"]:
                    return self._snapshot(db, key)
                if expected_revision != current["revision"] or revision != current["revision"] + 1:
                    raise ImmutableReport("Changed payload requires explicit next revision and expected_revision")
                touched = db.execute("SELECT 1 FROM chunks WHERE " + _REV + " AND status NOT IN ('pending','rejected')", (*identity, current["revision"])).fetchone()
                if current["status"] not in ("ready", "rejected") or current["memory_committed"] or touched:
                    raise ImmutableReport("Cannot revise delivered, ambiguous, or memory-committed report")
                db.execute("UPDATE revisions SET status='superseded' WHERE " + _REV, (*identity, current["revision"]))
                db.execute("UPDATE reports SET current_revision=? WHERE " + _WHERE, (revision, *identity))
            else:
                if revision != 1 or expected_revision is not None:
                    raise ImmutableReport("Initial report must have revision 1")
                db.execute("INSERT INTO reports VALUES(?,?,?,?)", (*identity, revision))
            db.execute("INSERT INTO revisions(agent,day,report_type,revision,destination,payload_json,payload_hash,status,created_at) VALUES(?,?,?,?,?,?,?,'ready',?)",
                       (*identity, revision, destination, payload, digest, _now()))
            db.executemany("INSERT INTO chunks(agent,day,report_type,revision,part,text,status,destination) VALUES(?,?,?,?,?,?,'pending',?)",
                           [(*identity, revision, index, part, destination) for index, part in enumerate(parts)])
            return self._snapshot(db, key)

    def _snapshot(self, db, key: ReportKey, revision: int | None = None) -> dict:
        row = self._current(db, key) if revision is None else db.execute("SELECT * FROM revisions WHERE " + _REV, (*key.values(), revision)).fetchone()
        if row is None:
            raise KeyError("Revision not archived")
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        result["chunks"] = [dict(r) for r in db.execute("SELECT part,text,status,message_id FROM chunks WHERE " + _REV + " ORDER BY part", (*key.values(), row["revision"]))]
        return result

    def get(self, key: ReportKey, *, revision: int | None = None) -> dict:
        with self._connection() as db:
            # A read transaction gives one coherent revision/chunk snapshot.
            db.execute("BEGIN")
            return self._snapshot(db, key, revision)

    def commit_memory(self, key: ReportKey) -> bool:
        """Commit archived reflection's memory patch exactly once, within SQLite.

        Delivery retries never regenerate reflection or call this operation.
        There is no external callback, which could not be made atomic with SQLite.
        Older reports enter the ledger but cannot roll back newer per-key memory.
        """
        with self._transaction() as db:
            row = self._current(db, key)
            if row["memory_committed"]:
                return False
            updates = json.loads(row["payload_json"])["memory_updates"]
            db.execute("INSERT INTO memory_events VALUES(?,?,?,?,?,?)", (*key.values(), row["revision"], _json(updates), _now()))
            version = [key.day, row["created_at"], key.report_type, row["revision"]]
            for name, value in updates.items():
                previous = db.execute("SELECT version_json FROM memory_versions WHERE agent=? AND name=?", (key.agent, name)).fetchone()
                if previous and version <= json.loads(previous["version_json"]):
                    continue
                db.execute("INSERT INTO memory VALUES(?,?,?) ON CONFLICT(agent,name) DO UPDATE SET value_json=excluded.value_json", (key.agent, name, _json(value)))
                db.execute("INSERT INTO memory_versions VALUES(?,?,?) ON CONFLICT(agent,name) DO UPDATE SET version_json=excluded.version_json", (key.agent, name, _json(version)))
            db.execute("UPDATE revisions SET memory_committed=1 WHERE " + _REV, (*key.values(), row["revision"]))
            return True

    def memory(self, agent: str) -> dict:
        with self._connection() as db:
            return {r["name"]: json.loads(r["value_json"]) for r in db.execute("SELECT name,value_json FROM memory WHERE agent=?", (agent,))}

    def backup(self, destination: str | Path) -> Path:
        """Consistent online SQLite snapshot, atomically published without overwrite.

        Copies committed WAL contents via SQLite's backup API, not file copying.
        Restore this standalone file to a private runtime directory, then call
        recover_inflight(): an archived 'sending' part is still ambiguous.
        """
        target = Path(destination).absolute()
        self._private_parent(target)
        if target.exists():
            raise FileExistsError("Backup destination already exists")
        descriptor, temporary_name = tempfile.mkstemp(prefix=".outbox-backup-", suffix=".tmp", dir=target.parent)
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            with self._connection() as source:
                backup = sqlite3.connect(str(temporary))
                try:
                    source.backup(backup)
                    # The source's WAL setting can be copied into the header.
                    # Publish a self-contained DELETE-mode snapshot, no sidecars.
                    if backup.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                        raise OutboxError("Backup must be a standalone database")
                    if backup.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise OutboxError("Backup integrity check failed")
                finally:
                    backup.close()
            descriptor = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            # link() is atomic and fails if another writer created the target.
            os.link(temporary, target)
            descriptor = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            return target
        finally:
            for path in (temporary, Path(str(temporary) + "-wal"), Path(str(temporary) + "-shm")):
                path.unlink(missing_ok=True)

    @staticmethod
    def _receipt(receipt: Receipt, destination: str) -> str:
        if not isinstance(receipt, Receipt) or receipt.destination != destination:
            raise ValueError("Missing receipt or mismatched destination")
        value = receipt.message_id
        if isinstance(value, bool) or not isinstance(value, (str, int)) or (isinstance(value, int) and value <= 0):
            raise ValueError("Invalid message ID")
        value = str(value)
        if (not value or len(value) > 512 or any(c.isspace() or ord(c) < 32 for c in value)
                or (value.lstrip("-").isdigit() and int(value) <= 0) or _TOKEN.search(value)):
            raise ValueError("Invalid message ID")
        return value

    def _finish_part(self, db, identity, part, message_id):
        db.execute("UPDATE chunks SET status='sent',message_id=? WHERE " + _REV + " AND part=?", (message_id, *identity, part))
        remaining = db.execute("SELECT 1 FROM chunks WHERE " + _REV + " AND status!='sent'", identity).fetchone()
        db.execute("UPDATE revisions SET status=?,sent_at=?,error_type=NULL WHERE " + _REV,
                   ("ready" if remaining else "sent", None if remaining else _now(), *identity))

    def _uncertain(self, key, revision, part, error_type):
        with self._transaction() as db:
            identity = (*key.values(), revision)
            db.execute("UPDATE chunks SET status='needs_review' WHERE " + _REV + " AND part=?", (*identity, part))
            db.execute("UPDATE revisions SET status='needs_review',error_type=? WHERE " + _REV, (error_type, *identity))

    def deliver(self, key: ReportKey, transport: Transport) -> dict:
        """Deliver pending parts; uncertain results ALWAYS require reconciliation.

        One OS lock covers transport calls, preventing other processes from
        treating a live send as a crash. A real process crash releases that lock.
        """
        with self._delivery_lock():
            with self._transaction() as db:
                row = self._current(db, key)
                if row["status"] == "sending":
                    identity = (*key.values(), row["revision"])
                    db.execute("UPDATE chunks SET status='needs_review' WHERE " + _REV + " AND status='sending'", identity)
                    db.execute("UPDATE revisions SET status='needs_review',error_type='InterruptedDelivery' WHERE " + _REV, identity)
                if row["status"] != "ready":
                    return self._snapshot(db, key)
                revision = row["revision"]
            identity = (*key.values(), revision)
            while True:
                with self._transaction() as db:
                    part = db.execute("SELECT * FROM chunks WHERE " + _REV + " AND status='pending' ORDER BY part LIMIT 1", identity).fetchone()
                    if part is None:
                        return self._snapshot(db, key)
                    db.execute("UPDATE chunks SET status='sending' WHERE " + _REV + " AND part=?", (*identity, part["part"]))
                    db.execute("UPDATE revisions SET status='sending',error_type=NULL WHERE " + _REV, identity)
                idempotency_key = hashlib.sha256(_json([*identity, part["part"]]).encode()).hexdigest()
                try:
                    receipt = transport.send(part["destination"], part["text"], idempotency_key=idempotency_key)
                except RejectedDelivery:
                    with self._transaction() as db:
                        db.execute("UPDATE chunks SET status='rejected' WHERE " + _REV + " AND part=?", (*identity, part["part"]))
                        db.execute("UPDATE revisions SET status='rejected',error_type='RejectedDelivery' WHERE " + _REV, identity)
                    return self.get(key)
                except Exception as exc:
                    self._uncertain(key, revision, part["part"], type(exc).__name__)
                    return self.get(key)
                try:
                    message_id = self._receipt(receipt, part["destination"])
                    with self._transaction() as db:
                        self._finish_part(db, identity, part["part"], message_id)
                except Exception as exc:
                    self._uncertain(key, revision, part["part"], type(exc).__name__)
                    return self.get(key)

    def retry_rejected(self, key: ReportKey) -> None:
        """Explicitly retry only parts whose non-acceptance was established."""
        with self._delivery_lock(), self._transaction() as db:
            row = self._current(db, key)
            if row["status"] != "rejected":
                raise OutboxError("Only deterministic rejections can be retried")
            identity = (*key.values(), row["revision"])
            db.execute("UPDATE chunks SET status='pending' WHERE " + _REV + " AND status='rejected'", identity)
            db.execute("UPDATE revisions SET status='ready',error_type=NULL WHERE " + _REV, identity)

    def recover_inflight(self) -> int:
        """Call at worker startup: mark abandoned sends without any network call.

        Refuses to run while another worker holds the delivery lock. Recovery
        never assumes that an interrupted send was rejected by the destination.
        """
        with self._delivery_lock(), self._transaction() as db:
            rows = db.execute("SELECT agent,day,report_type,revision FROM revisions WHERE status='sending'").fetchall()
            for row in rows:
                identity = tuple(row)
                db.execute("UPDATE chunks SET status='needs_review' WHERE " + _REV + " AND status='sending'", identity)
                db.execute("UPDATE revisions SET status='needs_review',error_type='InterruptedDelivery' WHERE " + _REV, identity)
            return len(rows)

    def reconcile(self, key: ReportKey, part: int, *, receipt: Receipt | None = None, confirmed_not_sent: bool = False) -> None:
        """Operator supplies a real receipt OR positive evidence of non-delivery.

        A missing/unknown receipt is never evidence of non-delivery.
        """
        if not isinstance(confirmed_not_sent, bool) or (receipt is not None) == confirmed_not_sent:
            raise ValueError("Supply either a receipt or confirmed_not_sent=True")
        with self._delivery_lock(), self._transaction() as db:
            row = self._current(db, key)
            identity = (*key.values(), row["revision"])
            chunk = db.execute("SELECT * FROM chunks WHERE " + _REV + " AND part=?", (*identity, part)).fetchone()
            if row["status"] != "needs_review" or chunk is None or chunk["status"] != "needs_review":
                raise OutboxError("Only ambiguous parts can be reconciled")
            if receipt is not None:
                self._finish_part(db, identity, part, self._receipt(receipt, row["destination"]))
            else:
                db.execute("UPDATE chunks SET status='pending' WHERE " + _REV + " AND part=?", (*identity, part))
                db.execute("UPDATE revisions SET status='ready',error_type=NULL WHERE " + _REV, identity)
