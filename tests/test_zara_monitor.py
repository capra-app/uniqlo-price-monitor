import unittest

import zara_monitor as zara

PRODUCT = {
    "id": 571684681,
    "name": "PANTALÓN REGULAR FIT 100% LANA",
    "detail": {
        "colors": [
            {
                "name": "Gris Antracita",
                "productId": 571694692,
                "price": 35900000,
                "pricing": {
                    "price": {
                        "currency": {"code": "COP", "exponent": -2},
                        "value": 35900000,
                    }
                },
                "extraInfo": {"highlightPrice": False},
                "sizes": [
                    {"name": "38", "availability": "out_of_stock"},
                    {"name": "40", "availability": "out_of_stock"},
                    {"name": "42", "availability": "out_of_stock"},
                    {"name": "44", "availability": "in_stock"},
                ],
            },
            {
                "name": "Marino",
                "productId": 571694690,
                "price": 35900000,
                "sizes": [
                    {"name": "38", "availability": "in_stock"},
                    {"name": "40", "availability": "in_stock"},
                ],
            },
            {
                "name": "Negro",
                "productId": 571694688,
                "price": 35900000,
                "sizes": [
                    {"name": "40", "availability": "in_stock"},
                ],
            },
        ]
    },
}

URL = (
    "https://www.zara.com/co/es/pantalon-regular-fit-100-lana-p02381381.html"
    "?v1=571694692&v2=2737921"
)


class ZaraMonitorTests(unittest.TestCase):
    def test_parse_url_and_variant(self):
        self.assertEqual(zara.parse_variant_id(URL), "571694692")
        self.assertEqual(zara.extract_seo_product_id(URL), "02381381")
        self.assertEqual(
            zara.normalize_product_url(URL),
            "https://www.zara.com/co/es/pantalon-regular-fit-100-lana-p02381381.html"
            "?v1=571694692",
        )

    def test_product_snapshot_selects_color_and_stock(self):
        snapshot = zara.product_snapshot(PRODUCT, URL)
        self.assertEqual(snapshot["name"], "PANTALÓN REGULAR FIT 100% LANA")
        self.assertEqual(snapshot["color"], "Gris Antracita")
        self.assertEqual(snapshot["variant_id"], "571694692")
        self.assertEqual(snapshot["price"], 359000.0)
        self.assertEqual(snapshot["currency"], "COP")
        self.assertFalse(snapshot["on_sale"])
        self.assertTrue(snapshot["in_stock"])
        self.assertEqual(snapshot["fit_sizes"], ["44"])

    def test_product_snapshot_can_limit_sizes(self):
        snapshot = zara.product_snapshot(PRODUCT, URL, wanted_sizes=["40"])
        self.assertFalse(snapshot["in_stock"])
        self.assertEqual(snapshot["fit_sizes"], [])

    def test_product_snapshots_all_colors_size_40(self):
        snapshots = zara.product_snapshots(
            PRODUCT,
            URL,
            wanted_sizes=["40"],
            all_colors=True,
        )
        self.assertEqual(
            [(row["color"], row["in_stock"], row["url"]) for row in snapshots],
            [
                (
                    "Gris Antracita",
                    False,
                    "https://www.zara.com/co/es/pantalon-regular-fit-100-lana-p02381381.html"
                    "?v1=571694692",
                ),
                (
                    "Marino",
                    True,
                    "https://www.zara.com/co/es/pantalon-regular-fit-100-lana-p02381381.html"
                    "?v1=571694690",
                ),
                (
                    "Negro",
                    True,
                    "https://www.zara.com/co/es/pantalon-regular-fit-100-lana-p02381381.html"
                    "?v1=571694688",
                ),
            ],
        )
        self.assertTrue(zara.watch_all_colors({"colors": "*"}))
        self.assertFalse(zara.watch_all_colors({}))

    def test_sale_and_out_of_stock_alerts(self):
        regular = zara.product_snapshot(PRODUCT, URL)
        sale_product = {
            "id": PRODUCT["id"],
            "name": PRODUCT["name"],
            "detail": {
                "colors": [
                    {
                        **PRODUCT["detail"]["colors"][0],
                        "price": 25900000,
                        "oldPrice": 35900000,
                        "extraInfo": {"highlightPrice": True},
                    }
                ]
            },
        }
        sale = zara.product_snapshot(sale_product, URL)
        self.assertTrue(sale["on_sale"])
        self.assertEqual(sale["list_price"], 359000.0)

        previous = {"on_sale": False, "in_stock": True}
        self.assertTrue(zara.should_alert_sale(previous, sale))
        self.assertFalse(zara.should_alert_sale(None, sale))

        oos_product = {
            "id": PRODUCT["id"],
            "name": PRODUCT["name"],
            "detail": {
                "colors": [
                    {
                        **PRODUCT["detail"]["colors"][0],
                        "sizes": [
                            {"name": "44", "availability": "out_of_stock"},
                        ],
                    }
                ]
            },
        }
        oos = zara.product_snapshot(oos_product, URL)
        self.assertFalse(oos["in_stock"])
        self.assertTrue(zara.should_alert_out_of_stock(previous, oos))
        self.assertFalse(zara.should_alert_out_of_stock(None, oos))

        sale_line = zara.format_alert_line(sale, "on_sale")
        self.assertIn("259.000", sale_line)
        self.assertIn("359.000", sale_line)
        self.assertIn("on sale", sale_line)
        self.assertIn("v1=571694692", sale_line)

        oos_line = zara.format_alert_line(oos, "out_of_stock")
        self.assertIn("out of stock", oos_line)
        self.assertIn("Gris Antracita", oos_line)

        sized = zara.product_snapshot(PRODUCT, URL, wanted_sizes=["40"])
        sized_line = zara.format_alert_line(sized, "out_of_stock")
        self.assertIn(" · 40 · out of stock", sized_line)

    def test_storefront_color_from_component(self):
        scc = {
            "identifier": {"id": "571694692", "productParentId": "571684681"},
            "name": "100% WOOL REGULAR FIT PANTS",
            "color": {"id": "801", "name": "Anthracite Gray"},
            "pricing": {
                "price": {
                    "current": {
                        "value": 35900000,
                        "currency": {"code": "COP", "exponent": -2},
                    }
                }
            },
            "sizes": [
                {
                    "identifier": {"sku": 571684691},
                    "nomenclature": {"name": "40"},
                    "availability": "in_stock",
                }
            ],
        }
        color = zara.storefront_color_from_component(
            scc, stock_by_sku={"571684691": "out_of_stock"}
        )
        self.assertEqual(color["name"], "Anthracite Gray")
        self.assertEqual(color["productId"], "571694692")
        self.assertEqual(color["price"], 35900000)
        self.assertEqual(color["sizes"][0]["availability"], "out_of_stock")

        product = {
            "id": "571684681",
            "name": scc["name"],
            "detail": {"colors": [color]},
        }
        snapshot = zara.product_snapshot(product, URL, wanted_sizes=["40"])
        self.assertEqual(snapshot["price"], 359000.0)
        self.assertFalse(snapshot["in_stock"])
        self.assertEqual(snapshot["color"], "Anthracite Gray")


if __name__ == "__main__":
    unittest.main()
