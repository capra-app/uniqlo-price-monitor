import json
import os
import re
import sys
import time
from urllib.parse import urlparse

import requests

from monitor import (
    DRY_RUN,
    FAILURE_ALERT_THRESHOLD,
    MAX_RETRIES,
    RETRY_BACKOFF_SEC,
    chunk_lines,
    format_linked_line,
    load_json_file,
    save_json_file,
    send_notification,
)

WATCHLIST_FILE = os.getenv("IKEA_WATCHLIST_FILE", "ikea_watchlist.json")
STATE_FILE = os.getenv("IKEA_STATE_FILE", "ikea_state.json")
HEALTH_FILE = os.getenv("IKEA_HEALTH_FILE", "ikea_health.json")
SEARCH_API_TEMPLATE = os.getenv(
    "IKEA_SEARCH_API_TEMPLATE",
    "https://sik.search.blue.cdtapps.com/{country}/{lang}/search-result-page",
)

ITEM_NO_RE = re.compile(r"-(\d{8})/?$")
SALE_TAGS = frozenset({
    "NEW_LOWER_PRICE",
    "SPECIAL_PRICE",
    "FAMILY_PRICE",
    "BREATHTAKING",
    "TIME_RESTRICTED",
    "LAST_CHANCE",
})
NON_SALE_TAGS = frozenset({"NONE", "NEW_PRODUCT", ""})


def parse_watchlist():
    raw = os.getenv("IKEA_WATCHLIST", "").strip()
    if raw:
        return [{"url": part.strip()} for part in raw.split(";") if part.strip()]
    return load_json_file(WATCHLIST_FILE, [])


def normalize_product_url(url):
    parsed = urlparse(url.strip())
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}{path}/"


def parse_market(url):
    parts = [part for part in urlparse(url).path.split("/") if part]
    if len(parts) >= 2:
        return parts[0], parts[1]
    return "co", "es"


def extract_item_no(url):
    path = urlparse(url).path.rstrip("/")
    match = ITEM_NO_RE.search(path)
    if not match:
        raise RuntimeError(f"Could not parse IKEA item number from {url}")
    return match.group(1)


def make_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "es-CO,es;q=0.9,en;q=0.8",
    })
    return session


def request_with_retry(session, url, *, params=None):
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            response = session.get(url, params=params, timeout=20)
            if response.status_code in {429, 500, 502, 503, 504}:
                time.sleep(RETRY_BACKOFF_SEC * (2 ** attempt))
                continue
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_error = exc
            time.sleep(RETRY_BACKOFF_SEC * (2 ** attempt))
    if last_error:
        raise last_error
    raise RuntimeError(f"Failed to fetch {url}")


def parse_formatted_price(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    digits = re.sub(r"[^\d]", "", str(value))
    if not digits:
        return None
    return float(digits)


def sales_price_numeral(sales_price, key="current"):
    if not isinstance(sales_price, dict):
        return None
    if key == "current" and sales_price.get("numeral") is not None:
        return float(sales_price["numeral"])
    block = sales_price.get(key) if key != "current" else sales_price.get("current")
    if isinstance(block, dict):
        whole = block.get("wholeNumber")
        parsed = parse_formatted_price(whole)
        if parsed is not None:
            return parsed
    if key == "previous":
        return None
    return None


def is_on_sale(product):
    if product.get("lastChance"):
        return True
    sales_price = product.get("salesPrice") or {}
    if sales_price.get("isBreathTaking"):
        return True
    if sales_price.get("previous"):
        return True
    tag = str(sales_price.get("tag") or product.get("tag") or "").strip().upper()
    if tag in SALE_TAGS:
        return True
    if tag and tag not in NON_SALE_TAGS and sales_price.get("discount"):
        return True
    return False


def product_display_name(product):
    name = (product.get("name") or "").strip()
    type_name = (product.get("typeName") or "").strip()
    measure = (product.get("itemMeasureReferenceText") or "").strip()
    design = (product.get("validDesignText") or "").strip()
    parts = [part for part in (name, type_name) if part]
    label = " ".join(parts) if parts else "IKEA item"
    details = [part for part in (design, measure) if part]
    if details:
        label = f"{label}, {', '.join(details)}"
    return label


def product_snapshot(product, url):
    sales_price = product.get("salesPrice") or {}
    price = sales_price_numeral(sales_price, "current")
    if price is None:
        raise RuntimeError(f"No price found for {url}")
    list_price = sales_price_numeral(sales_price, "previous")
    on_sale = is_on_sale(product)
    if on_sale and list_price is None:
        list_price = price
    tag = str(sales_price.get("tag") or product.get("tag") or "NONE")
    tag_text = (
        sales_price.get("tagText")
        or (sales_price.get("tagWithText") or {}).get("text")
        or ""
    )
    return {
        "item_no": str(product.get("itemNo") or product.get("id") or extract_item_no(url)),
        "name": product_display_name(product),
        "price": price,
        "list_price": list_price,
        "currency": sales_price.get("currencyCode") or "COP",
        "on_sale": on_sale,
        "last_chance": bool(product.get("lastChance")),
        "tag": tag,
        "tag_text": tag_text,
        "pip_url": product.get("pipUrl") or normalize_product_url(url),
    }


def find_product_in_search(payload, item_no):
    products = ((payload or {}).get("searchResultPage") or {}).get("products") or {}
    main = products.get("main") or {}
    for item in main.get("items") or []:
        product = item.get("product") or {}
        product_id = str(product.get("id") or product.get("itemNo") or "")
        if product_id == item_no:
            return product
    raise RuntimeError(f"IKEA product {item_no} not found in search results")


def fetch_product(session, url):
    item_no = extract_item_no(url)
    country, lang = parse_market(url)
    api_url = SEARCH_API_TEMPLATE.format(country=country, lang=lang)
    response = request_with_retry(
        session,
        api_url,
        params={"types": "PRODUCT", "q": item_no, "size": "10"},
    )
    product = find_product_in_search(response.json(), item_no)
    return product_snapshot(product, url)


def watch_key(entry):
    return normalize_product_url(entry["url"])


def format_cop(value):
    return f"{int(round(float(value))):,}".replace(",", ".")


def should_alert_sale(previous, snapshot):
    if not snapshot.get("on_sale"):
        return False
    if previous is None:
        return False
    return previous.get("on_sale") is not True


def should_alert_price_drop(previous, snapshot):
    if previous is None:
        return False
    previous_price = float(previous.get("price", 0) or 0)
    return previous_price > 0 and snapshot["price"] < previous_price


def format_alert_line(snapshot, reason):
    price_text = format_cop(snapshot["price"])
    label = f"{snapshot['name']} · ${price_text}"
    list_price = snapshot.get("list_price")
    if list_price and list_price > snapshot["price"]:
        discount_pct = round((1 - (snapshot["price"] / list_price)) * 100)
        label += f" (was ${format_cop(list_price)}, -{discount_pct}%)"
    if reason == "on_sale":
        tag_text = snapshot.get("tag_text") or snapshot.get("tag") or "on sale"
        label += f" · {tag_text}"
    elif reason == "price_drop":
        label += " · price drop"
    return format_linked_line(label, snapshot["pip_url"])


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
    print(f"IKEA monitor failure ({health['consecutive_failures']}): {message}", flush=True)
    if health["consecutive_failures"] >= FAILURE_ALERT_THRESHOLD:
        send_notification(f"IKEA monitor error: {message}")


def main():
    watchlist = parse_watchlist()
    if not watchlist:
        print("No IKEA watchlist entries configured.", flush=True)
        sys.exit(1)

    print(f"Watching {len(watchlist)} IKEA item(s)", flush=True)
    print(f"Dry run: {DRY_RUN}", flush=True)

    health = load_health()
    state = load_json_file(STATE_FILE, {})
    session = make_session()
    alerts = []
    checked = 0

    for entry in watchlist:
        url = normalize_product_url(entry["url"])
        print(f"Checking {url}", flush=True)
        try:
            snapshot = fetch_product(session, url)
        except Exception as exc:
            record_failure(health, f"{url}: {exc}")
            continue

        checked += 1
        key = watch_key(entry)
        previous = state.get(key)
        sale_text = "on sale" if snapshot["on_sale"] else "regular"
        print(
            f"  {snapshot['name']}: ${format_cop(snapshot['price'])} · {sale_text}"
            f" · {snapshot['tag']}",
            flush=True,
        )

        if should_alert_sale(previous, snapshot):
            alerts.append(format_alert_line(snapshot, "on_sale"))
            print("  ALERT: on sale", flush=True)
        elif should_alert_price_drop(previous, snapshot):
            alerts.append(format_alert_line(snapshot, "price_drop"))
            print("  ALERT: price drop", flush=True)

        state[key] = {
            "item_no": snapshot["item_no"],
            "name": snapshot["name"],
            "price": snapshot["price"],
            "list_price": snapshot.get("list_price"),
            "currency": snapshot["currency"],
            "on_sale": snapshot["on_sale"],
            "last_chance": snapshot["last_chance"],
            "tag": snapshot["tag"],
            "tag_text": snapshot.get("tag_text"),
            "url": url,
        }

    if checked:
        record_success(health)
    save_json_file(STATE_FILE, state)

    if alerts:
        for content in chunk_lines(alerts):
            send_notification(content)
    else:
        print("No alerts.", flush=True)

    print("Done.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        import traceback

        print(f"Critical error: {exc}", flush=True)
        traceback.print_exc()
        send_notification(f"IKEA monitor crashed: {exc}")
        sys.exit(1)
