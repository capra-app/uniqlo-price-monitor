import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

STATE_FILE = os.getenv("STATE_FILE", "last_seen_prices.json")
HEALTH_FILE = os.getenv("HEALTH_FILE", "monitor_health.json")

BASE = "https://www.uniqlo.com/us/api/commerce/v5/en/products"
DEFAULT_SEARCHES = [
    ("UNIQLO U", "uniqlo u"),
    ("UNIQLO C", "uniqlo c"),
]
DEFAULT_WOOL_SEARCHES = ["wool", "merino wool"]
DEFAULT_CASHMERE_SEARCHES = ["cashmere"]
MATERIAL_LINE_LABEL = "Wool/Cashmere 60%+"

ONLINE_STOCK_STATUSES = frozenset({"IN_STOCK", "LOW_STOCK"})
DISCORD_SUPPRESS_EMBEDS = 4
DISCORD_CONTENT_LIMIT = 1900

MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
RETRY_BACKOFF_SEC = float(os.getenv("RETRY_BACKOFF_SEC", "1.5"))
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "6"))
FAILURE_ALERT_THRESHOLD = int(os.getenv("FAILURE_ALERT_THRESHOLD", "2"))

DRY_RUN = os.getenv("DRY_RUN", "").strip().lower() in {"1", "true", "yes"}


def parse_target_sizes():
    raw = os.getenv("TARGET_SIZES", "S,M,30,31")
    return [part.strip().upper() for part in raw.split(",") if part.strip()]


def parse_searches():
    raw = os.getenv("SEARCH_QUERIES", "").strip()
    if not raw:
        return list(DEFAULT_SEARCHES)
    searches = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            label, query = part.split(":", 1)
        else:
            label, query = part, part
        searches.append((label.strip(), query.strip()))
    return searches or list(DEFAULT_SEARCHES)


def parse_wool_searches():
    raw = os.getenv("WOOL_SEARCH_QUERIES", "").strip()
    if not raw:
        return list(DEFAULT_WOOL_SEARCHES)
    return [part.strip() for part in raw.split(",") if part.strip()]


def parse_cashmere_searches():
    raw = os.getenv("CASHMERE_SEARCH_QUERIES", "").strip()
    if not raw:
        return list(DEFAULT_CASHMERE_SEARCHES)
    return [part.strip() for part in raw.split(",") if part.strip()]


def parse_min_wool_percent():
    return float(os.getenv("MIN_WOOL_PERCENT", "60"))


def parse_min_cashmere_percent():
    return float(os.getenv("MIN_CASHMERE_PERCENT", "60"))


TARGET_SIZES = parse_target_sizes()
SEARCHES = parse_searches()
WOOL_SEARCHES = parse_wool_searches()
CASHMERE_SEARCHES = parse_cashmere_searches()
MIN_WOOL_PERCENT = parse_min_wool_percent()
MIN_CASHMERE_PERCENT = parse_min_cashmere_percent()


def make_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.uniqlo.com/us/en/",
    })
    request_with_retry(session, "GET", "https://www.uniqlo.com/us/en/", timeout=15)
    return session


def request_with_retry(session, method, url, **kwargs):
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            response = session.request(method, url, **kwargs)
            if response.status_code in {429, 500, 502, 503, 504}:
                time.sleep(RETRY_BACKOFF_SEC * (2 ** attempt))
                continue
            return response
        except requests.RequestException as exc:
            last_error = exc
            time.sleep(RETRY_BACKOFF_SEC * (2 ** attempt))
    if last_error:
        raise last_error
    return None


def load_json_file(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as handle:
        try:
            return json.load(handle)
        except json.JSONDecodeError:
            return default


def save_json_file(path, data):
    if DRY_RUN:
        print(f"Dry run: would write {path}", flush=True)
        return
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=4)


def load_state():
    return load_json_file(STATE_FILE, {})


def save_state(state):
    save_json_file(STATE_FILE, state)


def load_health():
    health = load_json_file(HEALTH_FILE, {})
    health.setdefault("consecutive_failures", 0)
    return health


def save_health(health):
    save_json_file(HEALTH_FILE, health)


def record_success(health):
    health["consecutive_failures"] = 0
    save_health(health)


def record_failure(health, message):
    health["consecutive_failures"] = health.get("consecutive_failures", 0) + 1
    save_health(health)
    print(f"Monitor failure ({health['consecutive_failures']}): {message}", flush=True)
    if health["consecutive_failures"] >= FAILURE_ALERT_THRESHOLD:
        notify_error(message)


def send_notification(content):
    if DRY_RUN:
        print(f"Dry run notification:\n{content}", flush=True)
        return

    send_discord_message({"content": content})
    send_telegram_message(content)


def send_discord_message(payload):
    if not DISCORD_WEBHOOK_URL:
        return

    payload = dict(payload)
    payload["flags"] = payload.get("flags", 0) | DISCORD_SUPPRESS_EMBEDS

    for attempt in range(MAX_RETRIES):
        try:
            response = requests.post(
                DISCORD_WEBHOOK_URL,
                json=payload,
                timeout=10,
            )
            if response.status_code in {200, 204}:
                return
            print(
                f"Discord webhook failed ({response.status_code}): {response.text[:200]}",
                flush=True,
            )
        except requests.RequestException as exc:
            print(f"Discord request failed: {exc}", flush=True)
        time.sleep(RETRY_BACKOFF_SEC * (2 ** attempt))


def send_telegram_message(content):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for attempt in range(MAX_RETRIES):
        try:
            response = requests.post(
                url,
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": content,
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
            if response.status_code == 200 and response.json().get("ok"):
                return
            print(
                f"Telegram failed ({response.status_code}): {response.text[:200]}",
                flush=True,
            )
        except requests.RequestException as exc:
            print(f"Telegram request failed: {exc}", flush=True)
        time.sleep(RETRY_BACKOFF_SEC * (2 ** attempt))


def notify_error(message):
    send_notification(f"Uniqlo monitor error: {message}")


def format_price(value):
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


PRODUCT_KEY_PREFIX = "__product__"


def format_linked_line(label, url):
    return f"[{label}](<{url}>)"


def format_sale_line(item):
    size = item["size_label"].replace(" (", "/").replace(")", "")
    price_text = format_price(item["price"])
    label = f"{item['name']} · {size} · ${price_text}"

    base_price = item.get("base_price", 0)
    current_price = item["price"]
    if base_price > current_price:
        discount_pct = round((1 - (current_price / base_price)) * 100)
        base_text = format_price(base_price)
        label += f" (was ${base_text}, -{discount_pct}%)"

    return format_linked_line(label, item["url"])


def send_new_sale_digest(sale_items):
    if not sale_items:
        return

    lines = [
        format_sale_line(item)
        for item in sorted(sale_items, key=lambda row: (row["name"], row["size_label"]))
    ]
    for content in chunk_lines(lines):
        send_notification(content)


def format_new_product_line(item):
    price_text = format_price(item["price"])
    label = f"{item['name']} · ${price_text}"
    return format_linked_line(label, item["url"])


def chunk_lines(lines):
    messages = []
    chunk = ""

    for line in lines:
        next_chunk = line if not chunk else f"{chunk}\n{line}"
        if len(next_chunk) <= DISCORD_CONTENT_LIMIT:
            chunk = next_chunk
        else:
            if chunk:
                messages.append(chunk)
            chunk = line

    if chunk:
        messages.append(chunk)
    return messages


def send_new_products_digest(new_products):
    if not new_products:
        return

    lines = [
        format_new_product_line(item)
        for item in sorted(new_products, key=lambda row: row["name"])
    ]
    for content in chunk_lines(lines):
        send_notification(content)


def product_registry_key(product_id):
    return f"{PRODUCT_KEY_PREFIX}{product_id}"


def is_product_known(state, product_id):
    if product_registry_key(product_id) in state:
        return True
    prefix = f"{product_id}_"
    return any(key.startswith(prefix) for key in state)


def mark_product_known(state, product_id):
    state[product_registry_key(product_id)] = {"seen": True}


def variant_matches_criteria(variant, stock_map):
    raw_size = variant_size_name(variant)
    if not raw_size:
        return False

    clean_size = normalize_size(raw_size)
    if clean_size not in TARGET_SIZES and raw_size.upper() not in TARGET_SIZES:
        return False
    if not variant_online_purchasable(variant, stock_map):
        return False

    _, _, current_price = variant_prices(variant)
    return current_price > 0


def fetch_by_query(session, query):
    items = []
    offset = 0
    while True:
        response = request_with_retry(
            session,
            "GET",
            BASE,
            params={"q": query, "offset": offset, "limit": 100},
            timeout=15,
        )
        if response is None or response.status_code != 200:
            break

        result = response.json().get("result", {})
        batch = result.get("items", [])
        total = result.get("pagination", {}).get("total", 0)
        print(f"  '{query}' offset={offset}: {len(batch)} items (total={total})", flush=True)
        if not batch:
            break

        items.extend(batch)
        offset += 100
        if offset >= total:
            break

    return items


def fetch_product_detail(session, product_id):
    response = request_with_retry(session, "GET", f"{BASE}/{product_id}", timeout=15)
    if response is None or response.status_code != 200:
        return None
    return response.json().get("result")


def product_price_group(item):
    price_group = item.get("priceGroup")
    if price_group:
        return str(price_group)
    plds = item.get("plds") or []
    if plds:
        display_code = str(plds[0].get("displayCode", "000"))
        return display_code.lstrip("0") or "00"
    return "00"


def fetch_product_stock(session, product_id, price_group):
    url = f"{BASE}/{product_id}/price-groups/{price_group}/stock"
    response = request_with_retry(
        session,
        "GET",
        url,
        params={"httpFailure": "true"},
        timeout=15,
    )
    if response is None or response.status_code != 200:
        return {}

    data = response.json()
    if data.get("status") != "ok":
        return {}
    return data.get("result", {})


def fetch_product_bundle(item):
    session = make_session()
    product_id = item.get("productId", "")
    detail = fetch_product_detail(session, product_id)
    price_group = product_price_group(item)
    stock_map = fetch_product_stock(session, product_id, price_group)
    return product_id, detail, stock_map


def apply_product(item, line_label, detail, stock_map, historical_state, from_uc=True):
    product_id = item.get("productId", "")
    name = item.get("name", "")
    if not detail:
        return product_id, [], None

    composition = detail.get("composition", "")
    if not qualifies_for_monitoring(from_uc, composition):
        wool_pct = max_fiber_percent(composition, "wool")
        cashmere_pct = max_fiber_percent(composition, "cashmere")
        print(
            f"  SKIP {name}: wool {wool_pct:g}%, cashmere {cashmere_pct:g}% "
            f"(need {MIN_WOOL_PERCENT:g}%+ wool or {MIN_CASHMERE_PERCENT:g}%+ cashmere)",
            flush=True,
        )
        mark_product_known(historical_state, product_id)
        return product_id, [], None

    known_before = is_product_known(historical_state, product_id)
    new_items = []
    new_product = None
    matching_prices = []
    variants = detail.get("l2s", [])
    if variants:
        for variant in variants:
            if variant_matches_criteria(variant, stock_map):
                _, _, price = variant_prices(variant)
                matching_prices.append(price)

        target_sizes_seen = set()
        for variant in variants:
            raw_size = variant_size_name(variant)
            clean_size = normalize_size(raw_size)
            if clean_size in TARGET_SIZES or raw_size.upper() in TARGET_SIZES:
                target_sizes_seen.add(clean_size)
            new_items = record_variant(
                variant,
                product_id,
                name,
                line_label,
                historical_state,
                new_items,
                stock_map,
            )
        for clean_size in target_sizes_seen:
            legacy_key = f"{product_id}_{clean_size}"
            if legacy_key in historical_state:
                del historical_state[legacy_key]

        if not known_before and matching_prices:
            new_product = {
                "name": name,
                "url": product_url(product_id),
                "price": min(matching_prices),
                "line_label": line_label,
            }
            print(f"  NEW PRODUCT: {name}", flush=True)

        mark_product_known(historical_state, product_id)
        return product_id, new_items, new_product

    prices = item.get("prices") or {}
    base = float((prices.get("base") or {}).get("value", 0) or 0)
    promo_raw = prices.get("promo")
    promo = float((promo_raw or {}).get("value", 0) or 0) if promo_raw else 0
    current = promo if (0 < promo < base) else base
    state_key = f"{product_id}_NOSZ"
    on_sale = 0 < promo < base
    previous = get_state_entry(historical_state, state_key)

    if current > 0:
        if should_alert(on_sale, previous, current):
            new_items.append({
                "name": name,
                "size_label": "Check site",
                "price": current,
                "base_price": base,
                "line_label": line_label,
                "url": product_url(product_id),
            })
            print(f"  ALERT: {name} (no size data) ${current}", flush=True)
        set_state_entry(historical_state, state_key, current, on_sale)
        if not known_before:
            new_product = {
                "name": name,
                "url": product_url(product_id),
                "price": current,
                "line_label": line_label,
            }
            print(f"  NEW PRODUCT: {name}", flush=True)

    mark_product_known(historical_state, product_id)
    return product_id, new_items, new_product


def get_state_entry(state, key):
    value = state.get(key)
    if value is None:
        return None
    if isinstance(value, dict):
        on_sale = value.get("on_sale")
        return {
            "price": float(value.get("price", 0) or 0),
            "on_sale": bool(on_sale) if on_sale is not None else None,
        }
    return {"price": float(value), "on_sale": None}


def set_state_entry(state, key, price, on_sale):
    state[key] = {"price": price, "on_sale": on_sale}


def variant_color_code(variant):
    return str((variant.get("color") or {}).get("displayCode", "UNK")).strip()


def state_keys(product_id, color_code, clean_size):
    variant_key = f"{product_id}_{color_code}_{clean_size}"
    legacy_key = f"{product_id}_{clean_size}"
    return variant_key, legacy_key


def lookup_previous(state, variant_key, legacy_key):
    previous = get_state_entry(state, variant_key)
    if previous is not None:
        return previous
    legacy = get_state_entry(state, legacy_key)
    if legacy is None:
        return None
    return {"price": legacy["price"], "on_sale": None}


def should_alert(on_sale, previous, current_price):
    if not on_sale:
        return False
    if previous is None:
        return True
    if previous.get("on_sale") is not True:
        return True
    return previous.get("price", 0) > current_price


def normalize_size(size_str):
    size = str(size_str).strip().upper().replace(" ", "")
    for suffix in ("INCH", "IN", "CM"):
        size = size.split(suffix)[0]
    if "(" in size:
        size = size.split("(")[0]
    return size


def is_men_item(item):
    gender_name = str(item.get("genderName", "")).upper().strip()
    size_gender = str(item.get("sizeGender", "")).upper().strip()
    gender_category = str(item.get("genderCategory", "")).upper().strip()

    if gender_name == "WOMEN" or size_gender == "WOMEN" or gender_category == "WOMEN":
        return False
    if gender_name == "MEN" or size_gender == "MEN" or gender_category == "MEN":
        return True
    return gender_name == "UNISEX" and size_gender == "MEN"


def variant_size_name(variant):
    if variant.get("sizeName"):
        return str(variant["sizeName"]).strip()
    size = variant.get("size") or {}
    return str(size.get("name", "")).strip()


def variant_online_purchasable(variant, stock_map):
    if not stock_map:
        return bool(variant.get("sales", False))

    l2_id = str(variant.get("l2Id", "")).strip()
    if not l2_id:
        return False

    status = stock_map.get(l2_id, {}).get("statusCode", "")
    return status in ONLINE_STOCK_STATUSES


def variant_prices(variant):
    prices = variant.get("prices") or {}
    base = float((prices.get("base") or {}).get("value", 0) or 0)
    promo_raw = prices.get("promo")
    promo = float((promo_raw or {}).get("value", 0) or 0) if promo_raw else 0
    current = promo if (0 < promo < base) else base
    return base, promo, current


def variant_has_discount_flag(variant):
    for flag in (variant.get("flags") or {}).get("priceFlags", []):
        if flag.get("code") == "discount":
            return True
    return False


def variant_on_sale(variant, base_price, promo_price):
    if 0 < promo_price < base_price:
        return True
    return variant_has_discount_flag(variant)


def product_url(product_id, variant=None):
    url = f"https://www.uniqlo.com/us/en/products/{product_id}/00"
    if not variant:
        return url

    color_code = (variant.get("color") or {}).get("displayCode")
    size_code = (variant.get("size") or {}).get("displayCode")
    params = []
    if color_code:
        params.append(f"colorDisplayCode={color_code}")
    if size_code:
        params.append(f"sizeDisplayCode={size_code}")
    if params:
        url += "?" + "&".join(params)
    return url


def max_fiber_percent(composition, fiber):
    if not composition:
        return 0.0

    text = re.sub(r"<br\s*/?>", " ", composition, flags=re.I)
    text = text.replace("/", " ")
    pattern = rf"(\d+(?:\.\d+)?)\s*%\s*{re.escape(fiber)}"
    percentages = [float(match) for match in re.findall(pattern, text, flags=re.I)]
    if percentages:
        return max(percentages)
    if re.search(rf"\b100\s*%\s*{re.escape(fiber)}\b", text, flags=re.I):
        return 100.0
    return 0.0


def max_wool_percent(composition):
    return max_fiber_percent(composition, "wool")


def max_cashmere_percent(composition):
    return max_fiber_percent(composition, "cashmere")


def qualifies_for_monitoring(from_uc, composition):
    if from_uc:
        return True
    if max_wool_percent(composition) >= MIN_WOOL_PERCENT:
        return True
    return max_cashmere_percent(composition) >= MIN_CASHMERE_PERCENT


def collect_men_products(session):
    seen = {}

    def add_product(product_id, item, line_label, from_uc):
        if product_id in seen:
            entry = seen[product_id]
            if from_uc:
                entry["from_uc"] = True
                entry["line_label"] = line_label
            return
        seen[product_id] = {
            "item": item,
            "line_label": line_label,
            "from_uc": from_uc,
        }

    for line_label, query in SEARCHES:
        items = fetch_by_query(session, query)
        print(f"{line_label}: {len(items)} items fetched", flush=True)
        for item in items:
            if not is_men_item(item):
                continue
            if item.get("storeStockOnly"):
                continue
            product_id = item.get("productId", "")
            if not product_id:
                continue
            add_product(product_id, item, line_label, from_uc=True)

    for query in WOOL_SEARCHES:
        items = fetch_by_query(session, query)
        print(f"Wool search '{query}': {len(items)} items fetched", flush=True)
        for item in items:
            if not is_men_item(item):
                continue
            if item.get("storeStockOnly"):
                continue
            product_id = item.get("productId", "")
            if not product_id:
                continue
            add_product(product_id, item, MATERIAL_LINE_LABEL, from_uc=False)

    for query in CASHMERE_SEARCHES:
        items = fetch_by_query(session, query)
        print(f"Cashmere search '{query}': {len(items)} items fetched", flush=True)
        for item in items:
            if not is_men_item(item):
                continue
            if item.get("storeStockOnly"):
                continue
            product_id = item.get("productId", "")
            if not product_id:
                continue
            add_product(product_id, item, MATERIAL_LINE_LABEL, from_uc=False)

    return [
        (entry["item"], entry["line_label"], entry["from_uc"])
        for entry in seen.values()
    ]


def product_id_from_state_key(key):
    if key.startswith(PRODUCT_KEY_PREFIX):
        return key[len(PRODUCT_KEY_PREFIX):]
    if key.endswith("_NOSZ"):
        return key[: -len("_NOSZ")]
    parts = key.split("_")
    if len(parts) >= 3:
        return "_".join(parts[:-2])
    if len(parts) == 2:
        return parts[0]
    return None


def cleanup_state(state, active_product_ids):
    stale_keys = []
    for key in state:
        product_id = product_id_from_state_key(key)
        if product_id and product_id not in active_product_ids:
            stale_keys.append(key)
    for key in stale_keys:
        del state[key]
    if stale_keys:
        print(f"Removed {len(stale_keys)} stale state entries", flush=True)


def record_variant(
    variant,
    product_id,
    name,
    line_label,
    historical_state,
    new_sale_items,
    stock_map,
):
    raw_size = variant_size_name(variant)
    if not raw_size:
        return new_sale_items

    clean_size = normalize_size(raw_size)
    if clean_size not in TARGET_SIZES and raw_size.upper() not in TARGET_SIZES:
        return new_sale_items

    color_code = variant_color_code(variant)
    state_key, legacy_key = state_keys(product_id, color_code, clean_size)

    if not variant_online_purchasable(variant, stock_map):
        previous = lookup_previous(historical_state, state_key, legacy_key)
        if previous is not None:
            set_state_entry(historical_state, state_key, previous["price"], False)
        return new_sale_items

    base_price, promo_price, current_price = variant_prices(variant)
    if current_price <= 0:
        return new_sale_items

    color_name = (variant.get("color") or {}).get("name", "")
    size_label = f"{raw_size} ({color_name})" if color_name else raw_size
    on_sale = variant_on_sale(variant, base_price, promo_price)
    previous = lookup_previous(historical_state, state_key, legacy_key)

    if should_alert(on_sale, previous, current_price):
        new_sale_items.append({
            "name": name,
            "size_label": size_label,
            "price": current_price,
            "base_price": base_price,
            "line_label": line_label,
            "url": product_url(product_id, variant),
        })
        print(f"  ALERT: {name} ({size_label}) ${current_price}", flush=True)

    set_state_entry(historical_state, state_key, current_price, on_sale)
    return new_sale_items



def main():
    print(f"Target sizes: {TARGET_SIZES}", flush=True)
    print(f"Searches: {SEARCHES}", flush=True)
    print(
        f"Wool searches: {WOOL_SEARCHES} (min {MIN_WOOL_PERCENT:g}%), "
        f"cashmere searches: {CASHMERE_SEARCHES} (min {MIN_CASHMERE_PERCENT:g}%)",
        flush=True,
    )
    print(f"Dry run: {DRY_RUN}", flush=True)
    print(f"Discord configured: {'yes' if DISCORD_WEBHOOK_URL else 'no'}", flush=True)
    print(f"Telegram configured: {'yes' if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID else 'no'}", flush=True)

    health = load_health()
    session = make_session()
    historical_state = load_state()

    try:
        men_products = collect_men_products(session)
    except Exception as exc:
        record_failure(health, f"Search failed: {exc}")
        sys.exit(1)

    if not men_products:
        record_failure(health, "0 matching men's products found via U/C, wool, and cashmere searches.")
        sys.exit(1)

    record_success(health)
    print(f"Products to check: {len(men_products)}", flush=True)

    new_sale_items = []
    new_products = []
    active_product_ids = set()
    fetched_products = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(fetch_product_bundle, item): (item, line_label, from_uc)
            for item, line_label, from_uc in men_products
        }
        for future in as_completed(futures):
            item, line_label, from_uc = futures[future]
            product_id = item.get("productId", "")
            try:
                _, detail, stock_map = future.result()
            except Exception as exc:
                print(f"  Error fetching {product_id}: {exc}", flush=True)
                continue
            fetched_products.append((item, line_label, from_uc, detail, stock_map))

    for item, line_label, from_uc, detail, stock_map in fetched_products:
        product_id, items, new_product = apply_product(
            item, line_label, detail, stock_map, historical_state, from_uc=from_uc
        )
        if product_id:
            active_product_ids.add(product_id)
        new_sale_items.extend(items)
        if new_product:
            new_products.append(new_product)

    cleanup_state(historical_state, active_product_ids)

    print(
        f"Summary: {len(men_products)} products, "
        f"{len(new_sale_items)} sale alerts, {len(new_products)} new products",
        flush=True,
    )
    send_new_products_digest(new_products)
    send_new_sale_digest(new_sale_items)
    save_state(historical_state)
    print("Done.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        import traceback

        print(f"Critical error: {exc}", flush=True)
        traceback.print_exc()
        notify_error(f"Script crashed: {exc}")
        sys.exit(1)
