"""Read-only subscription usage from private local inference receipts, not dollars."""
import argparse
from collections import Counter
import json
from pathlib import Path
import stat
import time

from local_codex_inference import _read, _hash, _json


def summarize(runtime: Path, since: float = 0):
    # Do not follow a symlink into credentials or publish archived prompts.
    for path in (runtime, *runtime.parents):
        if path.is_symlink():
            raise ValueError("unsafe_runtime_path")
    if not runtime.is_dir() or stat.S_IMODE(runtime.stat().st_mode) & 0o077:
        raise ValueError("private_runtime_required")
    operations = runtime / "inference" / "operations"
    if operations.is_symlink():
        raise ValueError("unsafe_runtime_path")
    completed, incomplete, models, usage = 0, 0, Counter(), Counter()
    threads = set()
    if operations.is_dir():
        for operation in operations.iterdir():
            if operation.is_symlink() or not operation.is_dir():
                raise ValueError("unsafe_runtime_path")
            metadata = operation / "metadata.json"
            if not metadata.is_file():
                continue
            state = _read(metadata)
            if state.get("started_at", 0) < since:
                continue
            if state.get("status") != "completed":
                incomplete += 1
                continue
            result = _read(operation / "output.json")
            if _hash(_json(result)) != state.get("output_hash"):
                raise ValueError("archive_integrity_error")
            thread = result.get("thread_id")
            if not thread or thread in threads:
                continue
            threads.add(thread)
            completed += 1
            models[result["model"]] += 1
            usage.update(result.get("usage") or {})
    return {"billing": "ChatGPT subscription", "usd": None,
            "completed_inferences": completed, "incomplete_inferences": incomplete,
            "models": dict(models), "usage": dict(usage),
            "limitations": "Counts observed CLI receipts only; cached replays excluded. Not an account-wide quota meter or subscription invoice."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--hours", type=float, default=24)
    args = parser.parse_args()
    print(json.dumps(summarize(Path(args.runtime), time.time() - args.hours * 3600), ensure_ascii=False))
