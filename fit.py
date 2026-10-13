import json
import os
import re

FIT_PROFILE_FILE = os.getenv("FIT_PROFILE_FILE", "fit_profile.json")
FIT_STRICTNESS = os.getenv("FIT_STRICTNESS", "strict").strip().lower()

MEASUREMENT_KEYS = (
    "chest_cm",
    "waist_cm",
    "hip_cm",
    "inseam_cm",
    "torso_cm",
)

FIT_TOLERANCE_CM = {
    "strict": 0.0,
    "moderate": 1.5,
    "loose": 3.0,
}

DEFAULT_UNISEX_SHOE_BRANDS = frozenset({
    "vivobarefoot",
    "vivo barefoot",
    "vivo",
})

DEFAULT_WOMEN_SHOE_US_OFFSET = 1.5
DEFAULT_WOMEN_SHOE_EU_OFFSET = 1.5


def profile_gender(profile):
    return str((profile or {}).get("gender", "men")).strip().lower()


def normalize_brand_key(brand):
    return re.sub(r"[^a-z0-9]+", " ", str(brand or "").strip().lower()).strip()


def shoe_sizing_config(profile):
    config = (profile or {}).get("shoe_sizing") or {}
    unisex = {
        normalize_brand_key(name)
        for name in config.get("unisex_brands", [])
    }
    unisex.update(DEFAULT_UNISEX_SHOE_BRANDS)
    return {
        "women_us_offset": float(config.get("women_us_offset", DEFAULT_WOMEN_SHOE_US_OFFSET)),
        "women_eu_offset": float(config.get("women_eu_offset", DEFAULT_WOMEN_SHOE_EU_OFFSET)),
        "unisex_brands": unisex,
    }


def is_unisex_shoe_brand(profile, brand):
    brand_key = normalize_brand_key(brand)
    return brand_key in shoe_sizing_config(profile)["unisex_brands"]


def convert_mens_shoe_to_womens(profile, shoe_us=None, shoe_eu=None):
    config = shoe_sizing_config(profile)
    converted = {}
    if shoe_us is not None:
        converted["shoe_us"] = float(shoe_us) + config["women_us_offset"]
    if shoe_eu is not None:
        converted["shoe_eu"] = float(shoe_eu) + config["women_eu_offset"]
    return converted


def shoe_search_targets(profile, brand):
    if not profile:
        return []

    shoe_us = profile.get("shoe_us")
    shoe_eu = profile.get("shoe_eu")
    targets = [{
        "gender": profile_gender(profile),
        "shoe_us": shoe_us,
        "shoe_eu": shoe_eu,
        "reason": "primary",
    }]

    if is_unisex_shoe_brand(profile, brand):
        targets.append({
            "gender": "women",
            "shoe_us": shoe_us,
            "shoe_eu": shoe_eu,
            "reason": "unisex_same_size",
        })
        return targets

    converted = convert_mens_shoe_to_womens(profile, shoe_us=shoe_us, shoe_eu=shoe_eu)
    targets.append({
        "gender": "women",
        "shoe_us": converted.get("shoe_us"),
        "shoe_eu": converted.get("shoe_eu"),
        "reason": "converted_from_mens",
    })
    return targets


def parse_shoe_label(label):
    text = str(label).strip().upper()
    numbers = [float(match) for match in re.findall(r"\d+(?:\.\d+)?", text)]
    if not numbers:
        return None, None

    if "EU" in text or re.search(r"\b4[0-9]\b", text):
        return None, numbers[-1]
    if len(numbers) == 1:
        number = numbers[0]
        if number >= 30:
            return None, number
        return number, None
    return numbers[0], numbers[-1]


def shoe_target_matches_label(target, label, tolerance=0.0):
    label_us, label_eu = parse_shoe_label(label)
    target_us = target.get("shoe_us")
    target_eu = target.get("shoe_eu")

    if target_us is not None and label_us is not None:
        return abs(float(label_us) - float(target_us)) <= tolerance
    if target_eu is not None and label_eu is not None:
        return abs(float(label_eu) - float(target_eu)) <= tolerance
    if target_us is not None and label_eu is not None:
        converted = convert_mens_shoe_to_womens({"shoe_sizing": {}}, shoe_us=target_us)
        return abs(float(label_eu) - converted["shoe_eu"]) <= tolerance
    return False


def filter_shoe_labels_by_fit(profile, labels, brand, strictness=None):
    if not profile:
        return list(labels)

    strict = (strictness or FIT_STRICTNESS).strip().lower()
    tolerance = 0.0 if strict == "strict" else tolerance_cm(strict)
    targets = shoe_search_targets(profile, brand)
    matches = []

    for label in labels:
        for target in targets:
            if shoe_target_matches_label(target, label, tolerance=tolerance):
                matches.append({
                    "label": str(label).strip(),
                    "fits": True,
                    "confidence": "good" if target["reason"] == "primary" else target["reason"],
                    "gender": target["gender"],
                    "target": target,
                })
                break
    return matches


def sock_size_charts(profile):
    return (profile or {}).get("sock_size_charts") or {}


def sock_chart_for_brand(profile, brand):
    charts = sock_size_charts(profile)
    brand_key = normalize_brand_key(brand)
    if brand_key in charts:
        return charts[brand_key]

    for key, chart in charts.items():
        normalized_key = normalize_brand_key(key)
        if normalized_key and (
            normalized_key in brand_key or brand_key in normalized_key
        ):
            return chart

    return charts.get("default_mens") or charts.get("default") or {}


def chart_entry_shoe_us_range(entry):
    if isinstance(entry, dict):
        raw = entry.get("shoe_us")
    elif isinstance(entry, (list, tuple)):
        raw = entry
    else:
        return None, None
    if not raw or len(raw) < 2:
        return None, None
    return float(raw[0]), float(raw[1])


def lookup_chart_label(chart, label):
    target = str(label).strip().upper()
    for key in chart:
        if str(key).strip().upper() == target:
            return key
    return None


def parse_shoe_range_from_label(label):
    text = str(label).strip().upper()
    range_match = re.search(
        r"(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)",
        text,
    )
    if range_match:
        return float(range_match.group(1)), float(range_match.group(2))

    single_us, _single_eu = parse_shoe_label(label)
    if single_us is not None:
        return single_us, single_us
    return None, None


def user_shoe_us_in_range(profile, min_us, max_us, tolerance=0.0):
    user_us = profile.get("shoe_us")
    if user_us is None or min_us is None or max_us is None:
        return False
    return (
        float(min_us) - tolerance
        <= float(user_us)
        <= float(max_us) + tolerance
    )


def sock_label_matches_user(profile, label, brand, strictness=None):
    if not profile:
        return None

    strict = (strictness or FIT_STRICTNESS).strip().lower()
    tolerance = 0.0 if strict == "strict" else tolerance_cm(strict)
    chart = sock_chart_for_brand(profile, brand)
    chart_key = lookup_chart_label(chart, label)
    if chart_key is not None:
        min_us, max_us = chart_entry_shoe_us_range(chart[chart_key])
        if min_us is not None and user_shoe_us_in_range(profile, min_us, max_us, tolerance):
            return {
                "label": str(label).strip(),
                "fits": True,
                "confidence": "brand_chart",
                "brand": normalize_brand_key(brand),
                "chart_label": chart_key,
            }

    min_us, max_us = parse_shoe_range_from_label(label)
    if min_us is not None and user_shoe_us_in_range(profile, min_us, max_us, tolerance):
        return {
            "label": str(label).strip(),
            "fits": True,
            "confidence": "label_range",
            "brand": normalize_brand_key(brand),
        }

    shoe_matches = filter_shoe_labels_by_fit(
        profile,
        [label],
        brand,
        strictness=strictness,
    )
    if shoe_matches:
        match = dict(shoe_matches[0])
        match["confidence"] = "shoe_label"
        return match
    return None


def filter_sock_labels_by_fit(profile, labels, brand, strictness=None):
    if not profile:
        return list(labels)

    matches = []
    for label in labels:
        match = sock_label_matches_user(profile, label, brand, strictness=strictness)
        if match:
            matches.append(match)
    return matches


def product_gender_matches_profile(profile, product_gender):
    target_gender = profile_gender(profile)
    normalized = str(product_gender or "").strip().lower()
    if not normalized or normalized in {"unisex", "neutral"}:
        return True
    if target_gender == "men":
        return normalized in {"men", "male", "mens", "man's"}
    if target_gender == "women":
        return normalized in {"women", "female", "womens", "woman's"}
    return normalized == target_gender


def load_fit_profile(path=None):
    profile_path = path or FIT_PROFILE_FILE
    if not os.path.exists(profile_path):
        return None
    with open(profile_path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def measurement_midpoint(value):
    if isinstance(value, (list, tuple)):
        if not value:
            return None
        if len(value) == 1:
            return float(value[0])
        return (float(value[0]) + float(value[-1])) / 2.0
    if value is None:
        return None
    return float(value)


def measurement_range(value):
    if isinstance(value, (list, tuple)):
        if not value:
            return None, None
        if len(value) == 1:
            number = float(value[0])
            return number, number
        return float(value[0]), float(value[-1])
    if value is None:
        return None, None
    number = float(value)
    return number, number


def parse_size_chart_range(raw):
    if raw is None:
        return None, None
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        return float(raw[0]), float(raw[1])
    if isinstance(raw, (int, float)):
        number = float(raw)
        return number, number
    text = str(raw).strip()
    if not text:
        return None, None
    numbers = [float(match) for match in re.findall(r"\d+(?:\.\d+)?", text)]
    if not numbers:
        return None, None
    if len(numbers) == 1:
        return numbers[0], numbers[0]
    return numbers[0], numbers[-1]


def tolerance_cm(strictness=None):
    key = (strictness or FIT_STRICTNESS).strip().lower()
    return FIT_TOLERANCE_CM.get(key, FIT_TOLERANCE_CM["strict"])


def measurement_fits_range(user_value, size_min, size_max, strictness=None):
    user_min, user_max = measurement_range(user_value)
    if user_min is None or size_min is None or size_max is None:
        return None

    slack = tolerance_cm(strictness)
    if strictness == "loose":
        return user_max + slack >= size_min and user_min - slack <= size_max
    return user_min >= size_min - slack and user_max <= size_max + slack


def size_chart_entry_measurements(entry):
    measurements = {}
    for key in MEASUREMENT_KEYS:
        if key in entry:
            measurements[key] = parse_size_chart_range(entry[key])
            continue
        short_key = key.replace("_cm", "")
        if short_key in entry:
            measurements[key] = parse_size_chart_range(entry[short_key])
    return measurements


def evaluate_size_fit(profile, size_entry, required_keys, strictness=None):
    chart_measurements = size_chart_entry_measurements(size_entry)
    available = []
    for key in required_keys:
        if key not in chart_measurements:
            continue
        size_min, size_max = chart_measurements[key]
        if size_min is None or size_max is None:
            continue
        available.append(key)
    if not available:
        return {
            "label": str(size_entry.get("label", "")).strip(),
            "fits": None,
            "confidence": "unknown",
            "matched": [],
            "failed": [],
            "missing": list(required_keys),
        }

    strict = (strictness or FIT_STRICTNESS).strip().lower()
    matched = []
    failed = []
    for key in available:
        size_min, size_max = chart_measurements[key]
        result = measurement_fits_range(profile.get(key), size_min, size_max, strictness=strict)
        if result:
            matched.append(key)
        else:
            failed.append(key)

    if strict == "loose":
        fits = len(failed) == 0 or (len(matched) >= len(available) - 1 and matched)
    elif strict == "moderate":
        fits = len(failed) == 0
    else:
        fits = len(failed) == 0 and len(matched) == len(available)

    if len(available) < len(required_keys):
        confidence = "partial"
    elif fits:
        confidence = "good"
    elif fits is False:
        confidence = "poor"
    else:
        confidence = "unknown"

    return {
        "label": str(size_entry.get("label", "")).strip(),
        "fits": fits,
        "confidence": confidence,
        "matched": matched,
        "failed": failed,
        "missing": [key for key in required_keys if key not in available],
    }


def recommend_sizes(profile, size_chart, category="tops", strictness=None, brand=None):
    if not profile:
        return []

    category = str(category).strip().lower()
    if category in {"bottoms", "pants", "trousers", "jeans"}:
        required_keys = ("waist_cm", "hip_cm", "inseam_cm")
    elif category in {"shoes", "footwear"}:
        if size_chart and isinstance(size_chart[0], dict):
            labels = [str(entry.get("label", "")).strip() for entry in size_chart if entry.get("label")]
        else:
            labels = [str(label).strip() for label in (size_chart or []) if str(label).strip()]
        return filter_shoe_labels_by_fit(profile, labels, brand, strictness=strictness)
    else:
        required_keys = ("chest_cm", "waist_cm")

    strict = (strictness or FIT_STRICTNESS).strip().lower()
    evaluations = [
        evaluate_size_fit(profile, entry, required_keys, strictness=strict)
        for entry in size_chart
    ]
    by_label = {row["label"]: row for row in evaluations if row["label"]}

    if strict == "strict":
        good = [row for row in evaluations if row["fits"] is True and row["confidence"] == "good"]
        if brand:
            for label in filter_labels_by_fit(profile, by_label.keys(), brand, category=category):
                row = by_label.get(label)
                if row and row not in good:
                    confirmed = dict(row)
                    confirmed["confidence"] = "known_good"
                    confirmed["fits"] = True
                    good.append(confirmed)
        return good
    if strict == "moderate":
        return [row for row in evaluations if row["fits"] is True]
    return [row for row in evaluations if row["fits"] is not False]


def preferred_sizes(profile, category="tops", brand=None):
    if not profile:
        return []

    brand_key = normalize_brand_key(brand or "uniqlo")
    known = profile.get("known_good_sizes") or {}
    entry = known.get(brand_key) or known.get("uniqlo") or {}
    category = str(category).strip().lower()
    if category in {"bottoms", "pants", "trousers", "jeans", "waist"}:
        return list(entry.get("bottoms") or [])
    if category in {"shoes", "footwear"}:
        return []
    return list(entry.get("tops") or [])


def filter_labels_by_fit(profile, labels, brand, category="tops", strictness=None):
    if not profile:
        return list(labels)

    known = (profile.get("known_good_sizes") or {})
    brand_key = normalize_brand_key(brand)
    entry = known.get(brand_key) or known.get("uniqlo") or {}
    preferred = entry.get(category) or entry.get("tops") or []
    if not preferred:
        return list(labels)

    preferred_set = {str(label).strip().upper() for label in preferred}
    return [label for label in labels if str(label).strip().upper() in preferred_set]


def format_profile_summary(profile):
    if not profile:
        return "No fit profile loaded."

    parts = [
        f"gender {profile_gender(profile)}",
        f"{profile.get('height_cm')} cm",
        f"chest {measurement_range(profile.get('chest_cm'))[0]}-{measurement_range(profile.get('chest_cm'))[1]} cm",
        f"waist {measurement_range(profile.get('waist_cm'))[0]}-{measurement_range(profile.get('waist_cm'))[1]} cm",
        f"hip {measurement_range(profile.get('hip_cm'))[0]}-{measurement_range(profile.get('hip_cm'))[1]} cm",
        f"inseam {measurement_range(profile.get('inseam_cm'))[0]}-{measurement_range(profile.get('inseam_cm'))[1]} cm",
        f"fit {profile.get('fit_preference', 'regular')}",
    ]
    return ", ".join(str(part) for part in parts if part)
