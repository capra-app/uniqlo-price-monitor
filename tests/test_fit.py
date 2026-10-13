import json
import os
import tempfile
import unittest

import fit


PROFILE = {
    "units": "cm",
    "gender": "men",
    "height_cm": 176,
    "weight_kg": 68,
    "chest_cm": [97, 97],
    "waist_cm": [80, 80],
    "hip_cm": [92, 93],
    "inseam_cm": [77, 77],
    "torso_cm": [49, 49],
    "fit_preference": "regular",
    "shoe_us": 9.5,
    "shoe_eu": 43,
    "shoe_sizing": {
        "women_us_offset": 1.5,
        "women_eu_offset": 1.5,
        "unisex_brands": ["vivobarefoot", "vivo barefoot", "vivo"],
    },
    "known_good_sizes": {
        "uniqlo": {
            "tops": ["S", "M"],
            "bottoms": ["M", "30", "31"],
        }
    },
    "sock_size_charts": {
        "smartwool": {
            "M": {"shoe_us": [6, 8.5]},
            "L": {"shoe_us": [9, 11.5]},
        },
    },
}

UNIQLO_TOPS_CHART = [
    {"label": "S", "chest_cm": [89, 97], "waist_cm": [71, 79]},
    {"label": "M", "chest_cm": [97, 104], "waist_cm": [79, 87]},
    {"label": "L", "chest_cm": [107, 114], "waist_cm": [91, 99]},
]

UNIQLO_BOTTOMS_CHART = [
    {"label": "30", "waist_cm": [76, 81], "hip_cm": [91, 96], "inseam_cm": [76, 78]},
    {"label": "31", "waist_cm": [79, 84], "hip_cm": [94, 99], "inseam_cm": [76, 78]},
    {"label": "32", "waist_cm": [81, 86], "hip_cm": [97, 102], "inseam_cm": [76, 78]},
]


class FitProfileTests(unittest.TestCase):
    def test_load_fit_profile(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "fit_profile.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(PROFILE, handle)
            loaded = fit.load_fit_profile(path)
            self.assertEqual(loaded["height_cm"], 176)

    def test_recommend_uniqlo_tops_strict(self):
        matches = fit.recommend_sizes(
            PROFILE,
            UNIQLO_TOPS_CHART,
            category="tops",
            strictness="strict",
            brand="uniqlo",
        )
        labels = [row["label"] for row in matches]
        self.assertIn("S", labels)
        self.assertIn("M", labels)
        self.assertNotIn("L", labels)

    def test_recommend_uniqlo_bottoms_strict(self):
        matches = fit.recommend_sizes(
            PROFILE,
            UNIQLO_BOTTOMS_CHART,
            category="bottoms",
            strictness="strict",
            brand="uniqlo",
        )
        labels = [row["label"] for row in matches]
        self.assertIn("30", labels)
        self.assertIn("31", labels)
        self.assertNotIn("32", labels)

    def test_filter_labels_by_known_good_sizes(self):
        labels = fit.filter_labels_by_fit(PROFILE, ["S", "M", "L"], brand="uniqlo", category="tops")
        self.assertEqual(labels, ["S", "M"])

    def test_unknown_chart_returns_empty_in_strict_mode(self):
        chart = [{"label": "M"}]
        matches = fit.recommend_sizes(PROFILE, chart, category="tops", strictness="strict")
        self.assertEqual(matches, [])

    def test_shoe_search_targets_for_unisex_brand(self):
        targets = fit.shoe_search_targets(PROFILE, "Vivobarefoot")
        self.assertEqual(len(targets), 2)
        self.assertEqual(targets[0]["gender"], "men")
        self.assertEqual(targets[1]["gender"], "women")
        self.assertEqual(targets[0]["shoe_us"], 9.5)
        self.assertEqual(targets[1]["shoe_us"], 9.5)
        self.assertEqual(targets[1]["reason"], "unisex_same_size")

    def test_shoe_search_targets_for_gendered_brand(self):
        targets = fit.shoe_search_targets(PROFILE, "Salomon")
        self.assertEqual(targets[1]["gender"], "women")
        self.assertEqual(targets[1]["shoe_us"], 11.0)
        self.assertEqual(targets[1]["shoe_eu"], 44.5)
        self.assertEqual(targets[1]["reason"], "converted_from_mens")

    def test_filter_shoe_labels_by_fit(self):
        matches = fit.filter_shoe_labels_by_fit(PROFILE, ["9.5", "10", "11"], brand="Salomon")
        labels = [row["label"] for row in matches]
        self.assertIn("9.5", labels)
        self.assertIn("11", labels)
        self.assertNotIn("10", labels)

    def test_filter_sock_labels_by_brand_chart(self):
        matches = fit.filter_sock_labels_by_fit(
            PROFILE,
            ["M", "L", "XL"],
            brand="SmartWool",
        )
        labels = [row["label"] for row in matches]
        self.assertEqual(labels, ["L"])
        self.assertEqual(matches[0]["confidence"], "brand_chart")

    def test_filter_sock_labels_by_numeric_range(self):
        profile = dict(PROFILE)
        profile["sock_size_charts"] = {}
        matches = fit.filter_sock_labels_by_fit(
            profile,
            ["8-9", "9.5-12", "12-14"],
            brand="Unknown Brand",
        )
        labels = [row["label"] for row in matches]
        self.assertEqual(labels, ["9.5-12"])

    def test_product_gender_matches_profile(self):
        self.assertTrue(fit.product_gender_matches_profile(PROFILE, "Men"))
        self.assertFalse(fit.product_gender_matches_profile(PROFILE, "Women"))


if __name__ == "__main__":
    unittest.main()
