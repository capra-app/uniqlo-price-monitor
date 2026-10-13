import os
import unittest
from unittest.mock import patch

import monitor


class MonitorLogicTests(unittest.TestCase):
    def test_parse_target_sizes_from_env(self):
        with patch.dict(os.environ, {"TARGET_SIZES": "s, m ,32"}):
            self.assertEqual(monitor.parse_target_sizes(), ["S", "M", "32"])

    def test_parse_searches_default(self):
        with patch.dict(os.environ, {"SEARCH_QUERIES": ""}, clear=False):
            searches = monitor.parse_searches()
            self.assertEqual(searches, monitor.DEFAULT_SEARCHES)

    def test_parse_searches_custom(self):
        with patch.dict(os.environ, {"SEARCH_QUERIES": "U Line:uniqlo u,C Line:uniqlo c"}):
            self.assertEqual(
                monitor.parse_searches(),
                [("U Line", "uniqlo u"), ("C Line", "uniqlo c")],
            )

    def test_is_men_item(self):
        self.assertTrue(monitor.is_men_item({"genderName": "MEN"}))
        self.assertFalse(monitor.is_men_item({"genderName": "WOMEN"}))
        self.assertTrue(
            monitor.is_men_item({"genderName": "UNISEX", "sizeGender": "MEN"})
        )
        self.assertFalse(
            monitor.is_men_item({"genderName": "UNISEX", "sizeGender": "WOMEN"})
        )

    def test_normalize_size(self):
        self.assertEqual(monitor.normalize_size("M (US)"), "M")
        self.assertEqual(monitor.normalize_size("30 INCH"), "30")

    def test_variant_on_sale(self):
        variant = {"flags": {"priceFlags": [{"code": "discount"}]}}
        self.assertTrue(monitor.variant_on_sale(variant, 49.9, 49.9))
        self.assertTrue(monitor.variant_on_sale({}, 49.9, 29.9))
        self.assertFalse(monitor.variant_on_sale({}, 49.9, 49.9))

    def test_should_alert(self):
        self.assertTrue(monitor.should_alert(True, None, 29.9))
        self.assertTrue(monitor.should_alert(True, {"price": 39.9, "on_sale": False}, 29.9))
        self.assertTrue(
            monitor.should_alert(True, {"price": 39.9, "on_sale": True}, 29.9)
        )
        self.assertFalse(
            monitor.should_alert(True, {"price": 29.9, "on_sale": True}, 29.9)
        )
        self.assertFalse(monitor.should_alert(False, None, 29.9))

    def test_format_sale_line(self):
        line = monitor.format_sale_line({
            "name": "Relaxed Fit Jacket",
            "size_label": "S (BROWN)",
            "price": 29.9,
            "base_price": 49.9,
            "url": "https://example.com/item",
        })
        self.assertIn("Relaxed Fit Jacket · S/BROWN · $29.9", line)
        self.assertIn("was $49.9", line)
        self.assertIn("-40%", line)
        self.assertIn("<https://example.com/item>", line)

    def test_product_id_from_state_key(self):
        self.assertEqual(
            monitor.product_id_from_state_key("E483836-000_34_S"),
            "E483836-000",
        )
        self.assertEqual(
            monitor.product_id_from_state_key("E483836-000_S"),
            "E483836-000",
        )
        self.assertEqual(
            monitor.product_id_from_state_key("E483836-000_NOSZ"),
            "E483836-000",
        )

    def test_cleanup_state(self):
        state = {
            "KEEP-000_01_S": {"price": 10, "on_sale": False},
            "OLD-000_01_S": {"price": 10, "on_sale": False},
            "__product__KEEP-000": {"seen": True},
        }
        monitor.cleanup_state(state, {"KEEP-000"})
        self.assertIn("KEEP-000_01_S", state)
        self.assertIn("__product__KEEP-000", state)
        self.assertNotIn("OLD-000_01_S", state)

    def test_is_product_known(self):
        state = {"E100-000_01_S": {"price": 10, "on_sale": False}}
        self.assertTrue(monitor.is_product_known(state, "E100-000"))
        self.assertFalse(monitor.is_product_known(state, "E200-000"))

    def test_format_new_product_line(self):
        line = monitor.format_new_product_line({
            "name": "Relaxed Fit Jacket",
            "price": 49.9,
            "url": "https://example.com/item",
        })
        self.assertIn("Relaxed Fit Jacket · $49.9", line)
        self.assertIn("<https://example.com/item>", line)

    def test_max_wool_percent(self):
        self.assertEqual(monitor.max_wool_percent("100% Wool"), 100.0)
        self.assertEqual(
            monitor.max_wool_percent("60% Wool, 40% Polyester"),
            60.0,
        )
        self.assertEqual(
            monitor.max_wool_percent("70% Acrylic, 30% Wool"),
            30.0,
        )
        self.assertEqual(
            monitor.max_wool_percent("Shell: 65% Wool, 35% Nylon/ Lining: 100% Polyester"),
            65.0,
        )
        self.assertEqual(
            monitor.max_wool_percent("Shell: 100% Polyester/ Lining: 100% Polyester"),
            0.0,
        )

    def test_max_cashmere_percent(self):
        self.assertEqual(monitor.max_cashmere_percent("100% Cashmere"), 100.0)
        self.assertEqual(
            monitor.max_cashmere_percent("57% Acrylic, 28% Rayon, 9% Cashmere, 6% Spandex"),
            9.0,
        )
        self.assertEqual(
            monitor.max_cashmere_percent("60% Cashmere, 40% Wool"),
            60.0,
        )

    def test_qualifies_for_monitoring(self):
        with patch.object(monitor, "MIN_WOOL_PERCENT", 60.0), patch.object(
            monitor, "MIN_CASHMERE_PERCENT", 60.0
        ):
            self.assertTrue(monitor.qualifies_for_monitoring(True, "100% Polyester"))
            self.assertTrue(monitor.qualifies_for_monitoring(False, "100% Wool"))
            self.assertTrue(monitor.qualifies_for_monitoring(False, "60% Wool, 40% Nylon"))
            self.assertTrue(monitor.qualifies_for_monitoring(False, "100% Cashmere"))
            self.assertFalse(monitor.qualifies_for_monitoring(False, "70% Acrylic, 30% Wool"))
            self.assertFalse(
                monitor.qualifies_for_monitoring(
                    False, "57% Acrylic, 28% Rayon, 9% Cashmere, 6% Spandex"
                )
            )


if __name__ == "__main__":
    unittest.main()
