"""Offline webhook event identity regressions; no client or CRM requests."""
from collections import deque
from contextlib import ExitStack
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent))
import codex_sdk
import вебхук


class EventIdentity(unittest.TestCase):
    account = {"ключ": "gektar", "название": "fixture", "avito_user_id": 999}

    def fixtures(self, client):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(вебхук.провайдер, "выбран", return_value="codex"))
        stack.enter_context(patch.object(вебхук, "РЕЖИМ", "send"))
        stack.enter_context(patch.object(вебхук, "клиент_кабинета", return_value=client))
        stack.enter_context(patch.object(вебхук, "имя_клиента", return_value="fixture"))
        stack.enter_context(patch.object(вебхук, "лог"))
        crm = stack.enter_context(patch.object(вебхук, "обеспечить_сделку"))
        process = SimpleNamespace(wait=lambda **kwargs: 0)
        start = stack.enter_context(patch("subprocess.Popen", return_value=process))
        return stack, crm, start

    def expected(self, event):
        return "sales:chat:" + codex_sdk.digest(["gektar", "fixture-chat", event])

    def test_initial_incoming_id_survives_new_outgoing_between_reads(self):
        incoming = {"id": "incoming-original", "direction": "in", "author_id": 123,
                    "type": "text", "content": {"text": "Сколько стоит участок?"}}
        outgoing = {"id": "outgoing-after-send", "direction": "out", "author_id": 999}
        client = SimpleNamespace(user_id=999, chat_info=lambda *args: {},
                                 chat_messages=Mock(side_effect=[[incoming], [outgoing]]))
        stack, crm, start = self.fixtures(client)
        stack.enter_context(patch.object(вебхук.реестр, "настроен", return_value=True))
        stack.enter_context(patch.object(вебхук.реестр, "партнёрский_чат", return_value=False))
        stack.enter_context(patch.object(вебхук, "_обработанные", deque(maxlen=10)))
        stack.enter_context(patch.object(вебхук, "зеркалить_сообщение"))
        stack.enter_context(patch.object(вебхук, "след_шаблона", return_value="fixture-template"))
        stack.enter_context(patch.object(вебхук, "главный_чат", return_value=None))
        вебхук.разобрать_синхронно("fixture-chat", self.account)
        self.assertEqual(client.chat_messages.call_count, 1)
        self.assertEqual(start.call_args.kwargs["env"]["CODEX_OPERATION_KEY"], self.expected("incoming-original"))
        crm.assert_called_once()

    def test_explicit_event_does_not_reread_latest_message(self):
        client = SimpleNamespace(chat_messages=Mock(side_effect=AssertionError("unexpected history read")))
        _, crm, start = self.fixtures(client)
        вебхук.разбудить_агента("fixture-chat", "fixture", self.account, событие_id="incoming-original")
        client.chat_messages.assert_not_called()
        self.assertEqual(start.call_args.kwargs["env"]["CODEX_OPERATION_KEY"], self.expected("incoming-original"))
        crm.assert_called_once()

    def test_direct_fallback_rejects_outgoing_or_ambiguous_before_crm(self):
        examples = [[], [{"id": "event"}], [{"id": "event", "direction": "out", "author_id": 123}],
                    [{"id": "event", "author_id": 999}], [{"direction": "in", "author_id": 123}],
                    [{"id": "event", "author_id": 0}], [{"id": "event", "author_id": 1}],
                    [{"id": "event", "direction": "unknown", "author_id": 123}]]
        for messages in examples:
            with self.subTest(messages=messages):
                client = SimpleNamespace(user_id=999, chat_messages=Mock(return_value=messages))
                stack, crm, start = self.fixtures(client)
                with self.assertRaisesRegex(codex_sdk.CodexError, "chat_operation_identity_unavailable"):
                    вебхук.разбудить_агента("fixture-chat", "fixture", self.account)
                crm.assert_not_called()
                start.assert_not_called()
                stack.close()

    def test_direct_fallback_accepts_known_incoming_and_system_identity(self):
        for message in ({"id": "event", "direction": "in"},
                        {"id": "event", "author_id": 123},
                        {"id": "event", "author_id": 1, "type": "system"}):
            with self.subTest(message=message):
                client = SimpleNamespace(user_id=999, chat_messages=Mock(return_value=[message]))
                stack, _, start = self.fixtures(client)
                вебхук.разбудить_агента("fixture-chat", "fixture", self.account)
                self.assertEqual(start.call_args.kwargs["env"]["CODEX_OPERATION_KEY"], self.expected("event"))
                stack.close()

    def test_template_fallback_passes_original_event_identity(self):
        with patch.object(вебхук, "разбудить_агента") as start, patch.object(вебхук, "лог"):
            вебхук.отправить_шаблон("fixture-chat", dict(self.account, шаблон=""), событие_id="original-system")
        self.assertEqual(start.call_args.kwargs["событие_id"], "original-system")


if __name__ == "__main__":
    unittest.main()
