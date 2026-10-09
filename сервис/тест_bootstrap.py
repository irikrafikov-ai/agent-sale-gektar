"""Storage/startup fixtures only. No production clients, root mutation or AI."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import bootstrap as b


class StorageContracts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.volume, self.seed = self.root / "volume", self.root / "seed"
        self.volume.mkdir()
        self.seed.mkdir()
        self.uid, self.gid = os.getuid(), os.getgid()
        (self.seed / "реестр-клиентов.md").write_text("fixture work registry", encoding="utf-8")

    def prepare(self, **kwargs):
        options = dict(require_mount=False, env={})
        options.update(kwargs)
        return b.prepare_volume(self.volume, self.seed, self.uid, self.gid, **options)

    def test_first_seed_is_private_and_restart_never_overwrites_or_recopies(self):
        data, runtime = self.prepare()
        self.assertEqual(stat.S_IMODE(data.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(runtime.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((data / "реестр-клиентов.md").stat().st_mode), 0o600)
        (data / "реестр-клиентов.md").write_text("live state", encoding="utf-8")
        (runtime / "fixture-session.json").write_text("{\"state\":1}")
        (self.seed / "реестр-клиентов.md").write_text("new image stale state")
        (self.seed / "обучение.md").write_text("new image history")
        with patch.object(b, "own_new", wraps=b.own_new) as ownership:
            self.prepare()
        ownership.assert_not_called()
        self.assertEqual((data / "реестр-клиентов.md").read_text(), "live state")
        self.assertFalse((data / "обучение.md").exists())
        self.assertTrue((runtime / "fixture-session.json").exists())

    def test_seed_allowlist_rejects_credentials_symlinks_and_non_work_files(self):
        log = self.seed / "журнал"
        log.mkdir()
        (log / "work.md").write_text("fixture daily history")
        (log / "state.json").write_text('{"stage":"fixture"}')
        (log / "auth.json").write_text('{"access_token":"fixture-token-123456"}')
        (log / "env-secret.md").write_text("text fixture-private-value text")
        (log / "key.md").write_text("api_key=fixture-key-123456")
        (log / "binary.md").write_bytes(b"\xff\x00")
        (log / ".hidden.md").write_text("hidden")
        (log / "code.py").write_text("raise SystemExit(1)")
        (self.seed / ".env").write_text("SECRET=fixture")
        outside = self.root / "outside.md"
        outside.write_text("fixture outside")
        (log / "link.md").symlink_to(outside)
        data, _ = self.prepare(env={"EXAMPLE_SECRET": "fixture-private-value"})
        self.assertEqual(sorted(p.name for p in (data / "журнал").iterdir()), ["state.json", "work.md"])
        self.assertFalse((data / ".env").exists())

    def test_only_new_dedicated_volume_is_claimed(self):
        (self.volume / "existing.txt").write_text("unrelated")
        with self.assertRaisesRegex(b.StorageError, "dedicated_empty_volume_required"):
            self.prepare()
        self.assertFalse((self.volume / b.MARKER).exists())

    def test_mount_is_required_before_any_claim(self):
        with patch.object(b.os.path, "ismount", return_value=False):
            with self.assertRaisesRegex(b.StorageError, "persistent_mount_required"):
                self.prepare(require_mount=True)
        self.assertEqual(list(self.volume.iterdir()), [])

    def test_volume_symlink_is_rejected(self):
        alias = self.root / "alias"
        alias.symlink_to(self.volume, target_is_directory=True)
        with self.assertRaisesRegex(b.StorageError, "symlink_storage_path"):
            b.prepare_volume(alias, self.seed, self.uid, self.gid, require_mount=False)

    def test_marker_conflict_and_existing_permissions_are_not_repaired(self):
        data, runtime = self.prepare()
        data.chmod(0o755)
        with patch.object(b.os, "chown") as ownership:
            with self.assertRaisesRegex(b.StorageError, "existing_directory_not_owned"):
                self.prepare()
        ownership.assert_not_called()
        self.assertEqual(stat.S_IMODE(data.stat().st_mode), 0o755)
        data.chmod(0o700)
        marker = self.volume / b.MARKER
        state = json.loads(marker.read_text())
        state["role"] = "other-project"
        marker.write_text(json.dumps(state))
        with self.assertRaisesRegex(b.StorageError, "volume_marker_conflict"):
            self.prepare()

    def test_partial_initialization_never_overwrites_existing_file(self):
        data, _ = self.prepare()
        marker = self.volume / b.MARKER
        state = json.loads(marker.read_text())
        state["state"] = "initializing"
        marker.write_text(json.dumps(state))
        (data / "реестр-клиентов.md").write_text("already copied")
        self.prepare()
        self.assertEqual((data / "реестр-клиентов.md").read_text(), "already copied")

    def test_working_mapping_is_exact_and_never_removes_directory(self):
        data, runtime = self.prepare()
        anchor = self.root / "app-data"
        b.link_working_data(anchor, data)
        b.link_working_data(anchor, data)
        self.assertEqual(anchor.resolve(), data)
        with self.assertRaisesRegex(b.StorageError, "working_data_mapping_conflict"):
            b.link_working_data(anchor, runtime)
        real = self.root / "existing-work"
        real.mkdir()
        (real / "keep.md").write_text("keep")
        with self.assertRaisesRegex(b.StorageError, "working_data_mapping_conflict"):
            b.link_working_data(real, data)
        self.assertEqual((real / "keep.md").read_text(), "keep")

    def test_drop_privileges_order_and_nonroot_enforcement(self):
        order = []
        user = SimpleNamespace(pw_name="agent", pw_uid=1234, pw_gid=2345, pw_dir="/fixture/agent")
        with patch.object(b.os, "geteuid", side_effect=[0, 1234]), \
                patch.object(b.os, "getegid", return_value=2345), \
                patch.object(b.os, "initgroups", side_effect=lambda *a: order.append("groups")), \
                patch.object(b.os, "setgid", side_effect=lambda *a: order.append("gid")), \
                patch.object(b.os, "setuid", side_effect=lambda *a: order.append("uid")), \
                patch.dict(os.environ, {}, clear=True):
            b.drop_privileges(user)
            self.assertEqual(os.environ["HOME"], "/fixture/agent")
        self.assertEqual(order, ["groups", "gid", "uid"])

    def test_bootstrap_only_never_executes_business_code(self):
        user = SimpleNamespace(pw_name="agent", pw_uid=1234, pw_gid=2345, pw_dir="/fixture/agent")
        env = {"AGENT_SDK_PROVIDER": "codex", "SALES_RUNTIME_DIR": "/data/sales/runtime", "SALES_BOOTSTRAP_ONLY": "1"}
        with patch.dict(os.environ, env, clear=True), patch.object(b.pwd, "getpwnam", return_value=user), \
                patch.object(b.os, "geteuid", return_value=0), \
                patch.object(b, "prepare_volume", return_value=(Path("/data/sales/data"), Path("/data/sales/runtime"))), \
                patch.object(b, "link_working_data"), patch.object(b, "drop_privileges") as drop, \
                patch.object(b.os, "access", return_value=True), \
                patch.object(b.os, "execv") as execute:
            self.assertEqual(b.main(["вечер"]), 0)
        drop.assert_called_once_with(user)
        execute.assert_not_called()

    def test_agent_mount_access_is_checked_after_drop(self):
        user = SimpleNamespace(pw_name="agent", pw_uid=1234, pw_gid=2345, pw_dir="/fixture/agent")
        events = []
        env = {"AGENT_SDK_PROVIDER": "codex", "SALES_RUNTIME_DIR": "/data/sales/runtime", "SALES_BOOTSTRAP_ONLY": "1"}
        with patch.dict(os.environ, env, clear=True), patch.object(b.pwd, "getpwnam", return_value=user), \
                patch.object(b.os, "geteuid", return_value=0), \
                patch.object(b, "prepare_volume", return_value=(Path("/data/sales/data"), Path("/data/sales/runtime"))), \
                patch.object(b, "link_working_data"), \
                patch.object(b, "drop_privileges", side_effect=lambda *a: events.append("drop")), \
                patch.object(b.os, "access", side_effect=lambda *a: events.append("access") or False), \
                patch.object(b.os, "execv") as execute:
            self.assertEqual(b.main(["вечер"]), 2)
        self.assertEqual(events, ["drop", "access"])
        execute.assert_not_called()

    def test_only_webhook_can_keep_preflight_alive_without_business_code(self):
        user = SimpleNamespace(pw_name="agent", pw_uid=1234, pw_gid=2345)
        env = {"AGENT_SDK_PROVIDER": "codex", "SALES_RUNTIME_DIR": "/data/sales/runtime",
               "SALES_BOOTSTRAP_ONLY": "1", "SALES_PREFLIGHT_KEEPALIVE": "1", "PORT": "8123"}
        for role in ("вебхук", "утро", "вечер"):
            with self.subTest(role=role), patch.dict(os.environ, env, clear=True), \
                    patch.object(b.pwd, "getpwnam", return_value=user), \
                    patch.object(b.os, "geteuid", return_value=0), \
                    patch.object(b, "prepare_volume", return_value=(Path("/data/sales/data"), Path("/data/sales/runtime"))), \
                    patch.object(b, "link_working_data"), patch.object(b, "drop_privileges"), \
                    patch.object(b.os, "access", return_value=True), \
                    patch.object(b, "preflight_server") as hold, patch.object(b.os, "execv") as execute:
                self.assertEqual(b.main([role]), 0)
                if role == "вебхук":
                    hold.assert_called_once_with(8123)
                else:
                    hold.assert_not_called()
                execute.assert_not_called()

    def test_preflight_http_never_acknowledges_webhooks(self):
        import threading
        from http.server import HTTPServer
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError
        server = HTTPServer(("127.0.0.1", 0), object)
        port = server.server_port
        server.server_close()
        # Capture the server class configured by preflight_server; no external network.
        with patch("http.server.HTTPServer") as factory:
            b.preflight_server(port)
        handler = factory.call_args.args[1]
        with HTTPServer(("127.0.0.1", 0), handler) as actual:
            thread = threading.Thread(target=actual.serve_forever, daemon=True)
            thread.start()
            origin = "http://127.0.0.1:" + str(actual.server_port)
            try:
                with urlopen(origin + "/health") as response:
                    self.assertEqual(json.load(response)["business_code_started"], False)
                with self.assertRaises(HTTPError) as denied:
                    urlopen(Request(origin + "/avito/fixture", data=b"{}"))
                self.assertEqual(denied.exception.code, 503)
            finally:
                actual.shutdown()
                thread.join(timeout=5)

    def test_codex_missing_durable_path_fails_before_business_or_mount_mutation(self):
        user = SimpleNamespace(pw_name="agent", pw_uid=1234, pw_gid=2345)
        with patch.dict(os.environ, {"AGENT_SDK_PROVIDER": "codex"}, clear=True), \
                patch.object(b.pwd, "getpwnam", return_value=user), patch.object(b.os, "geteuid", return_value=0), \
                patch.object(b, "prepare_volume") as prepare, patch.object(b.os, "execv") as execute:
            self.assertEqual(b.main(["вечер"]), 2)
        prepare.assert_not_called()
        execute.assert_not_called()

    def test_cron_gate_uses_moscow_midnight_and_does_not_hold_webhook(self):
        env = {"AGENT_SDK_PROVIDER": "codex", "SALES_CRON_START_DATE": "2026-10-07"}
        before = datetime(2026, 10, 6, 20, 59, tzinfo=timezone.utc)
        after = datetime(2026, 10, 6, 21, 0, tzinfo=timezone.utc)
        for role in ("утро", "вечер"):
            self.assertTrue(b.cron_held([role], env, before))
            self.assertFalse(b.cron_held([role], env, after))
        self.assertFalse(b.cron_held(["вебхук"], env, before))
        self.assertFalse(b.cron_held(["доставка-отчёта"], env, before))
        with self.assertRaisesRegex(b.StorageError, "cron_start_date_required"):
            b.cron_held(["вечер"], {"AGENT_SDK_PROVIDER": "codex"}, before)

    def test_cron_schedule_guard_does_not_send_on_nightly_deployment(self):
        env = {"AGENT_SDK_PROVIDER": "codex", "SALES_CRON_START_DATE": "2026-10-09",
               "SALES_CRON_SCHEDULE_GUARD": "1"}
        self.assertTrue(b.cron_held(["утро"], env, datetime(2026, 10, 10, 0, 1, tzinfo=timezone.utc)))
        self.assertTrue(b.cron_held(["вечер"], env, datetime(2026, 10, 9, 20, 59, tzinfo=timezone.utc)))
        self.assertFalse(b.cron_held(["утро"], env, datetime(2026, 10, 10, 6, 30, tzinfo=timezone.utc)))
        self.assertFalse(b.cron_held(["вечер"], env, datetime(2026, 10, 10, 15, 35, tzinfo=timezone.utc)))
        self.assertTrue(b.cron_held(["утро"], env, datetime(2026, 10, 10, 6, 51, tzinfo=timezone.utc)))
        self.assertFalse(b.cron_held(["вебхук"], env, datetime(2026, 10, 10, 0, 1, tzinfo=timezone.utc)))


class FilesystemContracts(unittest.TestCase):
    def setUp(self):
        try:
            import openai_sdk
        except ModuleNotFoundError:
            self.skipTest("Full sales runtime dependencies required")
        self.files = openai_sdk
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        self.app, self.base = root / "app", root / "sales"
        self.app.mkdir()
        self.base.mkdir()
        self.data, self.runtime = self.base / "data", self.base / "runtime"
        self.data.mkdir()
        self.runtime.mkdir()
        (self.app / "данные").symlink_to(self.data, target_is_directory=True)
        for name, value in (("КОРЕНЬ", self.app), ("DURABLE_DATA", self.data),
                            ("INFRASTRUCTURE_ROOT", self.base), ("БАЗА_ЗНАНИЙ", self.base)):
            context = patch.object(self.files, name, value)
            context.start()
            self.addCleanup(context.stop)

    def test_exact_mapping_allows_only_data_not_runtime_or_auth(self):
        self.files._write({"file_path": "данные/work.md", "content": "new work"})
        self.assertEqual((self.data / "work.md").read_text(), "new work")
        for target in (self.runtime / "ledger.md", self.base / "marker.json", self.data / "auth.json",
                       self.data / ".env", self.data / ".codex/auth.json"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                self.files._разрешённый_путь(str(target))

    def test_nested_symlink_cannot_reach_runtime_in_read_search_or_write(self):
        secret = self.runtime / "ledger.md"
        secret.write_text("fixture ledger")
        (self.data / "link.md").symlink_to(secret)
        self.assertEqual(self.files._glob({"path": str(self.data), "pattern": "*.md"}), [])
        self.assertEqual(self.files._grep({"path": str(self.data), "pattern": "fixture", "glob": "*.md"}), [])
        with self.assertRaises(ValueError):
            self.files._write({"file_path": str(self.data / "link.md"), "content": "bad"})
        self.assertEqual(secret.read_text(), "fixture ledger")

    def test_preexisting_tmp_symlink_is_never_followed_by_write(self):
        secret = self.runtime / "ledger.md"
        secret.write_text("keep")
        (self.data / "work.md.tmp").symlink_to(secret)
        self.files._write({"file_path": "данные/work.md", "content": "work"})
        self.assertEqual(secret.read_text(), "keep")
        self.assertEqual((self.data / "work.md").read_text(), "work")

    def test_arbitrary_data_anchor_mapping_is_rejected(self):
        anchor = self.app / "данные"
        anchor.unlink()
        anchor.symlink_to(self.runtime, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "отображение"):
            self.files._разрешённый_путь(str(self.runtime / "file.md"))


if __name__ == "__main__":
    unittest.main()
