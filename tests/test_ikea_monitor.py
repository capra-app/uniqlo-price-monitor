import unittest

import ikea_monitor as ikea


class IkeaMonitorTests(unittest.TestCase):
    def test_extract_item_no_and_normalize_url(self):
        url = "https://www.ikea.com/co/es/p/lindbyn-espejo-negro-80597232/"
        self.assertEqual(ikea.extract_item_no(url), "80597232")
        self.assertEqual(
            ikea.normalize_product_url(url.rstrip("/")),
            "https://www.ikea.com/co/es/p/lindbyn-espejo-negro-80597232/",
        )
        self.assertEqual(ikea.parse_market(url), ("co", "es"))

    def test_parse_formatted_price(self):
        self.assertEqual(ikea.parse_formatted_price("349.990"), 349990.0)
        self.assertEqual(ikea.parse_formatted_price("1.699.990"), 1699990.0)
        self.assertEqual(ikea.parse_formatted_price(24990), 24990.0)

    def test_is_on_sale(self):
        regular = {
            "lastChance": False,
            "salesPrice": {"tag": "NONE", "numeral": 349990.0},
        }
        lowered = {
            "lastChance": False,
            "salesPrice": {
                "tag": "NEW_LOWER_PRICE",
                "numeral": 1499990.0,
                "previous": {"wholeNumber": "1.699.990"},
            },
        }
        last_chance = {"lastChance": True, "salesPrice": {"tag": "NONE", "numeral": 10}}
        self.assertFalse(ikea.is_on_sale(regular))
        self.assertTrue(ikea.is_on_sale(lowered))
        self.assertTrue(ikea.is_on_sale(last_chance))

    def test_product_snapshot_and_alerts(self):
        product = {
            "id": "80597232",
            "itemNo": "80597232",
            "name": "LINDBYN",
            "typeName": "Espejo",
            "itemMeasureReferenceText": "60x170 cm",
            "validDesignText": "negro",
            "lastChance": False,
            "pipUrl": "https://www.ikea.com/co/es/p/lindbyn-espejo-negro-80597232/",
            "salesPrice": {
                "currencyCode": "COP",
                "numeral": 349990.0,
                "tag": "NONE",
            },
        }
        url = "https://www.ikea.com/co/es/p/lindbyn-espejo-negro-80597232/"
        snapshot = ikea.product_snapshot(product, url)
        self.assertEqual(snapshot["price"], 349990.0)
        self.assertFalse(snapshot["on_sale"])
        self.assertIn("LINDBYN", snapshot["name"])

        sale_product = dict(product)
        sale_product["salesPrice"] = {
            "currencyCode": "COP",
            "numeral": 299990.0,
            "tag": "NEW_LOWER_PRICE",
            "tagText": "Bajamos el precio",
            "previous": {"wholeNumber": "349.990"},
        }
        sale = ikea.product_snapshot(sale_product, url)
        self.assertTrue(sale["on_sale"])
        self.assertEqual(sale["list_price"], 349990.0)

        previous = {"price": 349990.0, "on_sale": False}
        self.assertTrue(ikea.should_alert_sale(previous, sale))
        self.assertFalse(ikea.should_alert_sale(None, sale))
        self.assertTrue(ikea.should_alert_price_drop(previous, sale))
        self.assertFalse(ikea.should_alert_price_drop(None, sale))

        line = ikea.format_alert_line(sale, "on_sale")
        self.assertIn("299.990", line)
        self.assertIn("349.990", line)
        self.assertIn("Bajamos el precio", line)
        self.assertIn(url, line)

    def test_find_product_in_search(self):
        payload = {
            "searchResultPage": {
                "products": {
                    "main": {
                        "items": [
                            {"product": {"id": "11111111", "name": "Other"}},
                            {"product": {"id": "80597232", "name": "LINDBYN"}},
                        ]
                    }
                }
            }
        }
        product = ikea.find_product_in_search(payload, "80597232")
        self.assertEqual(product["name"], "LINDBYN")
        with self.assertRaises(RuntimeError):
            ikea.find_product_in_search(payload, "00000000")


if __name__ == "__main__":
    unittest.main()
