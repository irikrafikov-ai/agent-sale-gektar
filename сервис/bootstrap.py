"""Prepare the dedicated volume as root, then exec business code as agent.

Only this entrypoint owns the /app/данные -> /data/sales/data mapping.
No business imports, clients, AI calls or existing-tree recursive chown here.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import pwd
import re
import stat
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone

VOLUME = Path("/data")
APP_DATA = Path("/app/данные")
SEED = Path("/opt/sales-seed")
MARKER = ".zemfond-sales-volume.json"
ROOT_FILES = {"реестр-клиентов.md", "реестр-клиентов.json", "обучение.md", "гипотезы.md"}
FOLDERS = {"журнал", "отчёты", "реестры"}
SECRET_NAMES = re.compile(r"TOKEN|SECRET|PASSWORD|API_KEY|WEBHOOK", re.I)
SECRET_CONTENT = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    r"\bsk-(?:ant-)?[A-Za-z0-9_-]{16,}|\b[0-9]{6,}:[A-Za-z0-9_-]{25,}|"
    r"https?://[^\s/]+/rest/[0-9]+/[A-Za-z0-9_-]{8,}/|"
    r"(?:api[_-]?key|access[_-]?token|client[_-]?secret|password|bot[_-]?token)"
    r"\s*[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9_./:+-]{8,}", re.I)


class StorageError(RuntimeError):
    pass


def plain(path, *, directory=True):
    """Do not follow symlinks at any existing path component."""
    path = Path(path)
    for component in (path, *path.parents):
        if component.is_symlink():
            raise StorageError("symlink_storage_path")
    if directory and not path.is_dir():
        raise StorageError("storage_directory_missing")
    return path


def own_new(path, uid, gid):
    # Only called immediately after exclusive creation by this process.
    if (path.stat().st_uid, path.stat().st_gid) != (uid, gid):
        os.chown(path, uid, gid, follow_symlinks=False)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def directory(path, uid, gid, mode):
    plain(path, directory=False)
    try:
        path.mkdir(mode=mode)
    except FileExistsError:
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or info.st_gid != gid
                or stat.S_IMODE(info.st_mode) != mode):
            raise StorageError("existing_directory_not_owned") from None
    else:
        os.chmod(path, mode, follow_symlinks=False)
        own_new(path, uid, gid)
        sync_directory(path.parent)


def private_json(path, value, *, new=False):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    if new:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, raw)
            os.fsync(fd)
        finally:
            os.close(fd)
        sync_directory(path.parent)
        return
    fd, temporary = tempfile.mkstemp(prefix=MARKER + ".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)  # only this call's freshly created temporary


def permitted_seed(relative):
    return (not any(part.startswith(".") for part in relative.parts)
            and relative.suffix.lower() in {".md", ".json"}
            and (str(relative) in ROOT_FILES
                 or (len(relative.parts) > 1 and relative.parts[0] in FOLDERS)))


def secret_content(text, env):
    if SECRET_CONTENT.search(text):
        return True
    return any(value in text for key, value in env.items()
               if SECRET_NAMES.search(key) and len(value) >= 8)


def secret_json(value):
    if isinstance(value, dict):
        return any((SECRET_NAMES.search(str(key)) and bool(item)) or secret_json(item)
                   for key, item in value.items())
    return isinstance(value, list) and any(secret_json(item) for item in value)


def seed_once(source, destination, uid, gid, *, env=None):
    """Allowlisted work documents only. Existing files are never overwritten."""
    plain(source)
    plain(destination)
    env = os.environ if env is None else env
    copied = skipped = 0
    for base, dirs, files in os.walk(source, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".")
                         and not (Path(base) / d).is_symlink())
        for name in sorted(files):
            path = Path(base) / name
            relative = path.relative_to(source)
            if not permitted_seed(relative):
                skipped += 1
                continue
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 16 * 1024 * 1024:
                skipped += 1
                continue
            try:
                raw = path.read_bytes()
                text = raw.decode("utf-8")
                if "\x00" in text or secret_content(text, env):
                    raise ValueError()
                if relative.suffix.lower() == ".json":
                    if secret_json(json.loads(text)):
                        raise ValueError()
            except (ValueError, UnicodeError):
                skipped += 1
                continue
            target = destination / relative
            parent = destination
            for part in relative.parts[:-1]:
                parent = parent / part
                directory(parent, uid, gid, 0o700)
            plain(target, directory=False)
            if target.exists():
                # A partial first start may have copied this file already.
                if not target.is_file():
                    raise StorageError("seed_target_not_regular")
                continue
            fd, temporary = tempfile.mkstemp(prefix=".seed-", dir=target.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                own_new(Path(temporary), uid, gid)
                try:
                    os.link(temporary, target, follow_symlinks=False)  # atomic, no overwrite
                    copied += 1
                except FileExistsError:
                    raise StorageError("seed_target_changed") from None
            finally:
                os.unlink(temporary)  # only the temporary created immediately above
            sync_directory(target.parent)
    return {"copied": copied, "skipped": skipped}


def prepare_volume(volume, source, uid, gid, *, require_mount=True, env=None):
    volume, source = Path(volume), Path(source)
    plain(volume)
    if require_mount and not os.path.ismount(volume):
        raise StorageError("persistent_mount_required")
    marker = volume / MARKER
    root_uid, root_gid = os.geteuid(), os.getegid()
    allowed = {MARKER, "sales", "lost+found"}
    if marker.exists() or marker.is_symlink():
        plain(marker, directory=False)
        info = marker.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != root_uid or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 4096):
            raise StorageError("volume_marker_invalid")
        try:
            state = json.loads(marker.read_text())
        except (ValueError, UnicodeError):
            raise StorageError("volume_marker_invalid") from None
        if (state.get("version"), state.get("role"), state.get("uid"), state.get("gid")) != (1, "sales", uid, gid):
            raise StorageError("volume_marker_conflict")
        if state.get("state") not in {"initializing", "ready"}:
            raise StorageError("volume_marker_invalid")
    else:
        if any(p.name != "lost+found" for p in volume.iterdir()):
            raise StorageError("dedicated_empty_volume_required")
        state = {"version": 1, "role": "sales", "uid": uid, "gid": gid, "state": "initializing"}
        private_json(marker, state, new=True)
    if any(p.name not in allowed for p in volume.iterdir()):
        raise StorageError("unexpected_volume_contents")
    base, data, runtime = volume / "sales", volume / "sales/data", volume / "sales/runtime"
    directory(base, root_uid, root_gid, 0o711)
    directory(data, uid, gid, 0o700)
    directory(runtime, uid, gid, 0o700)
    if state["state"] != "ready":
        counts = seed_once(source, data, uid, gid, env=env)
        state.update(state="ready", seed=counts)
        private_json(marker, state)
    return data, runtime


def link_working_data(anchor, target):
    """No arbitrary symlink accepted. Existing directories are never removed."""
    anchor, target = Path(anchor), plain(target)
    plain(anchor.parent)
    if anchor.is_symlink():
        if os.readlink(anchor) != str(target) or anchor.resolve() != target:
            raise StorageError("working_data_mapping_conflict")
        return
    if anchor.exists():
        raise StorageError("working_data_mapping_conflict")
    anchor.symlink_to(target, target_is_directory=True)


def drop_privileges(user):
    if os.geteuid() == 0:
        os.initgroups(user.pw_name, user.pw_gid)
        os.setgid(user.pw_gid)
        os.setuid(user.pw_uid)
    if os.geteuid() != user.pw_uid or os.getegid() != user.pw_gid or user.pw_uid == 0:
        raise StorageError("nonroot_agent_required")
    os.environ["HOME"] = user.pw_dir


def cron_held(argv, env, now=None):
    """Image/deploy starts must not fire a pre-cutover cron business cycle."""
    role = argv[0] if argv else "вечер"
    if role not in {"утро", "вечер"}:
        return False
    raw = env.get("SALES_CRON_START_DATE")
    if not raw and env.get("AGENT_SDK_PROVIDER", "").strip().lower() != "codex":
        return False
    try:
        start = date.fromisoformat(raw or "")
        if start < date(2026, 10, 7):
            raise ValueError()
    except ValueError:
        raise StorageError("cron_start_date_required") from None
    msk = timezone(timedelta(hours=3))
    moment = now or datetime.now(msk)
    if moment.tzinfo is None:
        raise StorageError("cron_timezone_required")
    local = moment.astimezone(msk)
    if local.date() < start:
        return True
    if env.get("SALES_CRON_SCHEDULE_GUARD") == "1":
        # Deployment starts are not scheduled sales cycles, especially at night.
        scheduled = local.replace(hour=9 if role == "утро" else 18,
                                  minute=30, second=0, microsecond=0)
        return not 0 <= (local - scheduled).total_seconds() <= 20 * 60
    return False


def preflight_server(port):
    """Keep the webhook inspectable without importing or acknowledging business."""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Hold(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Never log secret-bearing webhook paths or request bodies.

        def do_GET(self):
            if self.path != "/health":
                self.send_error(503)
                return
            body = b'{"status":"migration_hold","business_code_started":false}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            # Do not ACK incoming events during migration: allow Avito retry.
            self.send_response(503)
            self.send_header("Retry-After", "30")
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

    with HTTPServer(("0.0.0.0", port), Hold) as server:
        server.serve_forever()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        user = pwd.getpwnam("agent")
        if os.geteuid() != 0:
            raise StorageError("bootstrap_root_required")
        durable = os.environ.get("AGENT_SDK_PROVIDER", "claude").strip().lower() == "codex" or bool(os.environ.get("SALES_RUNTIME_DIR"))
        if durable:
            if os.environ.get("SALES_RUNTIME_DIR") != str(VOLUME / "sales/runtime"):
                raise StorageError("persistent_runtime_path_required")
            data, runtime = prepare_volume(VOLUME, SEED, user.pw_uid, user.pw_gid)
            link_working_data(APP_DATA, data)
        else:
            # Legacy-only services (e.g. dashboard) retain isolated local state.
            if not APP_DATA.exists():
                directory(APP_DATA, user.pw_uid, user.pw_gid, 0o700)
                seed_once(SEED, APP_DATA, user.pw_uid, user.pw_gid)
            else:
                plain(APP_DATA)
        drop_privileges(user)
        if durable and not all(os.access(path, os.R_OK | os.W_OK | os.X_OK) for path in (data, runtime)):
            raise StorageError("agent_storage_inaccessible")
        if os.environ.get("SALES_BOOTSTRAP_ONLY") == "1":
            print("sales_storage_preflight_ok; business_code_not_started", flush=True)
            if argv and argv[0] == "вебхук" and os.environ.get("SALES_PREFLIGHT_KEEPALIVE") == "1":
                preflight_server(int(os.environ.get("PORT", "8080")))
            return 0
        if cron_held(argv, os.environ):
            print("sales_cron_date_hold; business_code_not_started", flush=True)
            return 0
        dispatcher = Path(__file__).with_name("запуск.py")
        os.execv(sys.executable, [sys.executable, str(dispatcher), *argv])
    except StorageError as error:
        print("sales_storage_bootstrap_failed:" + str(error), file=sys.stderr)
        return 2
    except Exception:
        print("sales_storage_bootstrap_failed:unexpected_error", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
