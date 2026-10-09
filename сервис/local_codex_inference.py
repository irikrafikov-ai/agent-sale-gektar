"""Isolated, idempotent Codex CLI inference. No business tools or API credentials.

Only this adapter's private archive is portable state. CODEX_HOME remains a
separate, explicitly supplied credential store and is never copied into it.
An interrupted operation needs operator reconciliation, never an automatic retry.
"""
# Adapted from zemfond-codex-control 20eb3a2, cloud_control/codex_inference.py.
# Local-only changes: pinned desktop CLI, ChatGPT-only auth, no inherited rules.
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import tempfile
import time
from typing import Any, Mapping

RUNTIME_VERSION = "codex-inference-v2-roles"
PINNED_CODEX_VERSION = "0.159.0-alpha.12.1"
DISABLED_FEATURES = (
    "shell_tool", "apps", "plugins", "browser_use", "computer_use",
    "multi_agent", "hooks",
)
# Deliberately no OPENAI_API_KEY/CODEX_API_KEY, business secrets, PYTHONPATH,
# NODE_OPTIONS, loader variables, arbitrary CODEX_* overrides or inherited MCP.
ENV_ALLOWLIST = frozenset({
    "HOME", "CODEX_HOME", "PATH", "LANG", "LC_ALL",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR",
})
MAX_STREAM_BYTES = 8 * 1024 * 1024
MAX_PROMPT_BYTES = 4 * 1024 * 1024


class CodexInferenceError(RuntimeError):
    """Public errors contain a fixed code only, never prompts or CLI stderr."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(f"Codex inference: {code}")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise CodexInferenceError("insecure_runtime_directory")


def _atomic_text(path: Path, value: str) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _atomic(path: Path, value: Any) -> None:
    _atomic_text(path, _json(value) + "\n")


def _read(path: Path) -> Any:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise CodexInferenceError("insecure_archive_file")
        return json.load(stream)


def _validator(schema: dict[str, Any]):
    # Remote references would let validation itself read a URL or local file.
    def references(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"$ref", "$dynamicRef", "$recursiveRef"}:
                    if not isinstance(item, str) or not item.startswith("#"):
                        raise CodexInferenceError("external_schema_reference")
                references(item)
        elif isinstance(value, list):
            for item in value:
                references(item)
    references(schema)
    try:
        from jsonschema import validators
    except ImportError:
        raise CodexInferenceError("schema_validator_unavailable") from None
    try:
        cls = validators.validator_for(schema)
        cls.check_schema(schema)
        return cls(schema)
    except Exception:
        raise CodexInferenceError("invalid_schema") from None


def _strict_loads(value: str) -> Any:
    def duplicate_checked(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = item
        return result

    def invalid_constant(_):
        raise ValueError("non-finite number")
    return json.loads(value, object_pairs_hook=duplicate_checked,
                      parse_constant=invalid_constant)


def _check_event(event: Any) -> None:
    if not isinstance(event, dict):
        raise CodexInferenceError("invalid_events")
    kind = event.get("type")
    if kind in {"turn.failed", "error"}:
        raise CodexInferenceError("turn_failed")
    if kind in {"item.started", "item.updated", "item.completed"}:
        item = event.get("item")
        if not isinstance(item, dict):
            raise CodexInferenceError("invalid_events")
        # Deny unknown/new item kinds too: a future CLI must not acquire tools
        # merely because its new event name is absent from a denylist.
        if item.get("type") not in {"agent_message", "reasoning"}:
            raise CodexInferenceError("unexpected_tool_call")
    elif kind not in {"thread.started", "turn.started", "turn.completed"}:
        raise CodexInferenceError("unexpected_event")


def _kill_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def _capture(command: list[str], *, prompt: bytes, env: dict[str, str],
             cwd: Path, timeout: float, events: bool = True) -> tuple[bytes, bytes, int, str | None]:
    """Bounded, deadlock-free pipes; kill the whole group on timeout/tool events."""
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   cwd=cwd, env=env, start_new_session=True)
    except OSError:
        return b"", b"", -1, "cli_start_failed"
    output, errors, pending = bytearray(), bytearray(), bytearray()
    failure = None
    deadline = time.monotonic() + timeout
    selector = selectors.DefaultSelector()
    try:
        for stream, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        if prompt:
            os.set_blocking(process.stdin.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
        else:
            process.stdin.close()
        written = 0
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failure = "timeout"
                break
            for key, _ in selector.select(min(remaining, 0.1)):
                if key.data == "stdin":
                    try:
                        written += os.write(key.fd, prompt[written:written + 65536])
                    except BrokenPipeError:
                        written = len(prompt)
                    if written == len(prompt):
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                    continue
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                target = output if key.data == "stdout" else errors
                target.extend(chunk)
                if len(output) + len(errors) > MAX_STREAM_BYTES:
                    failure = "output_limit"
                    break
                if events and key.data == "stdout":
                    pending.extend(chunk)
                    while b"\n" in pending:
                        line, _, rest = pending.partition(b"\n")
                        pending[:] = rest
                        if not line.strip():
                            continue
                        try:
                            _check_event(_strict_loads(line.decode("utf-8")))
                        except CodexInferenceError as error:
                            failure = error.code
                            break
                        except (ValueError, UnicodeError):
                            failure = "invalid_events"
                            break
                if failure:
                    break
            if failure:
                break
        if failure:
            _kill_group(process)
        else:
            try:
                process.wait(timeout=max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                failure = "timeout"
                _kill_group(process)
        return bytes(output), bytes(errors), process.returncode, failure
    finally:
        # Covers caller cancellation and unexpected local IO errors as well.
        _kill_group(process)
        selector.close()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream and not stream.closed:
                stream.close()


def _result(raw: bytes, validator, model: str) -> dict[str, Any]:
    try:
        events = [_strict_loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    except (ValueError, UnicodeError):
        raise CodexInferenceError("invalid_events") from None
    thread_id, final, usage = None, None, None
    completed = False
    for event in events:
        _check_event(event)
        if completed:
            raise CodexInferenceError("events_after_completion")
        if event["type"] == "thread.started":
            candidate = event.get("thread_id")
            if thread_id or not isinstance(candidate, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", candidate):
                raise CodexInferenceError("invalid_thread")
            thread_id = candidate
        elif event["type"] == "item.completed":
            item = event["item"]
            if item["type"] == "agent_message" and item.get("phase") in {None, "final_answer"}:
                final = item.get("text")
        elif event["type"] == "turn.completed":
            completed = True
            usage = event.get("usage")
    if not completed or not thread_id or not isinstance(final, str) or not final.strip():
        raise CodexInferenceError("incomplete_turn")
    try:
        structured = _strict_loads(final)
        validator.validate(structured)
        _json(structured)  # finite values only, including huge numeric literals
    except Exception:
        raise CodexInferenceError("invalid_output_schema") from None
    if usage is not None:
        if not isinstance(usage, dict) or any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in usage.values()
        ):
            raise CodexInferenceError("invalid_usage")
    return {"output": structured, "text": final.strip(), "usage": usage,
            "usd": None, "thread_id": thread_id, "model": model,
            "status": "completed", "runtime_version": RUNTIME_VERSION}


def _started_thread(raw: bytes) -> str | None:
    """Keep recovery identity even when a later event failed or was truncated."""
    for line in raw.decode("utf-8", "replace").splitlines():
        try:
            event = _strict_loads(line)
            candidate = event.get("thread_id")
            if (event.get("type") == "thread.started" and isinstance(candidate, str)
                    and re.fullmatch(r"[A-Za-z0-9_-]{1,160}", candidate)):
                return candidate
        except (ValueError, AttributeError):
            continue
    return None


def infer(prompt: str, *, trusted_instructions: str, schema: dict[str, Any], model: str, operation_key: str,
          runtime_dir: str | Path, auth_env: Mapping[str, str],
          timeout_seconds: float = 180, codex_bin: str = "codex",
          expected_codex_version: str = PINNED_CODEX_VERSION) -> dict[str, Any]:
    """One durable operation; completed results are cached, all others fail closed.

    auth_env is explicit, not merged with os.environ. Caller must pass absolute
    HOME and CODEX_HOME. It may contain extra business keys: they are discarded.
    An operation key is hashed in filenames and not written to the archive.
    The fingerprint covers trusted instructions, user input, schema, model and
    both runtime versions. The role replaces Codex's built-in base instructions
    via a private per-operation file; it is never flattened into the user input.
    """
    try:
        if (not isinstance(prompt, str) or not prompt.strip()
                or not isinstance(trusted_instructions, str) or not trusted_instructions.strip()
                or len(prompt.encode("utf-8")) + len(trusted_instructions.encode("utf-8")) > MAX_PROMPT_BYTES
                or not isinstance(schema, dict) or not isinstance(model, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,159}", model)
                or not isinstance(operation_key, str) or not operation_key.strip()
                or not isinstance(expected_codex_version, str)
                or not re.fullmatch(r"[A-Za-z0-9._-]+", expected_codex_version)
                or not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
            raise CodexInferenceError("invalid_request")
        validator = _validator(schema)
        fingerprint = _hash(_json({"prompt": prompt, "trusted_instructions": trusted_instructions,
                                   "schema": schema, "model": model,
                                   "runtime_version": RUNTIME_VERSION,
                                   "codex_version": expected_codex_version}))
        env = {key: value for key, value in auth_env.items()
               if key in ENV_ALLOWLIST and isinstance(value, str)}
        if any(not env.get(key) or not Path(env[key]).is_absolute()
               for key in ("HOME", "CODEX_HOME")):
            raise CodexInferenceError("explicit_auth_home_required")
        env.setdefault("PATH", os.defpath)
        env.setdefault("LANG", "C.UTF-8")
        root = Path(runtime_dir).absolute()
        credentials = Path(env["CODEX_HOME"]).resolve()
        resolved_root = root.resolve()
        if resolved_root == credentials or credentials in resolved_root.parents or resolved_root in credentials.parents:
            raise CodexInferenceError("archive_overlaps_credentials")
        _private_dir(root)
        operations = root / "operations"
        _private_dir(operations)
        operation = operations / _hash(operation_key)
        _private_dir(operation)
        lock_fd = os.open(operation / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            lock_info = os.fstat(lock_fd)
            if (not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != os.getuid()
                    or stat.S_IMODE(lock_info.st_mode) & 0o077):
                raise CodexInferenceError("insecure_archive_file")
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise CodexInferenceError("operation_in_progress") from None
            state_path = operation / "metadata.json"
            if state_path.exists():
                state = _read(state_path)
                if state.get("fingerprint") != fingerprint:
                    raise CodexInferenceError("operation_conflict")
                if state.get("status") != "completed":
                    raise CodexInferenceError("operation_incomplete")
                result = _read(operation / "output.json")
                if _hash(_json(result)) != state.get("output_hash"):
                    raise CodexInferenceError("archive_integrity_error")
                validator.validate(result["output"])
                return result
            state = {"fingerprint": fingerprint, "input_hash": _hash(prompt),
                     "trusted_instructions_hash": _hash(trusted_instructions),
                     "schema_hash": _hash(_json(schema)), "model": model,
                     "runtime_version": RUNTIME_VERSION,
                     "codex_version": expected_codex_version,
                     "status": "running", "started_at": time.time()}
            _atomic(operation / "input.json", {"prompt": prompt,
                                              "trusted_instructions": trusted_instructions,
                                              "schema": schema})
            _atomic(state_path, state)  # persisted before any child invocation
            try:
                deadline = time.monotonic() + timeout_seconds
                with tempfile.TemporaryDirectory(prefix=".inference-", dir=root) as temporary:
                    cwd = Path(temporary)
                    env["TMPDIR"] = str(cwd)
                    version, _, code, failure = _capture(
                        [codex_bin, "--version"], prompt=b"", env=env, cwd=cwd,
                        timeout=min(timeout_seconds, 10), events=False)
                    if failure or code != 0 or version.decode("utf-8", "replace").strip() != f"codex-cli {expected_codex_version}":
                        raise CodexInferenceError(failure or "cli_version_mismatch")
                    _atomic(cwd / "output-schema.json", schema)
                    role_path = cwd / "role.txt"
                    _atomic_text(role_path, trusted_instructions)
                    command = [codex_bin, "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral"]
                    for feature in DISABLED_FEATURES:
                        command.extend(["--disable", feature])
                    command.extend(["--model", model, "--sandbox", "read-only",
                                    "-c", "approval_policy=\"never\"",
                                    "-c", "forced_login_method=\"chatgpt\"",
                                    "-c", "web_search=\"disabled\"",
                                    "-c", "model_instructions_file=" + _json(str(role_path)),
                                    "-c", "mcp_servers={}", "--json", "--skip-git-repo-check",
                                    "--output-schema", str(cwd / "output-schema.json"), "-"])
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise CodexInferenceError("timeout")
                    raw, stderr, code, failure = _capture(command, prompt=prompt.encode("utf-8"),
                                                        env=env, cwd=cwd, timeout=remaining)
                    # Raw streams live only in the protected archive, never logs/errors.
                    _atomic(operation / "events.json", {"stdout": raw.decode("utf-8", "replace"),
                                                         "stderr": stderr.decode("utf-8", "replace")})
                    state["thread_id"] = _started_thread(raw)
                    if failure or code != 0:
                        raise CodexInferenceError(failure or "cli_failed")
                    result = _result(raw, validator, model)
                    _atomic(operation / "output.json", result)
                    state.update(status="completed", finished_at=time.time(),
                                 thread_id=result["thread_id"], output_hash=_hash(_json(result)))
                    _atomic(state_path, state)
                    return result
            except BaseException as error:
                state.update(status="incomplete", finished_at=time.time(),
                             error_code=error.code if isinstance(error, CodexInferenceError) else "local_failure")
                _atomic(state_path, state)
                raise
        finally:
            os.close(lock_fd)
    except CodexInferenceError:
        raise
    except Exception:
        raise CodexInferenceError("local_failure") from None
