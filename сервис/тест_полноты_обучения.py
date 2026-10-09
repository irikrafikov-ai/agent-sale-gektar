"""Offline-only regression: no business clients or network are imported."""
import contextlib
import importlib.util
import io
import os
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
SOURCE = Path(os.environ.get("SALES_LEARNING_SOURCE", str(ROOT / "обучение.py")))
package = types.ModuleType("интеграции")
bitrix = types.ModuleType("интеграции.bitrix")
bitrix.Bitrix = object
sys.modules["интеграции"] = package
sys.modules["интеграции.bitrix"] = bitrix
spec = importlib.util.spec_from_file_location("learning_completeness_subject", SOURCE)
subject = importlib.util.module_from_spec(spec)
spec.loader.exec_module(subject)


class FakeBitrix:
    def __init__(self, pages):
        self.pages = list(pages)
        self.writes = []
        self.reads = 0

    def call(self, method, params):
        assert method == "crm.timeline.comment.list"
        self.reads += 1
        result = self.pages.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def timeline_comment_add(self, entity, text):
        self.writes.append((entity, text))

    def close(self):
        pass


def row(number=8, age_days=0):
    return {"ID": str(number), "COMMENT": subject.строка_гипотезы(
        number, "проверяю", "synthetic", "offline fixture", "fixture metric",
        time.time() - age_days * 86400)}


class CompletenessTests(unittest.TestCase):
    def save(self, pages, text=None):
        api = FakeBitrix(pages)
        if text is None:
            text = "#ГИПОТЕЗА 999 проверяю: new fixture | offline | count"
        with patch.object(subject, "Bitrix", return_value=api), contextlib.redirect_stderr(io.StringIO()):
            result = subject.сохранить_из_вывода(text)
        return api, result

    def test_read_failure_does_not_write(self):
        api, result = self.save([RuntimeError("synthetic read failure")])
        self.assertEqual(api.writes, [])
        self.assertEqual(result["записано"], 0)
        self.assertFalse(result["полнота"])

    def test_partial_page_failure_does_not_write(self):
        api, result = self.save([[row(n) for n in range(1, 51)], RuntimeError("page two")])
        self.assertEqual(api.reads, 2)
        self.assertEqual(api.writes, [])
        self.assertFalse(result["полнота"])

    def test_invalid_page_does_not_write(self):
        for invalid in [None, {}, "invalid", ["not a record"]]:
            with self.subTest(invalid=type(invalid).__name__):
                api, result = self.save([invalid])
                self.assertEqual(api.writes, [])
                self.assertFalse(result["полнота"])

    def test_safety_limit_is_not_complete_history(self):
        api, result = self.save([[row(n) for n in range(i*50+1, i*50+51)] for i in range(12)])
        self.assertEqual(api.reads, 12)
        self.assertEqual(api.writes, [])
        self.assertFalse(result["полнота"])

    def test_real_empty_journal_allows_first_id(self):
        api, result = self.save([[]])
        self.assertEqual(len(api.writes), 1)
        self.assertEqual(subject.разобрать(api.writes[0][1])["номер"], 1)
        self.assertTrue(result["полнота"])

    def test_existing_id_gets_next_id(self):
        api, result = self.save([[row(8)]])
        self.assertEqual(subject.разобрать(api.writes[0][1])["номер"], 9)
        self.assertEqual(result["записано"], 1)

    def test_past_id_remains_reserved(self):
        api, result = self.save([[row(12, age_days=90)]])
        self.assertEqual(subject.разобрать(api.writes[0][1])["номер"], 13)
        self.assertEqual(result["записано"], 1)

    def test_existing_hypothesis_status_keeps_id(self):
        api, result = self.save([[row(8)]], "#ГИПОТЕЗА 8 снята: fixture | offline | count")
        parsed = subject.разобрать(api.writes[0][1])
        self.assertEqual((parsed["номер"], parsed["статус"]), (8, "снята"))

    def test_two_complete_pages(self):
        api, result = self.save([[row(n) for n in range(1, 51)], [row(72)]])
        self.assertEqual(subject.разобрать(api.writes[0][1])["номер"], 73)
        self.assertTrue(result["полнота"])

    def test_lessons_are_not_written_on_read_failure(self):
        api, result = self.save([RuntimeError("unavailable")], "#УРОК synthetic lesson")
        self.assertEqual(api.writes, [])
        self.assertEqual(result["уроки"], [])

    def test_brief_continues_without_failed_history(self):
        api = FakeBitrix([RuntimeError("unavailable")])
        with patch.object(subject, "Bitrix", return_value=api), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(subject.для_брифа(), "")
        self.assertEqual(api.writes, [])

    def test_report_distinguishes_unknown_from_empty(self):
        api = FakeBitrix([RuntimeError("unavailable")])
        with patch.object(subject, "Bitrix", return_value=api), contextlib.redirect_stderr(io.StringIO()):
            result = subject.для_вывода()
        self.assertIn("не прочитан полностью", result)
        self.assertNotIn("сегодня заводится первая", result)

    def test_error_log_does_not_reveal_exception_details(self):
        api = FakeBitrix([RuntimeError("synthetic-secret-marker")])
        output = io.StringIO()
        with patch.object(subject, "Bitrix", return_value=api), contextlib.redirect_stderr(output):
            subject.прочитать()
        self.assertNotIn("synthetic-secret-marker", output.getvalue())
        self.assertIn("RuntimeError", output.getvalue())


if __name__ == "__main__":
    unittest.main()
