import os
import unittest
from unittest import mock

import fit
import sierra_monitor as sierra


LISTING_HTML = """
<a href="https://www.sierra.com/smartwool-merino-wool-t-shirt-short-sleeve~p~5vcrt/?filterString=s~merino-wool/">SmartWool</a>
[SmartWool](https://www.sierra.com/smartwool-merino-wool-t-shirt-short-sleeve~p~5vcrt/?filterString=s~merino-wool/)
[Merino Wool T-Shirt - Short Sleeve](https://www.sierra.com/smartwool-merino-wool-t-shirt-short-sleeve~p~5vcrt/?filterString=s~merino-wool/)
$29.99
Compare at $65.00
<a href="https://www.sierra.com/smartwool-everyday-tank-top-merino-wool~p~7gdpd/">Tank</a>
[Everyday Tank Top - Merino Wool](https://www.sierra.com/smartwool-everyday-tank-top-merino-wool~p~7gdpd/)
$19.99
"""

DETAIL_HTML = """
<title>SmartWool Merino Wool T-Shirt - Short Sleeve</title>
Fabric: 88% merino wool, 12% nylon
Compare at $65.00
$29.99
<div class="size-tile-selector">M</div>
<div class="size-tile-selector">L</div>
"""

LOW_MERINO_DETAIL_HTML = """
Fabric: 29% merino wool, 71% polyester
$19.99
<div class="size-tile-selector">M</div>
"""

PROFILE = {
    "gender": "men",
    "shoe_us": 9.5,
    "shoe_eu": 43,
    "known_good_sizes": {
        "uniqlo": {
            "tops": ["S", "M"],
            "bottoms": ["M", "30", "31"],
        },
        "sierra": {
            "tops": ["M"],
            "bottoms": ["M", "30", "31"],
        },
    },
    "sock_size_charts": {
        "smartwool": {
            "M": {"shoe_us": [6, 8.5]},
            "L": {"shoe_us": [9, 11.5]},
        },
    },
}


class SierraMonitorTests(unittest.TestCase):
    def test_parse_listing_products(self):
        products = sierra.parse_listing_products(LISTING_HTML)
        self.assertEqual(len(products), 2)
        by_id = {item["product_id"]: item for item in products}
        self.assertEqual(by_id["5vcrt"]["price"], 29.99)
        self.assertEqual(by_id["5vcrt"]["compare_at"], 65.0)
        self.assertIn("Merino Wool T-Shirt", by_id["5vcrt"]["name"])

    def test_parse_product_detail_merino_percent(self):
        listing = {
            "product_id": "5vcrt",
            "url": "https://www.sierra.com/smartwool-merino-wool-t-shirt-short-sleeve~p~5vcrt/",
            "name": "Merino Wool T-Shirt - Short Sleeve",
            "price": 29.99,
            "compare_at": 65.0,
        }
        product = sierra.parse_product_detail(DETAIL_HTML, listing)
        self.assertEqual(product["merino_pct"], 88.0)
        self.assertIn("M", product["sizes"])

    def test_parse_available_sizes_from_sku_variant(self):
        html = """
        {"id":"9PTAJ","variant":"9PTAJ-01-M","productChildStock":118}
        """
        self.assertEqual(
            sierra.parse_available_sizes(html, product_id="9ptaj"),
            ["M"],
        )

    def test_parse_available_sizes_empty_when_out_of_stock(self):
        html = """
        {"id":"5VCRT","variant":"5VCRT-01-S","productChildStock":0}
        """
        self.assertEqual(sierra.parse_available_sizes(html, product_id="5vcrt"), [])

    def test_parse_available_sizes_from_taffy_not_just_default_sku(self):
        html = """
        <label id="property2Label" title="SIZE"><legend>Size:</legend></label>
        {"id":"9PYTT","variant":"9PYTT-01-L","productChildStock":19,"productParentStock":91}
        <script>
        var skus = new TAFFY(
        [
    { property1: "01", property2: "M", property3: "", availabilityMsg: "", availabilityCss: "inStock", price: "39.99" },
    { property1: "01", property2: "L", property3: "", availabilityMsg: "", availabilityCss: "inStock", price: "39.99" },
    { property1: "01", property2: "S", property3: "", availabilityMsg: "", availabilityCss: "outOfStock", price: "39.99" },
    { property1: "01", property2: "XL", property3: "", availabilityMsg: "", availabilityCss: "inStock", price: "39.99" }
]
    );
        </script>
        """
        self.assertEqual(
            sierra.parse_available_sizes(html, product_id="9pytt"),
            ["M", "L", "XL"],
        )

    def test_parse_taffy_in_stock_sizes_empty_when_all_out_of_stock(self):
        html = """
        <label id="property2Label" title="SIZE"></label>
        var skus = new TAFFY(
        [
    { property1: "01", property2: "M", property3: "", availabilityMsg: "Out of Stock", availabilityCss: "outOfStock" }
]
    );
        """
        self.assertEqual(sierra.parse_taffy_in_stock_sizes(html), [])
        self.assertEqual(sierra.parse_available_sizes(html, product_id="9pytt"), [])

    def test_qualifies_for_alert_rejects_missing_sizes(self):
        listing = {
            "product_id": "5vcrt",
            "url": "https://www.sierra.com/smartwool-merino-wool-t-shirt-short-sleeve~p~5vcrt/",
            "name": "Merino Wool T-Shirt - Short Sleeve",
            "price": 29.99,
            "compare_at": 65.0,
        }
        html = """
        Fabric: 88% merino wool, 12% nylon
        $29.99
        """
        product = sierra.parse_product_detail(html, listing)
        qualifies, reason = sierra.qualifies_for_alert(PROFILE, product)
        self.assertFalse(qualifies)
        self.assertEqual(reason, "missing size data")

    def test_qualifies_for_alert_requires_50_percent_merino(self):
        listing = {
            "product_id": "low",
            "url": "https://www.sierra.com/example~p~low/",
            "name": "Example Top",
            "price": 19.99,
            "compare_at": 0.0,
        }
        product = sierra.parse_product_detail(LOW_MERINO_DETAIL_HTML, listing)
        qualifies, reason = sierra.qualifies_for_alert(PROFILE, product)
        self.assertFalse(qualifies)
        self.assertIn("merino", reason)

    def test_qualifies_shoe_with_leather(self):
        html = """
        Materials: 60% leather, 40% rubber
        $79.99
        <div class="size-tile-selector">9.5</div>
        """
        listing = {
            "product_id": "shoe",
            "url": "https://www.sierra.com/leather-hiking-shoes-for-men~p~shoe/",
            "name": "Leather Hiking Shoes (For Men)",
            "price": 79.99,
            "compare_at": 0.0,
        }
        product = sierra.parse_product_detail(html, listing)
        with mock.patch.dict(os.environ, {"SIERRA_SHOE_ALERTS_ENABLED": "true"}):
            qualifies, reason = sierra.qualifies_for_alert(PROFILE, product)
        self.assertTrue(qualifies)
        self.assertEqual(reason, "ok")
        self.assertEqual(product["shoe_material_pct"], 60.0)

    def test_qualifies_shoe_rejects_when_alerts_disabled(self):
        html = """
        Materials: 60% leather, 40% rubber
        $79.99
        <div class="size-tile-selector">9.5</div>
        """
        listing = {
            "product_id": "shoe",
            "url": "https://www.sierra.com/leather-hiking-shoes-for-men~p~shoe/",
            "name": "Leather Hiking Shoes (For Men)",
            "price": 79.99,
            "compare_at": 0.0,
        }
        product = sierra.parse_product_detail(html, listing)
        with mock.patch.dict(os.environ, {"SIERRA_SHOE_ALERTS_ENABLED": "false"}, clear=False):
            qualifies, reason = sierra.qualifies_for_alert(PROFILE, product)
        self.assertFalse(qualifies)
        self.assertEqual(reason, "shoe alerts disabled")

    def test_qualifies_shoe_rejects_low_leather(self):
        html = """
        Materials: 30% leather, 70% synthetic
        $79.99
        """
        listing = {
            "product_id": "shoe",
            "url": "https://www.sierra.com/synthetic-shoes-for-men~p~shoe/",
            "name": "Synthetic Trail Running Shoes (For Men)",
            "price": 79.99,
            "compare_at": 0.0,
        }
        product = sierra.parse_product_detail(html, listing)
        with mock.patch.dict(os.environ, {"SIERRA_SHOE_ALERTS_ENABLED": "true"}):
            qualifies, reason = sierra.qualifies_for_alert(PROFILE, product)
        self.assertFalse(qualifies)
        self.assertIn("leather/suede", reason)

    def test_sock_still_uses_merino_not_leather(self):
        self.assertFalse(sierra.uses_shoe_material_rules("Merino Wool Crew Socks (For Men)"))
        self.assertEqual(sierra.product_size_category("Merino Wool Crew Socks (For Men)"), "socks")
        html = """
        Fabric: 88% merino wool, 12% nylon
        $14.99
        """
        listing = {
            "product_id": "sock",
            "url": "https://www.sierra.com/merino-socks~p~sock/",
            "name": "Merino Wool Crew Socks (For Men)",
            "price": 14.99,
            "compare_at": 0.0,
            "brand": "SmartWool",
        }
        product = sierra.parse_product_detail(html, listing)
        product["sizes"] = ["M", "L", "XL"]
        qualifies, reason = sierra.qualifies_for_alert(PROFILE, product)
        self.assertFalse(qualifies)
        self.assertEqual(reason, "socks excluded")

    def test_matching_sock_sizes_use_brand_chart(self):
        product = {
            "name": "Merino Wool Crew Socks (For Men)",
            "brand": "SmartWool",
            "sizes": ["M", "L", "XL"],
        }
        matches = sierra.matching_sizes(PROFILE, product)
        self.assertEqual(matches, ["L"])

    def test_max_shoe_material_percent_sums_leather_and_suede(self):
        composition = "Upper: 30% leather, 25% suede, 45% rubber"
        self.assertEqual(sierra.max_shoe_material_percent(composition), 55.0)

    def test_max_shoe_material_percent_reads_goat_leather(self):
        composition = "Upper: 100% goat leather"
        self.assertEqual(sierra.max_shoe_material_percent(composition), 100.0)

    def test_max_shoe_material_percent_prose_upper_leather(self):
        self.assertEqual(
            sierra.max_shoe_material_percent("Upper: Leather"),
            100.0,
        )
        self.assertEqual(
            sierra.max_shoe_material_percent("Upper: Leather and textile"),
            50.0,
        )
        self.assertEqual(
            sierra.max_shoe_material_percent("Upper: Suede"),
            100.0,
        )

    def test_detail_page_has_composition_accepts_prose_upper(self):
        html = "<div>Upper: Leather and textile</div>"
        self.assertTrue(sierra.detail_page_has_composition(html))

    def test_parse_composition_filters_css_noise(self):
        html = """
        Upper: Leather and synthetic
        #onetrust-consent-sdk .ot-general-modal{overflow:hidden;position:fixed;}
        """
        self.assertEqual(
            sierra.parse_composition(html),
            "Leather and synthetic",
        )

    def test_merino_shoe_uses_merino_rules_not_leather(self):
        self.assertFalse(
            sierra.uses_shoe_material_rules("UYN Merino Wool Trail Running Shoes (For Men)")
        )
        html = """
        Upper: Merino wool and synthetic
        Fabric: 88% merino wool, 12% nylon
        $99.99
        <div class="size-tile-selector">9.5</div>
        """
        listing = {
            "product_id": "mwshoe",
            "url": "https://www.sierra.com/uyn-merino-shoes~p~mwshoe/",
            "name": "UYN Merino Wool Trail Running Shoes (For Men)",
            "price": 99.99,
            "compare_at": 0.0,
        }
        product = sierra.parse_product_detail(html, listing)
        self.assertEqual(product["merino_pct"], 88.0)
        self.assertEqual(product["shoe_material_pct"], 0.0)
        qualifies, reason = sierra.qualifies_for_alert(PROFILE, product)
        self.assertTrue(qualifies)
        self.assertEqual(reason, "ok")
        self.assertEqual(product["fit_sizes"], ["9.5"])

    def test_max_down_fill_percent_reads_insulation_line(self):
        composition = "Insulation: 90% duck down, 10% duck feathers"
        self.assertEqual(sierra.max_down_fill_percent(composition), 90.0)

    def test_max_down_fill_percent_reads_grey_duck_down(self):
        composition = "90% grey duck down, 10% waterfowl feathers"
        self.assertEqual(sierra.max_down_fill_percent(composition), 90.0)

    def test_qualifies_obermeyer_style_grey_duck_down(self):
        html = """
        Insulation: 90% grey duck down, 10% waterfowl feathers
        Fill power: 800
        $129.99
        <div class="size-tile-selector">M</div>
        """
        listing = {
            "product_id": "ober",
            "url": "https://www.sierra.com/obermeyer-klaus-down-hooded-jacket-800-fill-power~p~ober/",
            "name": "Obermeyer Klaus Down Hooded Jacket - 800 Fill Power",
            "price": 129.99,
            "compare_at": 0.0,
        }
        product = sierra.parse_product_detail(html, listing)
        qualifies, reason = sierra.qualifies_for_alert(PROFILE, product)
        self.assertTrue(qualifies)
        self.assertEqual(reason, "ok")
        self.assertEqual(product["down_fill_pct"], 90.0)
        self.assertEqual(product["fill_power"], 800)

    def test_parse_fill_power_reads_spec_line_and_name(self):
        html = """
        Insulation: 80% goose down, 20% feathers
        Fill power: 800
        """
        self.assertEqual(
            sierra.parse_fill_power(html, name="Obermeyer Klaus Down Hooded Jacket"),
            800,
        )
        self.assertEqual(
            sierra.parse_fill_power("", name="Salewa Jacket - 750 Fill Power"),
            750,
        )

    def test_qualifies_down_jacket_with_70_percent_fill(self):
        html = """
        Insulation: 80% goose down, 20% feathers
        Fill power: 800
        Fabric: 100% nylon
        $199.99
        <div class="size-tile-selector">M</div>
        """
        listing = {
            "product_id": "down",
            "url": "https://www.sierra.com/marmot-down-jacket-insulated~p~down/",
            "name": "Marmot Down Jacket - Insulated",
            "price": 199.99,
            "compare_at": 0.0,
        }
        product = sierra.parse_product_detail(html, listing)
        qualifies, reason = sierra.qualifies_for_alert(PROFILE, product)
        self.assertTrue(qualifies)
        self.assertEqual(reason, "ok")
        self.assertEqual(product["down_fill_pct"], 80.0)
        self.assertEqual(product["fill_power"], 800)
        self.assertEqual(product["fit_sizes"], ["M"])

    def test_rejects_down_jacket_below_70_percent_fill(self):
        html = """
        Insulation: 60% duck down, 40% feathers
        $149.99
        <div class="size-tile-selector">M</div>
        """
        listing = {
            "product_id": "lowdown",
            "url": "https://www.sierra.com/light-down-jacket~p~lowdown/",
            "name": "Light Down Jacket",
            "price": 149.99,
            "compare_at": 0.0,
        }
        product = sierra.parse_product_detail(html, listing)
        qualifies, reason = sierra.qualifies_for_alert(PROFILE, product)
        self.assertFalse(qualifies)
        self.assertIn("down fill", reason)

    def test_rejects_down_jacket_below_800_fill_power(self):
        html = """
        Insulation: 80% goose down, 20% feathers
        Fill power: 700
        $199.99
        <div class="size-tile-selector">M</div>
        """
        listing = {
            "product_id": "lowfp",
            "url": "https://www.sierra.com/marmot-down-jacket-700-fill~p~lowfp/",
            "name": "Marmot Down Jacket - 700 Fill Power",
            "price": 199.99,
            "compare_at": 0.0,
        }
        product = sierra.parse_product_detail(html, listing)
        qualifies, reason = sierra.qualifies_for_alert(PROFILE, product)
        self.assertFalse(qualifies)
        self.assertIn("fill power", reason)

    def test_rejects_down_jacket_missing_fill_power(self):
        html = """
        Insulation: 90% duck down, 10% duck feathers
        $199.99
        <div class="size-tile-selector">M</div>
        """
        listing = {
            "product_id": "nofp",
            "url": "https://www.sierra.com/canada-goose-down-jacket~p~nofp/",
            "name": "Canada Goose Crofton Puffer Down Jacket - Insulated",
            "price": 199.99,
            "compare_at": 0.0,
        }
        product = sierra.parse_product_detail(html, listing)
        qualifies, reason = sierra.qualifies_for_alert(PROFILE, product)
        self.assertFalse(qualifies)
        self.assertIn("fill power missing", reason)

    def test_likely_down_listing_excludes_button_down_shirts(self):
        self.assertFalse(sierra.likely_down_listing({
            "name": "Dakine Aloha Button-Down Shirt - Short Sleeve",
            "url": "https://www.sierra.com/dakine-aloha-button-down-shirt~p~abc/?filterString=s~down-men/",
        }))
        self.assertTrue(sierra.likely_down_listing({
            "name": "Canada Goose Crofton Puffer Down Jacket - Insulated",
            "url": "https://www.sierra.com/canada-goose-down-jacket~p~abc/?filterString=s~down-men/",
        }))
        self.assertTrue(sierra.likely_down_listing({
            "name": "Helly Hansen Arctic Patrol H2Flow Parka - 800 Fill Power",
            "url": "https://www.sierra.com/helly-hansen-arctic-patrol-parka~p~abc/",
        }))

    def test_qualifies_for_alert_matches_profile_sizes(self):
        listing = {
            "product_id": "5vcrt",
            "url": "https://www.sierra.com/smartwool-merino-wool-t-shirt-short-sleeve~p~5vcrt/",
            "name": "Merino Wool T-Shirt - Short Sleeve",
            "price": 29.99,
            "compare_at": 65.0,
        }
        product = sierra.parse_product_detail(DETAIL_HTML, listing)
        qualifies, reason = sierra.qualifies_for_alert(PROFILE, product)
        self.assertTrue(qualifies)
        self.assertEqual(product["fit_sizes"], ["M"])

    def test_should_inspect_listing(self):
        self.assertFalse(sierra.should_inspect_listing({
            "name": "SmartWool Merino Wool Crew Socks (For Men)",
            "brand": "SmartWool",
            "url": "https://www.sierra.com/example~p~abc/",
        }))
        self.assertFalse(sierra.should_inspect_listing({
            "name": "SmartWool Active Fleece Wind Vest",
            "brand": "SmartWool",
            "url": "https://www.sierra.com/active-fleece-vest~p~abc/",
        }))
        with mock.patch.dict(os.environ, {"SIERRA_SHOE_ALERTS_ENABLED": "true"}):
            self.assertTrue(sierra.should_inspect_listing({
                "name": "Leather Hiking Shoes (For Men)",
                "brand": "Merrell",
                "url": "https://www.sierra.com/leather-hiking-shoes~p~abc/",
            }))
            self.assertTrue(sierra.should_inspect_listing({
                "name": "Nubuck Hiking Boots (For Men)",
                "brand": "Keen",
                "url": "https://www.sierra.com/nubuck-hiking-boots~p~abc/",
            }))
        with mock.patch.dict(os.environ, {"SIERRA_SHOE_ALERTS_ENABLED": "false"}, clear=False):
            self.assertFalse(sierra.should_inspect_listing({
                "name": "Leather Hiking Shoes (For Men)",
                "brand": "Merrell",
                "url": "https://www.sierra.com/leather-hiking-shoes~p~abc/",
            }))
        self.assertTrue(sierra.should_inspect_listing({
            "name": "Everyday Boxer Briefs",
            "brand": "SmartWool",
            "url": "https://www.sierra.com/everyday-boxer-briefs-merino-wool~p~abc/",
        }))
        self.assertTrue(sierra.should_inspect_listing({
            "name": "Canada Goose Crofton Puffer Down Jacket - Insulated",
            "brand": "Canada Goose",
            "url": "https://www.sierra.com/canada-goose-down-jacket~p~abc/?filterString=s~down-men/",
        }))
        self.assertFalse(sierra.should_inspect_listing({
            "name": "Dakine Aloha Button-Down Shirt - Short Sleeve",
            "brand": "Dakine",
            "url": "https://www.sierra.com/dakine-button-down-shirt~p~abc/?filterString=s~down-men/",
        }))
        self.assertFalse(sierra.should_inspect_listing({
            "name": "Marmot WarmCube Down Jacket - 700 Fill Power",
            "brand": "Marmot",
            "url": "https://www.sierra.com/marmot-down-jacket~p~abc/?filterString=s~down-men/",
        }))
        self.assertTrue(sierra.should_inspect_listing({
            "name": "Obermeyer Klaus Down Hooded Jacket - 800 Fill Power",
            "brand": "Obermeyer",
            "url": "https://www.sierra.com/obermeyer-down-jacket~p~abc/?filterString=s~down-men/",
        }))
        self.assertTrue(sierra.should_inspect_listing({
            "name": "Helly Hansen Arctic Patrol H2Flow Parka - 800 Fill Power",
            "brand": "Helly Hansen",
            "url": "https://www.sierra.com/helly-hansen-arctic-patrol-parka~p~abc/",
        }))

    def test_listings_to_inspect_filters_non_merino(self):
        listings = [
            {"name": "Merino Wool T-Shirt", "brand": "SmartWool", "url": "https://www.sierra.com/a~p~1/"},
            {"name": "Fleece Vest", "brand": "SmartWool", "url": "https://www.sierra.com/b~p~2/"},
        ]
        self.assertEqual(len(sierra.listings_to_inspect(listings)), 1)

    def test_is_mens_product_filters_womens(self):
        self.assertFalse(
            sierra.is_mens_product(
                "Trail Hiking Socks - Merino Wool (For Women)",
                "https://www.sierra.com/example-for-women~p~abc/",
            )
        )
        self.assertTrue(
            sierra.is_mens_product(
                "Micro Hiking Socks - Merino Wool, Crew (For Men)",
                "https://www.sierra.com/example-for-men~p~abc/",
            )
        )

    def test_format_alert_line(self):
        line = sierra.format_alert_line({
            "name": "Merino Wool T-Shirt",
            "fit_sizes": ["M"],
            "price": 29.99,
            "compare_at": 65.0,
            "merino_pct": 88,
            "url": "https://www.sierra.com/example~p~abc/",
        })
        self.assertIn("Merino Wool T-Shirt · M · $29.99", line)
        self.assertNotIn("merino", line)
        self.assertNotIn("was $", line)

    def test_parse_category_urls_adds_leather_when_shoe_alerts_enabled(self):
        with mock.patch.dict(os.environ, {"SIERRA_CATEGORY_URLS": "", "SIERRA_SHOE_ALERTS_ENABLED": "true"}):
            urls = sierra.parse_category_urls()
        self.assertIn(sierra.LEATHER_CATEGORY_URL, urls)

    def test_parse_category_urls_omits_leather_when_shoe_alerts_disabled(self):
        with mock.patch.dict(os.environ, {"SIERRA_CATEGORY_URLS": "", "SIERRA_SHOE_ALERTS_ENABLED": "false"}, clear=False):
            urls = sierra.parse_category_urls()
        self.assertNotIn(sierra.LEATHER_CATEGORY_URL, urls)

    def test_format_alert_line_omits_size_when_unknown(self):
        line = sierra.format_alert_line({
            "name": "Merino Wool T-Shirt",
            "fit_sizes": [],
            "price": 29.99,
            "url": "https://www.sierra.com/example~p~abc/",
        })
        self.assertIn("Merino Wool T-Shirt · $29.99", line)
        self.assertNotIn("Check site", line)

    def test_is_blocked_page_detects_security_failover(self):
        html = (
            "<html><title>Sierra.com: Something went wrong</title>"
            '<link href="/securityfailover/css/sierra.css">'
            "<h1>Looking to shop?</h1></html>"
        )
        self.assertTrue(sierra.has_hard_block_markers(html))
        self.assertTrue(sierra.is_blocked_page(html))
        self.assertFalse(
            sierra.page_is_ready(
                "https://www.sierra.com/s~merino-wool-men/men~d~5284/?perPage=96",
                html,
            )
        )

    def test_challenge_page_is_not_hard_block(self):
        html = (
            '<div id="sec-if-cpt-container" role="main">'
            '<p class="scf-akamai-protected-by">Powered and protected by</p>'
            '<div class="behavioral-content"></div></div>'
        )
        self.assertFalse(sierra.has_hard_block_markers(html))
        self.assertTrue(sierra.is_challenge_page(html))
        self.assertTrue(sierra.is_blocked_page(html))
        self.assertFalse(
            sierra.page_is_ready(
                "https://www.sierra.com/s~merino-wool-men/men~d~5284/?perPage=96",
                html,
            )
        )

    def test_page_is_ready_for_category_listing(self):
        html = "x" * 6000 + 'href="/smartwool-merino~p~5vcrt/"'
        self.assertTrue(
            sierra.page_is_ready(
                "https://www.sierra.com/s~merino-wool-men/men~d~5284/?perPage=96",
                html,
            )
        )

    def test_browser_fetch_retries_after_block(self):
        fetcher = sierra.BrowserFetcher()
        fetcher._browser = object()
        fetcher._page = mock.Mock()
        good_html = "x" * 6000 + 'href="/smartwool-merino~p~5vcrt/"'
        with mock.patch.object(
            fetcher,
            "_fetch_once",
            side_effect=[
                RuntimeError("Sierra fetch timed out or blocked: url"),
                good_html,
            ],
        ) as fetch_once, mock.patch.object(
            fetcher, "_reset_page"
        ) as reset_page, mock.patch.dict(
            os.environ, {"SIERRA_BROWSER_FETCH_ATTEMPTS": "3"}
        ), mock.patch(
            "sierra_monitor.time.sleep"
        ):
            html = fetcher.fetch("https://www.sierra.com/s~merino-wool-men/men~d~5284/")
        self.assertEqual(html, good_html)
        self.assertEqual(fetch_once.call_count, 2)
        reset_page.assert_called_once()


class FitPreferredSizesTests(unittest.TestCase):
    def test_preferred_sizes_from_profile(self):
        self.assertEqual(fit.preferred_sizes(PROFILE, category="tops"), ["S", "M"])
        self.assertEqual(fit.preferred_sizes(PROFILE, category="bottoms"), ["M", "30", "31"])
        self.assertEqual(fit.preferred_sizes(PROFILE, category="tops", brand="sierra"), ["M"])
        self.assertEqual(fit.preferred_sizes(PROFILE, category="bottoms", brand="sierra"), ["M", "30", "31"])


if __name__ == "__main__":
    unittest.main()
