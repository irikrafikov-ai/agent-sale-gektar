"""Офлайн-регрессии аудита 06.10.2026: реальные сетевые клиенты не создаются."""
import asyncio
import ast
import json
import os
from pathlib import Path
import sys
import time
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
http = types.ModuleType("httpx")
http.Client = object
http.HTTPError = Exception
http.Timeout = lambda *a, **k: None
sys.modules["httpx"] = http
sdk = types.ModuleType("claude_agent_sdk")
sdk.create_sdk_mcp_server = lambda *a, **k: None
sdk.tool = lambda *a, **k: lambda f: f
sys.modules["claude_agent_sdk"] = sdk

import инструменты as tools
from интеграции.avito import Avito


class BitrixFake:
    def __init__(self, stage="UC_CONTACT", category=0, broken=False):
        self.stage, self.category, self.broken = stage, category, broken
        self.writes = []

    def crm_get(self, *args):
        if self.broken:
            raise RuntimeError("offline")
        return {"STAGE_ID": self.stage, "CATEGORY_ID": self.category}

    def crm_update(self, entity, ident, fields):
        self.writes.append(fields)
        return True

    def call(self, *args):
        raise AssertionError("Direct REST must not bypass the update guard")


class AvitoFake:
    user_id = 84069402

    def __init__(self, texts=("Расскажите подробнее",), fail_call=0):
        self.messages = [{"id": str(n), "author_id": 987654321,
                          "created": time.time() - 100 + n,
                          "content": {"text": t}, "type": "text"}
                         for n, t in enumerate(texts)]
        self.fail_call, self.calls, self.sent = fail_call, 0, []

    def chat_messages(self, *args, **kw):
        self.calls += 1
        if self.calls == self.fail_call:
            raise RuntimeError("offline")
        return self.messages

    def send_message(self, chat_id, text):
        self.sent.append(text)
        return {"id": "sent"}


class AuditRegression(unittest.TestCase):
    def update(self, backend, stage, generic=False):
        args = {"entity": "deal", "id": "42", "fields": json.dumps({"STAGE_ID": stage})}
        with patch.object(tools, "bitrix", return_value=backend):
            if generic:
                return asyncio.run(tools.b24_call({"method": "crm.deal.update", "params": json.dumps({"id": 42, "fields": {"STAGE_ID": stage}})}))
            return asyncio.run(tools.b24_crm_update(args))

    def send(self, client):
        with patch.object(tools, "MODE", "send"), patch.object(tools, "_avito", return_value=client), \
                patch.object(tools, "_темп_позволяет", return_value=True), \
                patch.object(tools.отказники, "запрещён", return_value=None), \
                patch.object(tools.отказники, "добавить", return_value=True) as registry:
            result = asyncio.run(tools.avito_send_message({"chat_id": "test", "text": "Добрый день 🙂 Расскажу подробнее."}))
            return result, registry.call_count

    def test_no_sales_stage_regression(self):
        for old in ("UC_CONTACT", "UC_CALL_DONE", "UC_MEET_SET", "UC_MEET_DONE", "FINAL_INVOICE", "UC_DOGOVOR", "UC_RASSROCHKA", "WON"):
            for new in ("NEW", "EXECUTING", "PREPARATION"):
                with self.subTest(old=old, new=new):
                    b = BitrixFake(old)
                    self.assertTrue(self.update(b, new).get("is_error"))
                    self.assertEqual(b.writes, [])

    def test_regular_forward_stage_is_allowed(self):
        b = BitrixFake("EXECUTING")
        self.assertFalse(self.update(b, "PREPARATION").get("is_error"))
        self.assertEqual(b.writes, [{"STAGE_ID": "PREPARATION"}])

    def test_stage_read_failure_does_not_write(self):
        b = BitrixFake(broken=True)
        self.assertTrue(self.update(b, "EXECUTING").get("is_error"))
        self.assertEqual(b.writes, [])

    def test_generic_rest_update_uses_same_guard(self):
        b = BitrixFake()
        self.assertTrue(self.update(b, "EXECUTING", generic=True).get("is_error"))
        self.assertEqual(b.writes, [])

    def test_other_pipeline_is_not_reinterpreted(self):
        b = BitrixFake(category=1)
        self.assertFalse(self.update(b, "EXECUTING").get("is_error"))
        self.assertEqual(len(b.writes), 1)

    def test_no_send_if_history_cannot_be_checked(self):
        for fail in (1, 2):
            with self.subTest(failed_read=fail):
                c = AvitoFake(fail_call=fail)
                result, _ = self.send(c)
                self.assertTrue(result.get("is_error"))
                self.assertEqual(c.sent, [])

    def test_soft_refusal_does_not_create_permanent_blacklist(self):
        c = AvitoFake(("Не интересно",))
        result, persisted = self.send(c)
        self.assertTrue(result.get("is_error"))
        self.assertEqual(c.sent, [])
        self.assertEqual(persisted, 0)

    def test_returning_customer_after_soft_refusal_can_get_answer(self):
        c = AvitoFake(("Не интересно", "А какая площадь?"))
        result, persisted = self.send(c)
        self.assertFalse(result.get("is_error"))
        self.assertEqual(len(c.sent), 1)
        self.assertEqual(persisted, 0)

    def test_explicit_optout_is_still_permanent(self):
        c = AvitoFake(("Не пишите больше", "Уже всё сказал"))
        result, persisted = self.send(c)
        self.assertTrue(result.get("is_error"))
        self.assertEqual(c.sent, [])
        self.assertEqual(persisted, 1)

    def test_list_and_object_message_responses(self):
        messages = [{"id": "synthetic"}]
        c = Avito.__new__(Avito)
        c._user_id = 84069402
        for payload in (messages, {"messages": messages}):
            with self.subTest(response_type=type(payload).__name__):
                c._request = lambda *a, **k: payload
                self.assertEqual(c.chat_messages("test"), messages)

    def test_template_does_not_send_when_history_read_fails(self):
        # Выполняем настоящий обработчик, но без импорта HTTP-приложения.
        tree = ast.parse((Path(__file__).parent / "вебхук.py").read_text())
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "отправить_шаблон")
        c = AvitoFake(fail_call=1)
        scope = {"имя_клиента": lambda *a: "", "темп_позволяет": lambda: True,
                 "клиент_кабинета": lambda *a: c, "лог": lambda *a: None}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "template-test", "exec"), scope)
        scope["отправить_шаблон"]("test", {"шаблон": "Template"})
        self.assertEqual(c.sent, [])


if __name__ == "__main__":
    unittest.main()
