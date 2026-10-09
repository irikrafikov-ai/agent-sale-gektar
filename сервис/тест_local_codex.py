"""Offline local Codex contracts. All inference and tools are fixtures."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import codex_sdk as sdk
import local_codex_inference as cli
import расход_codex


class LocalContracts(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.env = {"HOME": str(self.root), "CODEX_HOME": str(self.root / "auth"),
                    "SALES_CODEX_BIN": "/usr/bin/true", "CODEX_MODEL": "fixture-model",
                    "SALES_RUNTIME_DIR": str(self.root / "runtime"), "SALES_CODEX_TRANSPORT": "local",
                    "OPENAI_API_KEY": "secret-api-fixture", "ANTHROPIC_API_KEY": "secret-claude-fixture",
                    "BITRIX_WEBHOOK": "https://secret.invalid", "CODEX_ACCESS_TOKEN": "secret-token-fixture"}

    async def test_local_selection_and_secrets_not_in_child_environment(self):
        with patch.dict(os.environ, self.env, clear=True):
            backend = sdk.configured_backend()
            self.assertIsInstance(backend, sdk.LocalCLIBackend)
            self.assertEqual(set(backend.auth_env), {"HOME", "CODEX_HOME", "PATH"})
            result = {"status": "completed", "output": {"ok": True}, "model": "fixture-model",
                      "thread_id": "fixture-thread", "usage": {"input_tokens": 3}, "usd": None}
            with patch.object(cli, "infer", return_value=result) as invoke:
                response = await backend.infer({"input": "fixture", "system": "fixture-rules",
                                               "schema": {}, "operation_key": "fixture-one"})
        self.assertIsNone(response["usd"])
        self.assertEqual(response["json"], {"ok": True})
        self.assertEqual(invoke.call_args.kwargs["auth_env"], backend.auth_env)
        self.assertEqual(invoke.call_args.kwargs["expected_codex_version"], cli.PINNED_CODEX_VERSION)

    async def test_local_errors_are_fixed_codes_and_no_http_fallback(self):
        with patch.dict(os.environ, self.env, clear=True):
            backend = sdk.configured_backend()
            with patch.object(cli, "infer", side_effect=cli.CodexInferenceError("turn_failed")), \
                 patch.object(sdk, "HTTPBackend", side_effect=AssertionError("must not fallback")):
                with self.assertRaisesRegex(sdk.CodexError, "local_turn_failed"):
                    await backend.infer({"input": "fixture", "system": "fixture", "schema": {},
                                         "operation_key": "fixture-one"})

    async def test_unknown_transport_and_missing_local_config_fail_closed(self):
        with patch.dict(os.environ, {"SALES_CODEX_TRANSPORT": "typo"}, clear=True):
            with self.assertRaisesRegex(sdk.CodexError, "unknown_codex_transport"):
                sdk.configured_backend()
        with patch.dict(os.environ, {"SALES_CODEX_TRANSPORT": "local"}, clear=True):
            with self.assertRaisesRegex(sdk.CodexError, "local_configuration_invalid"):
                sdk.configured_backend()

    async def test_private_runtime_and_usage_archive_integrity(self):
        runtime = self.root / "runtime"
        runtime.mkdir(mode=0o700)
        operation = runtime / "inference" / "operations" / "fixture"
        operation.mkdir(parents=True, mode=0o700)
        result = {"thread_id": "fixture-thread", "model": "fixture-model",
                  "usage": {"input_tokens": 10, "output_tokens": 3}}
        cli._atomic(operation / "output.json", result)
        cli._atomic(operation / "metadata.json", {"status": "completed", "started_at": 1,
                                                 "output_hash": cli._hash(cli._json(result))})
        report = расход_codex.summarize(runtime)
        self.assertEqual(report["completed_inferences"], 1)
        self.assertEqual(report["usage"]["input_tokens"], 10)
        self.assertIsNone(report["usd"])
        cli._atomic(operation / "output.json", dict(result, model="tampered"))
        with self.assertRaisesRegex(ValueError, "archive_integrity_error"):
            расход_codex.summarize(runtime)

    async def test_local_runtime_unsafe_permissions_and_symlink_rejected(self):
        runtime = self.root / "runtime"
        runtime.mkdir(mode=0o755)
        with patch.dict(os.environ, self.env, clear=True):
            with self.assertRaisesRegex(sdk.CodexError, "insecure_runtime_directory"):
                sdk.configured_backend()
        linked = self.root / "linked"
        linked.symlink_to(runtime)
        with patch.dict(os.environ, dict(self.env, SALES_RUNTIME_DIR=str(linked)), clear=True):
            with self.assertRaisesRegex(sdk.CodexError, "unsafe_runtime_path"):
                sdk.configured_backend()

    async def test_completed_inference_cached_without_second_cli_invocation(self):
        events = b'\n'.join(json.dumps(item).encode() for item in [
            {"type": "thread.started", "thread_id": "fixture-thread"},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": '{"ok":true}'}},
            {"type": "turn.completed", "usage": {"input_tokens": 4, "output_tokens": 2}}])
        captures = [(f"codex-cli {cli.PINNED_CODEX_VERSION}".encode(), b"", 0, None),
                    (events, b"", 0, None)]
        kwargs = dict(trusted_instructions="fixture-rules", schema={"type": "object"},
                      model="fixture-model", operation_key="fixture-one", runtime_dir=self.root / "archive",
                      auth_env=self.env, codex_bin="/usr/bin/true")
        with patch.object(cli, "_capture", side_effect=captures) as capture:
            first = cli.infer("fixture-data", **kwargs)
            second = cli.infer("fixture-data", **kwargs)
        self.assertEqual(first, second)
        self.assertEqual(capture.call_count, 2)
        command = capture.call_args_list[1].args[0]
        self.assertIn('forced_login_method="chatgpt"', command)
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--ignore-rules", command)
        child = capture.call_args_list[1].kwargs["env"]
        for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "BITRIX_WEBHOOK", "CODEX_ACCESS_TOKEN"):
            self.assertNotIn(key, child)

    async def test_incomplete_inference_never_replayed(self):
        kwargs = dict(trusted_instructions="fixture-rules", schema={"type": "object"},
                      model="fixture-model", operation_key="fixture-one", runtime_dir=self.root / "archive",
                      auth_env=self.env, codex_bin="/usr/bin/true")
        with patch.object(cli, "_capture", return_value=(b"", b"secret-stderr", -1, "timeout")) as capture:
            with self.assertRaisesRegex(cli.CodexInferenceError, "timeout"):
                cli.infer("fixture-data", **kwargs)
            with self.assertRaisesRegex(cli.CodexInferenceError, "operation_incomplete"):
                cli.infer("fixture-data", **kwargs)
        self.assertEqual(capture.call_count, 1)

    async def test_unexpected_native_tool_event_and_noncompletion_rejected(self):
        with self.assertRaisesRegex(cli.CodexInferenceError, "unexpected_tool_call"):
            cli._check_event({"type": "item.started", "item": {"type": "command_execution"}})
        with self.assertRaisesRegex(cli.CodexInferenceError, "incomplete_turn"):
            cli._result(b'{"type":"thread.started","thread_id":"fixture"}\n',
                        cli._validator({"type": "object"}), "fixture-model")

    async def test_preflight_local_does_not_require_backend_or_api_keys(self):
        import прогон
        env = dict(self.env, AGENT_SDK_PROVIDER="codex", CODEX_START_DATE="2026-10-07",
                   AVITO_CLIENT_ID="fixture", AVITO_CLIENT_SECRET="fixture", BITRIX_WEBHOOK="fixture",
                   TELEGRAM_BOT_TOKEN="fixture", TELEGRAM_CHAT_ID="fixture")
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(прогон.проверить_переменные(), [])


if __name__ == "__main__":
    unittest.main()
