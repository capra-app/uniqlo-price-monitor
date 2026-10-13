import unittest

import arcteryx_monitor as arx


class ArcteryxMonitorTests(unittest.TestCase):
    def setUp(self):
        arx._build_id_cache.clear()

    def test_normalize_product_url_strips_trailing_dash(self):
        url = "https://outlet.arcteryx.com/us/en/shop/mens/cerium-jacket-0579-"
        self.assertEqual(
            arx.normalize_product_url(url),
            "https://outlet.arcteryx.com/us/en/shop/mens/cerium-jacket-0579",
        )

    def test_next_data_url_and_build_id_helpers(self):
        url = "https://arcteryx.com/us/en/shop/mens/kragg-cotton-pant-9530"
        self.assertEqual(arx.market_prefix(url), "/us/en")
        self.assertEqual(
            arx.build_id_probe_url(url),
            "https://arcteryx.com/us/en/shop",
        )
        self.assertEqual(
            arx.next_data_url(url, "abc123"),
            "https://arcteryx.com/_next/data/abc123/us/en/shop/mens/kragg-cotton-pant-9530.json",
        )
        self.assertEqual(
            arx.extract_build_id('<script>{"buildId":"abc123","page":"/"}</script>'),
            "abc123",
        )

    def test_fetch_product_uses_next_data(self):
        product = {
            "name": "Kragg Cotton Pant",
            "colourOptions": [{"id": "1", "label": "Black"}],
            "sizeOptions": {"options": [{"label": "30-R", "value": "5801"}]},
            "variants": [],
        }
        url = "https://arcteryx.com/us/en/shop/mens/kragg-cotton-pant-9530"

        class FakeResponse:
            def __init__(self, status_code, payload=None, text=""):
                self.status_code = status_code
                self._payload = payload
                self.text = text

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise arx.requests.HTTPError(f"{self.status_code}")

            def json(self):
                return self._payload

        class FakeSession:
            def get(self, request_url, timeout=20):
                if request_url.endswith("/shop"):
                    return FakeResponse(
                        200,
                        text='{"buildId":"build-1","page":"/us/en/shop"}',
                    )
                if "/_next/data/build-1/" in request_url:
                    return FakeResponse(200, payload={"pageProps": {"product": product}})
                raise AssertionError(f"unexpected url: {request_url}")

        fetched = arx.fetch_product(FakeSession(), url)
        self.assertEqual(fetched["name"], "Kragg Cotton Pant")
        self.assertEqual(arx._build_id_cache["arcteryx.com"], "build-1")

    def test_fetch_product_treats_missing_listing_as_unavailable(self):
        url = "https://outlet.arcteryx.com/us/en/shop/mens/cerium-jacket-0579"

        class FakeResponse:
            def __init__(self, status_code, payload=None, text=""):
                self.status_code = status_code
                self._payload = payload
                self.text = text

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise arx.requests.HTTPError(f"{self.status_code}")

            def json(self):
                return self._payload

        class FakeSession:
            def get(self, request_url, timeout=20):
                if request_url.endswith("/shop"):
                    return FakeResponse(
                        200,
                        text='{"buildId":"build-1","page":"/us/en/shop"}',
                    )
                if "/_next/data/build-1/" in request_url:
                    return FakeResponse(
                        404,
                        payload={"pageProps": {"i18nResources": {}}, "__N_SSP": True},
                    )
                raise AssertionError(f"unexpected url: {request_url}")

        with self.assertRaises(arx.ProductUnavailableError):
            arx.fetch_product(FakeSession(), url)

    def test_fetch_product_refreshes_stale_build_id(self):
        product = {"name": "Kragg Cotton Pant", "variants": []}
        url = "https://outlet.arcteryx.com/us/en/shop/mens/kragg-cotton-pant-9530"
        calls = {"shop": 0}

        class FakeResponse:
            def __init__(self, status_code, payload=None, text=""):
                self.status_code = status_code
                self._payload = payload
                self.text = text

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise arx.requests.HTTPError(f"{self.status_code}")

            def json(self):
                return self._payload

        class FakeSession:
            def get(self, request_url, timeout=20):
                if request_url.endswith("/shop"):
                    calls["shop"] += 1
                    build = "build-old" if calls["shop"] == 1 else "build-new"
                    return FakeResponse(200, text=f'{{"buildId":"{build}"}}')
                if "/_next/data/build-old/" in request_url:
                    return FakeResponse(404, payload={})
                if "/_next/data/build-new/" in request_url:
                    return FakeResponse(200, payload={"pageProps": {"product": product}})
                raise AssertionError(f"unexpected url: {request_url}")

        fetched = arx.fetch_product(FakeSession(), url)
        self.assertEqual(fetched["name"], "Kragg Cotton Pant")
        self.assertEqual(arx._build_id_cache["outlet.arcteryx.com"], "build-new")

    def test_find_color_id_partial_match(self):
        colors = {"16271": "Azalea / Black", "1697": "Forage"}
        self.assertEqual(arx.find_color_id(colors, "Azalea"), "16271")

    def test_find_size_id(self):
        sizes = {"6004": "M", "6020": "S", "5801": "30-R"}
        self.assertEqual(arx.find_size_id(sizes, "M"), "6004")
        self.assertEqual(arx.find_size_id(sizes, "30R"), "5801")

    def test_should_alert_restock(self):
        previous = {"in_stock": False, "stock_status": "OutOfStock"}
        in_stock = {"in_stock": True, "stock_status": "InStock"}
        out_of_stock = {"in_stock": False, "stock_status": "OutOfStock"}
        self.assertFalse(arx.should_alert_restock(None, in_stock))
        self.assertFalse(arx.should_alert_restock(previous, out_of_stock))
        self.assertTrue(arx.should_alert_restock(previous, in_stock))

    def test_should_alert_outlet_price(self):
        previous = {"on_outlet": False}
        outlet = {"on_outlet": True, "price": 100.8, "list_price": 180.0}
        full_price = {"on_outlet": False, "price": 180.0, "list_price": 180.0}
        self.assertFalse(arx.should_alert_outlet_price(None, outlet))
        self.assertFalse(arx.should_alert_outlet_price({"on_outlet": True}, outlet))
        self.assertTrue(arx.should_alert_outlet_price(previous, outlet))
        self.assertFalse(arx.should_alert_outlet_price(previous, full_price))

    def test_lookup_variants_any_color(self):
        product = {
            "name": "Kragg Cotton Pant",
            "colourOptions": [
                {"id": "1", "label": "Black"},
                {"id": "2", "label": "Void"},
            ],
            "sizeOptions": {"options": [{"label": "30-R", "value": "5801"}]},
            "variants": [
                {
                    "id": "A",
                    "price": 180,
                    "discountPrice": 180,
                    "colourId": "1",
                    "sizeId": "5801",
                    "stockStatus": "InStock",
                },
                {
                    "id": "B",
                    "price": 180,
                    "discountPrice": 100.8,
                    "colourId": "2",
                    "sizeId": "5801",
                    "stockStatus": "OutOfStock",
                },
            ],
        }
        snapshots = arx.lookup_variants(product, "*", "30R", any_color=True)
        self.assertEqual(len(snapshots), 2)
        self.assertFalse(snapshots[0]["on_outlet"])
        self.assertTrue(snapshots[1]["on_outlet"])

    def test_variant_current_price_uses_discount(self):
        variant = {"price": 380, "discountPrice": 285}
        self.assertEqual(arx.variant_current_price(variant), 285.0)

    def test_should_alert_price(self):
        self.assertFalse(arx.should_alert_price(None, 285))
        self.assertTrue(arx.should_alert_price({"price": 300}, 285))
        self.assertFalse(arx.should_alert_price({"price": 285}, 285))

    def test_should_alert_low_stock(self):
        previous = {"in_stock": True, "stock_status": "InStock"}
        in_stock = {"in_stock": True, "stock_status": "InStock"}
        out_of_stock = {"in_stock": False, "stock_status": "OutOfStock"}
        self.assertFalse(arx.should_alert_low_stock(None, out_of_stock))
        self.assertFalse(arx.should_alert_low_stock(previous, in_stock))
        self.assertTrue(arx.should_alert_low_stock(previous, out_of_stock))

    def test_lookup_variant(self):
        product = {
            "name": "Cerium Jacket",
            "colourOptions": [{"id": "16271", "label": "Azalea / Black"}],
            "sizeOptions": {"options": [{"label": "M", "value": "6004"}]},
            "variants": [{
                "id": "X000010579003",
                "price": 380,
                "discountPrice": 285,
                "colourId": "16271",
                "sizeId": "6004",
                "stockStatus": "InStock",
            }],
        }
        snapshot = arx.lookup_variant(product, "Azalea", "M")
        self.assertEqual(snapshot["price"], 285.0)
        self.assertEqual(snapshot["color_label"], "Azalea / Black")
        self.assertTrue(snapshot["in_stock"])

    def test_product_is_merino(self):
        merino_product = {
            "name": "Rho Merino Wool Bottom",
            "materials": ["Self: 240gsm Mountain Merino Wool - 95% wool, 5% elastane"],
        }
        cotton_product = {
            "name": "Kragg Cotton Pant",
            "materials": ["Self: 100% cotton"],
        }
        self.assertTrue(arx.product_is_merino(merino_product))
        self.assertFalse(arx.product_is_merino(cotton_product))

    def test_merino_in_stock_snapshots_filters_sizes(self):
        product = {
            "name": "Rho Merino Wool Bottom",
            "colourOptions": [{"id": "1", "label": "Phantasm"}],
            "sizeOptions": {
                "options": [
                    {"label": "S", "value": "6020"},
                    {"label": "M", "value": "6004"},
                    {"label": "L", "value": "6005"},
                ]
            },
            "variants": [
                {
                    "id": "S",
                    "price": 120,
                    "discountPrice": 72,
                    "colourId": "1",
                    "sizeId": "6020",
                    "stockStatus": "InStock",
                },
                {
                    "id": "M",
                    "price": 120,
                    "discountPrice": 72,
                    "colourId": "1",
                    "sizeId": "6004",
                    "stockStatus": "OutOfStock",
                },
                {
                    "id": "L",
                    "price": 120,
                    "discountPrice": 72,
                    "colourId": "1",
                    "sizeId": "6005",
                    "stockStatus": "InStock",
                },
            ],
        }
        snapshots = arx.merino_in_stock_snapshots(product, arx.merino_target_size_labels(["S", "M"]))
        self.assertEqual([snap["size_label"] for snap in snapshots], ["S"])

    def test_fetch_outlet_mens_product_urls(self):
        class FakeResponse:
            status_code = 200

            text = """
            <urlset>
              <url><loc>https://outlet.arcteryx.com/us/en/shop/mens/rho-merino-wool-bottom</loc></url>
              <url><loc>https://outlet.arcteryx.com/us/fr/shop/mens/rho-merino-wool-bottom</loc></url>
              <url><loc>https://outlet.arcteryx.com/us/en/shop/womens/jacket</loc></url>
            </urlset>
            """

            def raise_for_status(self):
                return None

        class FakeSession:
            def get(self, url, timeout=20):
                return FakeResponse()

        urls = arx.fetch_outlet_mens_product_urls(FakeSession())
        self.assertEqual(
            urls,
            ["https://outlet.arcteryx.com/us/en/shop/mens/rho-merino-wool-bottom"],
        )


if __name__ == "__main__":
    unittest.main()
