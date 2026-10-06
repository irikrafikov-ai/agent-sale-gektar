"""Офлайн: свободные/проданные/брони и недоступность источника, без API."""
import unittest
from unittest.mock import patch
import инвентарь as inventory

CSV = "Участок,Статус,Площадь,Цена\n2,Свободен,8,320000\n3,Продан,7.91,310072\n4,Акция,8,310000\n5,Свободен,8,320000\n36,Забронирован,7.7,346500\n"
CAB = {"шахматка": "https://example.invalid/test.csv"}


class InventoryTests(unittest.TestCase):
    def check(self, text, expected):
        with patch.object(inventory, "загрузить", return_value=inventory.разобрать_csv(CSV)):
            self.assertEqual(inventory.проверить_предложение(text, CAB) is not None, expected, text)

    def test_sold_and_reserved_cannot_be_offered(self):
        for text in ("№3 свободен.", "Предлагаю участок 3.", "Подойдёт №36?", "№36 доступен к покупке.",
                     "Выбирайте №3 или №4", "Могу предложить участки 4, 36.", "№3 не продан, можно забронировать.",
                     "№3 продан, но можем его забронировать.", "№36 свободен, а №3 продан."):
            with self.subTest(text=text):
                self.check(text, True)

    def test_truthful_status_statements_are_allowed(self):
        for text in ("№3 уже продан.", "Участок 36 забронирован.", "№3 продан, №4 свободен.",
                     "№3 продан; предлагаю №4.", "Продан №3, свободен №5.", "№36 на броне.",
                     "№3 и №36 недоступны.", "№3 не предлагаю.", "№3 не свободен."):
            with self.subTest(text=text):
                self.check(text, False)

    def test_free_and_promo_can_be_offered(self):
        self.check("Предлагаю №4 или №5.", False)
        self.check("Свободны №4–5.", False)

    def test_number_ranges_do_not_hide_sold_parcels(self):
        for text in ("Свободны №2–4.", "Предлагаю участки с 2 по 4.", "Можно выбрать №2 до №4."):
            with self.subTest(text=text):
                self.check(text, True)

    def test_wrong_unavailable_status_is_not_accepted(self):
        self.check("№3 забронирован.", True)
        self.check("№36 продан.", True)

    def test_unknown_number_is_not_invented(self):
        self.check("№39 свободен", True)

    def test_unrelated_messages_do_not_query_inventory(self):
        for text in ("Добрый день!", "Держите тур https://example.invalid/3/", "Участок 7,91 сотки за 310000 ₽.",
                     "Участок 2 га", "Участок 310000 ₽", "Созвонимся в 15:30?"):
            with self.subTest(text=text), patch.object(inventory, "загрузить", side_effect=AssertionError("Must not fetch")):
                self.assertIsNone(inventory.проверить_предложение(text, CAB))

    def test_source_failure_blocks_specific_offer(self):
        for text in ("№3 свободен", "Подберём №4 или №5?", "Могу предложить участок 36"):
            with self.subTest(text=text), patch.object(inventory, "загрузить", side_effect=inventory.ОшибкаШахматки("offline")):
                self.assertIsNotNone(inventory.проверить_предложение(text, CAB))

    def test_source_failure_does_not_block_non_offer(self):
        with patch.object(inventory, "загрузить", side_effect=inventory.ОшибкаШахматки("offline")):
            self.assertIsNone(inventory.проверить_предложение("№3 продан.", CAB))

    def test_malformed_csv_is_not_silently_accepted(self):
        for bad in ("bad", CSV + "3,Свободен,7,1\n", CSV + "6,Неизвестно,7,1\n"):
            with self.subTest(csv=bad), self.assertRaises(inventory.ОшибкаШахматки):
                inventory.разобрать_csv(bad)

    def test_http_error_never_falls_back_to_stale_cache(self):
        inventory._кэш[CAB["шахматка"]] = (0, inventory.разобрать_csv(CSV))
        with patch.object(inventory, "urlopen", side_effect=OSError("offline")), self.assertRaises(inventory.ОшибкаШахматки):
            inventory.загрузить(CAB, свежая=True)


if __name__ == "__main__":
    unittest.main()
