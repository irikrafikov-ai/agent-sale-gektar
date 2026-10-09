"""Офлайн: источник профиля, исключение своего кабинета и ссылки в отчёте."""

import asyncio
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import отчёт
from ссылки_авито import профиль_клиента, профиль_для_алерта

URL = "https://avito.ru/user/customer-hash/profile"


def чат(url=URL, profile_id=22):
    return {"id": "u2i-test", "users": [
        {"id": 11, "name": "Наш кабинет", "public_user_profile":
         {"user_id": 11, "url": "https://avito.ru/user/owner/profile"}},
        {"id": 22, "name": "Клиент", "public_user_profile":
         {"user_id": profile_id, "url": url}},
    ]}


class Профили(unittest.TestCase):
    def test_source_and_tracking(self):
        self.assertEqual(профиль_клиента(чат(URL + "?iid=123&src=messenger#x"), 11), URL)

    def test_unknown_owner(self):
        for value in (None, "", "unknown", 0):
            with self.subTest(value=value):
                self.assertEqual(профиль_клиента(чат(), value), "")

    def test_owner_only_and_ambiguous(self):
        data = чат()
        data["users"].pop()
        self.assertEqual(профиль_клиента(data, 11), "")
        data = чат()
        data["users"].append({"id": 33, "public_user_profile": {"url": URL}})
        self.assertEqual(профиль_клиента(data, 11), "")

    def test_mismatched_profile(self):
        self.assertEqual(профиль_клиента(чат(profile_id=11), 11), "")

    def test_unsafe_urls(self):
        for url in ("https://example.org/user/x", "https://avito.ru.evil/user/x",
                    "http://avito.ru/user/x", "https://secret@avito.ru/user/x",
                    "https://avito.ru:444/user/x", "https://avito.ru/items/x",
                    URL + ")", URL + "\n", "https://avito.ru:bad/user/x"):
            with self.subTest(url=url):
                self.assertEqual(профиль_клиента(чат(url), 11), "")

    def test_missing_and_malformed_metadata(self):
        for data in ({}, {"users": {}}, {"users": [None]},
                     {"users": [{"id": 22, "public_user_profile": "bad"}]}):
            self.assertEqual(профиль_клиента(data, 11), "")

    def test_alert_success_and_read_failure(self):
        class Client:
            user_id = 11

            def chat_info(self, chat_id):
                return чат()

        self.assertIn(URL, профиль_для_алерта(Client(), "u2i-test"))
        with patch.object(Client, "chat_info", side_effect=RuntimeError("private error")):
            self.assertEqual(профиль_для_алерта(Client(), "u2i-test"), "профиль Авито не получен")

    def test_diary_includes_url_and_exact_chat(self):
        diary = {"дневник": [{"имя": "Клиент", "chat_id": "u2i-test", "кабинет": "gektar",
                 "профиль_авито": URL, "ответил_в_окне": True, "молчит_часов": 0,
                 "клиент": "Удобно завтра", "наше": "Когда удобно?", "последний_наш": False}]}
        text = отчёт.дневник_словами(diary)
        self.assertIn(URL, text)
        self.assertIn("u2i-test", text)

    def test_all_warm_sections(self):
        now = datetime.now(отчёт.МСК)
        card = {"ID": "1", "TITLE": "Клиент", "ORIGIN_ID": "u2i-test",
                "кабинет": "Щёкинские берега", "профиль_авито": URL}
        data = {"начало": now - timedelta(days=1), "конец": now, "причины": {},
                "по_кабинетам": [], "диалогов": 0, "с_ответом": 0, "отправлено": 0,
                "заинтересовались": 0, "отказали": 0, "новые_тёплые": [card],
                "ранее_тёплые": [dict(card, MOVED_TIME=now.isoformat()),
                                 dict(card, MOVED_TIME=(now - timedelta(days=40)).isoformat())]}
        with patch.object(отчёт.учёт_расхода, "словами", return_value="Расход не проверяется"):
            text = отчёт.шапка(data)
        self.assertEqual(text.count(f"[Профиль Авито]({URL})"), 3)
        self.assertEqual(text.count("u2i-test"), 3)
        data["новые_тёплые"] = [{"TITLE": "Без связи", "ID": "2"}]
        data["ранее_тёплые"] = []
        with patch.object(отчёт.учёт_расхода, "словами", return_value=""):
            self.assertIn("профиль Авито не получен", отчёт.шапка(data))

    def test_report_matches_exact_origin_not_name(self):
        snapshot = {"новые_тёплые": [{"TITLE": "Одно имя", "ORIGIN_ID": "u2i-test"},
                                    {"TITLE": "Одно имя", "ORIGIN_ID": "missing"}],
                    "ранее_тёплые": []}
        account = {"кабинет": "Щёкинские берега", "профили_чатов": {"u2i-test": URL},
                   "диалогов": 0, "с_ответом": 0, "отправлено": 0}
        with patch.object(отчёт.реестр, "активные", return_value=["gektar"]), \
             patch.object(отчёт, "диалоги_кабинета", return_value=account), \
             patch.object(отчёт, "Bitrix"), \
             patch.object(отчёт, "_битрикс_срез", return_value=snapshot), \
             patch.object(отчёт.учёт_расхода, "за_период", return_value={}), \
             patch.object(отчёт.учёт_расхода, "за_месяц", return_value={}), \
             patch.object(отчёт.учёт_расхода, "молчания_за_период", return_value=[]):
            result = отчёт.собрать()
        self.assertEqual(result["новые_тёплые"][0]["профиль_авито"], URL)
        self.assertNotIn("профиль_авито", result["новые_тёплые"][1])

    def test_real_alert_handler_and_client_failure(self):
        import инструменты
        client = Mock(user_id=11)
        client.chat_info.return_value = чат()
        sender = Mock()
        handler = инструменты.ОБРАБОТЧИКИ["telegram_alert"]["handler"]
        with patch.object(инструменты, "_лид_без_клиента", return_value=""), \
             patch.object(инструменты, "_похоже_на_лид", return_value=True), \
             patch.object(инструменты, "_уже_уведомляли", return_value=False), \
             patch.object(инструменты, "_запомнить_уведомление"), \
             patch.object(инструменты, "_avito", return_value=client) as factory, \
             patch.object(инструменты, "telegram", return_value=sender):
            asyncio.run(handler({"chat_id": "u2i-test", "text": "Тёплый лид"}))
            text = sender.send.call_args.args[0]
            self.assertIn(URL, text)
            self.assertIn("Кабинет: gektar", text)
            factory.side_effect = RuntimeError("private error")
            asyncio.run(handler({"chat_id": "u2i-test", "text": "Тёплый лид"}))
            self.assertIn("профиль Авито не получен", sender.send.call_args.args[0])
            self.assertNotIn("private error", sender.send.call_args.args[0])

    def test_webhook_alert_format(self):
        import вебхук
        client = Mock(user_id=11)
        client.chat_info.return_value = чат()
        sender = Mock()
        with patch.object(вебхук, "имя_клиента", return_value="Клиент"), \
             patch.object(вебхук, "клиент_кабинета", return_value=client), \
             patch.object(вебхук, "telegram", return_value=sender), \
             patch.object(вебхук, "лог"):
            вебхук.сообщить_о_тёплом("u2i-test", "fixture", {"название": "Щёкинские берега"})
        self.assertIn(URL, sender.send.call_args.args[0])
        self.assertIn("u2i-test", sender.send.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
