import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests

from monitor import (
    DRY_RUN,
    DISCORD_CONTENT_LIMIT,
    FAILURE_ALERT_THRESHOLD,
    MAX_RETRIES,
    RETRY_BACKOFF_SEC,
    chunk_lines,
    format_linked_line,
    format_price,
    load_json_file,
    save_json_file,
    send_notification,
)

WATCHLIST_FILE = os.getenv("ARCTERYX_WATCHLIST_FILE", "arcteryx_watchlist.json")
STATE_FILE = os.getenv("ARCTERYX_STATE_FILE", "arcteryx_state.json")
HEALTH_FILE = os.getenv("ARCTERYX_HEALTH_FILE", "arcteryx_health.json")
MERINO_STATE_FILE = os.getenv("ARCTERYX_MERINO_STATE_FILE", "arcteryx_merino_state.json")
OUTLET_SITEMAP_URL = os.getenv(
    "ARCTERYX_OUTLET_SITEMAP_URL",
    "https://outlet.arcteryx.com/sitemap_1.xml",
)
OUTLET_PRODUCT_PATH = "/us/en/shop/mens/"
MAX_WORKERS = int(os.getenv("ARCTERYX_MAX_WORKERS", "6"))
DEFAULT_MERINO_SIZES = ("S", "M")

NEXT_DATA_PATTERN = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
    re.DOTALL,
)
BUILD_ID_PATTERN = re.compile(r'"buildId"\s*:\s*"([^"]+)"')
IN_STOCK_STATUSES = frozenset({"InStock", "LowStock", "LimitedStock"})
DEFAULT_ALERT_ON = ("price_drop", "low_stock")
SITEMAP_LOC_RE = re.compile(r"<loc>([^<]+)</loc>")
# Catalog PDP HTML on arcteryx.com is Kasada-protected (429). Category pages and
# `/_next/data/{buildId}/...json` stay reachable, so prefer the Next data API.
_build_id_cache = {}


class ProductUnavailableError(RuntimeError):
    """Product page exists as a soft 404 / removed listing, not a transport failure."""


class StaleBuildIdError(RuntimeError):
    """Next.js data route returned an empty 404, usually a rotated buildId."""


def parse_watchlist():
    raw = os.getenv("ARCTERYX_WATCHLIST", "").strip()
    if raw:
        entries = []
        for part in raw.split(";"):
            part = part.strip()
            if not part:
                continue
            url, color, size = [segment.strip() for segment in part.split("|", 2)]
            entries.append({"url": url, "color": color, "size": size})
        return entries
    return load_json_file(WATCHLIST_FILE, [])


def normalize_product_url(url):
    parsed = urlparse(url.strip())
    path = parsed.path.rstrip("/")
    if path.endswith("-"):
        path = path[:-1]
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def make_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return session


def request_with_retry(session, url, *, accept_statuses=None):
    accept_statuses = accept_statuses or set()
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            response = session.get(url, timeout=20)
            if response.status_code in accept_statuses:
                return response
            if response.status_code in {429, 500, 502, 503, 504}:
                import time

                time.sleep(RETRY_BACKOFF_SEC * (2 ** attempt))
                continue
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_error = exc
            import time

            time.sleep(RETRY_BACKOFF_SEC * (2 ** attempt))
    if last_error:
        raise last_error
    raise RuntimeError(f"Failed to fetch {url}")


def market_prefix(url):
    parts = [part for part in urlparse(url).path.split("/") if part]
    if len(parts) >= 2:
        return f"/{parts[0]}/{parts[1]}"
    return "/us/en"


def extract_build_id(html):
    match = BUILD_ID_PATTERN.search(html or "")
    return match.group(1) if match else None


def build_id_probe_url(url):
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{market_prefix(url)}/shop"


def next_data_url(url, build_id):
    parsed = urlparse(normalize_product_url(url))
    return f"{parsed.scheme}://{parsed.netloc}/_next/data/{build_id}{parsed.path}.json"


def invalidate_build_id(url):
    _build_id_cache.pop(urlparse(url).netloc, None)


def resolve_build_id(session, url, *, force_refresh=False):
    netloc = urlparse(url).netloc
    if not force_refresh and netloc in _build_id_cache:
        return _build_id_cache[netloc]

    probe_url = build_id_probe_url(url)
    response = request_with_retry(session, probe_url, accept_statuses={404})
    build_id = extract_build_id(response.text)
    if not build_id:
        raise RuntimeError(f"Could not resolve Next.js buildId from {probe_url}")
    _build_id_cache[netloc] = build_id
    return build_id


def product_from_page_props(page_props, url):
    product_raw = (page_props or {}).get("product")
    if not product_raw:
        raise ProductUnavailableError(f"Product unavailable at {url}")
    if isinstance(product_raw, str):
        return json.loads(product_raw)
    return product_raw


def parse_next_data_payload(response, url, build_id):
    if response.status_code == 404:
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if not payload or "pageProps" not in payload:
            raise StaleBuildIdError(f"Stale Next.js buildId {build_id} for {url}")
        return product_from_page_props(payload.get("pageProps"), url)

    payload = response.json()
    return product_from_page_props(payload.get("pageProps"), url)


def fetch_product_from_next_data(session, url, *, force_refresh_build_id=False):
    build_id = resolve_build_id(session, url, force_refresh=force_refresh_build_id)
    data_url = next_data_url(url, build_id)
    response = request_with_retry(session, data_url, accept_statuses={404})
    return parse_next_data_payload(response, url, build_id)


def fetch_product_from_html(session, url):
    response = request_with_retry(session, url, accept_statuses={404})
    match = NEXT_DATA_PATTERN.search(response.text)
    if not match:
        if response.status_code == 404:
            raise ProductUnavailableError(f"Product unavailable at {url}")
        raise RuntimeError(f"No product data found at {url}")

    payload = json.loads(match.group(1))
    build_id = payload.get("buildId") or extract_build_id(response.text)
    if build_id:
        _build_id_cache[urlparse(url).netloc] = build_id
    return product_from_page_props(payload.get("props", {}).get("pageProps"), url)


def fetch_product(session, url):
    try:
        return fetch_product_from_next_data(session, url)
    except ProductUnavailableError:
        raise
    except StaleBuildIdError:
        invalidate_build_id(url)
        try:
            return fetch_product_from_next_data(session, url, force_refresh_build_id=True)
        except ProductUnavailableError:
            raise
        except Exception:
            return fetch_product_from_html(session, url)
    except Exception as first_error:
        try:
            invalidate_build_id(url)
            return fetch_product_from_next_data(session, url, force_refresh_build_id=True)
        except ProductUnavailableError:
            raise
        except Exception:
            pass
        try:
            return fetch_product_from_html(session, url)
        except ProductUnavailableError:
            raise
        except Exception:
            raise first_error from None


def color_map(product):
    return {
        str(color["id"]): color.get("label", "")
        for color in product.get("colourOptions", [])
    }


def size_map(product):
    options = (product.get("sizeOptions") or {}).get("options") or []
    return {str(option["value"]): option.get("label", "") for option in options}


def normalize_match_text(value):
    return re.sub(r"[^A-Z0-9]+", "", str(value).upper())


def find_color_id(color_map_data, color_query):
    query = normalize_match_text(color_query)
    for color_id, label in color_map_data.items():
        if query in normalize_match_text(label):
            return color_id
    return None


def normalize_size_label(value):
    return re.sub(r"[^A-Z0-9]+", "", str(value).upper())


def find_size_id(size_map_data, size_query):
    query = normalize_size_label(size_query)
    for size_id, label in size_map_data.items():
        if normalize_size_label(label) == query:
            return size_id
    return None


def variant_current_price(variant):
    discount = variant.get("discountPrice")
    if discount is not None and float(discount) > 0:
        return float(discount)
    return float(variant.get("price", 0) or 0)


def variant_list_price(variant):
    return float(variant.get("price", 0) or 0)


def variant_in_stock(stock_status):
    return str(stock_status) in IN_STOCK_STATUSES


def entry_watches_any_color(entry):
    if entry.get("any_color"):
        return True
    return str(entry.get("color", "")).strip() == "*"


def parse_alert_on(entry):
    raw = entry.get("alert_on")
    if raw is None:
        return DEFAULT_ALERT_ON
    if isinstance(raw, str):
        return tuple(part.strip() for part in raw.split(",") if part.strip())
    return tuple(raw)


def watch_key(entry, color_label=None):
    url = normalize_product_url(entry["url"])
    color = (color_label or entry["color"]).strip().upper()
    return f"{url}|{color}|{entry['size'].strip().upper()}"


def variant_snapshot(product, variant, colors, sizes):
    color_id = str(variant.get("colourId"))
    size_id = str(variant.get("sizeId"))
    price = variant_current_price(variant)
    list_price = variant_list_price(variant)
    return {
        "name": product.get("name") or product.get("analyticsName") or "Arc'teryx item",
        "color_label": colors[color_id],
        "size_label": sizes[size_id],
        "price": price,
        "list_price": list_price,
        "stock_status": str(variant.get("stockStatus", "Unknown")),
        "in_stock": variant_in_stock(variant.get("stockStatus")),
        "on_outlet": list_price > price and price > 0,
        "variant_id": str(variant.get("id", "")),
    }


def lookup_variants(product, color_query, size_query, any_color=False):
    colors = color_map(product)
    sizes = size_map(product)
    size_id = find_size_id(sizes, size_query)
    if not size_id:
        raise RuntimeError(f"Size not found: {size_query} (available: {', '.join(sizes.values())})")

    color_id = None
    if not any_color:
        color_id = find_color_id(colors, color_query)
        if not color_id:
            raise RuntimeError(f"Color not found: {color_query} (available: {', '.join(colors.values())})")

    snapshots = []
    for variant in product.get("variants", []):
        if str(variant.get("sizeId")) != size_id:
            continue
        if color_id and str(variant.get("colourId")) != color_id:
            continue
        snapshots.append(variant_snapshot(product, variant, colors, sizes))

    if not snapshots:
        target = f"{size_query}" if any_color else f"{color_query} / {size_query}"
        raise RuntimeError(f"No variant for {target}")

    return snapshots


def lookup_variant(product, color_query, size_query):
    return lookup_variants(product, color_query, size_query, any_color=False)[0]


def should_alert_price(previous, current_price):
    if previous is None:
        return False
    previous_price = float(previous.get("price", 0) or 0)
    return previous_price > 0 and current_price < previous_price


def should_alert_low_stock(previous, snapshot):
    if not previous:
        return False
    if previous.get("in_stock") and not snapshot["in_stock"]:
        return True
    previous_status = str(previous.get("stock_status", ""))
    current_status = snapshot["stock_status"]
    if previous_status in IN_STOCK_STATUSES and current_status not in IN_STOCK_STATUSES:
        return True
    return False


def should_alert_restock(previous, snapshot):
    if not previous:
        return False
    if previous.get("in_stock"):
        return False
    return snapshot["in_stock"]


def should_alert_outlet_price(previous, snapshot):
    if not previous:
        return False
    if previous.get("on_outlet"):
        return False
    return snapshot.get("on_outlet", False)


def format_alert_line(entry_url, snapshot, reason):
    size = f"{snapshot['size_label']}/{snapshot['color_label']}"
    price_text = format_price(snapshot["price"])
    label = f"{snapshot['name']} · {size} · ${price_text}"

    list_price = snapshot.get("list_price", 0)
    if list_price > snapshot["price"]:
        discount_pct = round((1 - (snapshot["price"] / list_price)) * 100)
        base_text = format_price(list_price)
        label += f" (was ${base_text}, -{discount_pct}%)"

    if reason == "price_drop":
        label += " · price drop"
    elif reason == "low_stock":
        label += f" · {snapshot['stock_status']}"
    elif reason == "restock":
        label += " · back in stock"
    elif reason == "outlet_price":
        label += " · outlet price"
    elif reason == "merino_outlet":
        label += " · merino outlet"

    return format_linked_line(label, normalize_product_url(entry_url))


def merino_discovery_enabled():
    raw = os.getenv("ARCTERYX_MERINO_DISCOVERY", "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def parse_merino_sizes():
    raw = os.getenv("ARCTERYX_MERINO_SIZES", ",".join(DEFAULT_MERINO_SIZES)).strip()
    sizes = tuple(part.strip() for part in raw.split(",") if part.strip())
    return sizes or DEFAULT_MERINO_SIZES


def merino_target_size_labels(target_sizes):
    return {normalize_size_label(size) for size in target_sizes}


def product_text_blob(product):
    texts = [
        product.get("name", ""),
        product.get("description", ""),
        product.get("shortDescription", ""),
    ]
    texts.extend(product.get("materials") or [])
    for feature in product.get("features") or []:
        if isinstance(feature, dict):
            texts.extend(feature.get("value") or [])
    return " ".join(str(text) for text in texts if text)


def product_is_merino(product):
    return "merino" in product_text_blob(product).lower()


def fetch_outlet_mens_product_urls(session):
    response = request_with_retry(session, OUTLET_SITEMAP_URL)
    urls = {
        loc.rstrip("/")
        for loc in SITEMAP_LOC_RE.findall(response.text)
        if OUTLET_PRODUCT_PATH in loc
    }
    return sorted(urls)


def merino_discovery_key(url, snapshot):
    return (
        f"{normalize_product_url(url)}|"
        f"{snapshot['size_label'].strip().upper()}|"
        f"{snapshot['color_label'].strip().upper()}"
    )


def merino_in_stock_snapshots(product, target_size_labels):
    colors = color_map(product)
    sizes = size_map(product)
    snapshots = []
    for variant in product.get("variants", []):
        size_label = sizes.get(str(variant.get("sizeId")), "")
        if normalize_size_label(size_label) not in target_size_labels:
            continue
        if not variant_in_stock(variant.get("stockStatus")):
            continue
        snapshots.append(variant_snapshot(product, variant, colors, sizes))
    return snapshots


def scan_outlet_product_for_merino(session, url, target_size_labels):
    product = fetch_product(session, url)
    if not product_is_merino(product):
        return []
    return [
        (url, snapshot)
        for snapshot in merino_in_stock_snapshots(product, target_size_labels)
    ]


def collect_merino_outlet_matches(session, product_urls, target_size_labels):
    matches = []
    failures = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(scan_outlet_product_for_merino, session, url, target_size_labels): url
            for url in product_urls
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                matches.extend(future.result())
            except Exception as exc:
                failures += 1
                print(f"  Merino scan failed for {url}: {exc}", flush=True)
    return matches, failures


def run_merino_discovery(session, alerts):
    target_sizes = parse_merino_sizes()
    target_size_labels = merino_target_size_labels(target_sizes)
    merino_state = load_json_file(MERINO_STATE_FILE, {})
    baseline_run = not merino_state

    print(
        f"Scanning Arc'teryx outlet for merino in sizes {', '.join(target_sizes)}",
        flush=True,
    )
    if baseline_run:
        print("  First merino scan: recording baseline without alerts.", flush=True)

    product_urls = fetch_outlet_mens_product_urls(session)
    print(f"  Outlet men's products in sitemap: {len(product_urls)}", flush=True)

    matches, failures = collect_merino_outlet_matches(session, product_urls, target_size_labels)
    print(f"  Merino matches in stock: {len(matches)}", flush=True)
    if failures:
        print(f"  Merino scan failures: {failures}", flush=True)

    for url, snapshot in sorted(matches, key=lambda item: (item[1]["name"], item[0], item[1]["size_label"])):
        key = merino_discovery_key(url, snapshot)
        previous = merino_state.get(key)
        stock_text = "in stock" if snapshot["in_stock"] else snapshot["stock_status"]
        print(
            f"  {snapshot['name']} {snapshot['color_label']} {snapshot['size_label']}: "
            f"${format_price(snapshot['price'])} · {stock_text}",
            flush=True,
        )
        if not baseline_run and previous is None:
            alerts.append(format_alert_line(url, snapshot, "merino_outlet"))
            print("  ALERT: new merino outlet item", flush=True)

        merino_state[key] = {
            "price": snapshot["price"],
            "list_price": snapshot["list_price"],
            "stock_status": snapshot["stock_status"],
            "in_stock": snapshot["in_stock"],
            "variant_id": snapshot["variant_id"],
            "name": snapshot["name"],
            "color_label": snapshot["color_label"],
            "size_label": snapshot["size_label"],
            "url": normalize_product_url(url),
        }

    save_json_file(MERINO_STATE_FILE, merino_state)
    return failures


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
    print(f"Arc'teryx monitor failure ({health['consecutive_failures']}): {message}", flush=True)
    if health["consecutive_failures"] >= FAILURE_ALERT_THRESHOLD:
        send_notification(f"Arc'teryx monitor error: {message}")


def main():
    watchlist = parse_watchlist()
    merino_enabled = merino_discovery_enabled()
    if not watchlist and not merino_enabled:
        print("No Arc'teryx watchlist entries or merino discovery configured.", flush=True)
        sys.exit(1)

    if watchlist:
        print(f"Watching {len(watchlist)} Arc'teryx item(s)", flush=True)
    if merino_enabled:
        print("Arc'teryx merino outlet discovery: enabled", flush=True)
    print(f"Dry run: {DRY_RUN}", flush=True)

    health = load_health()
    state = load_json_file(STATE_FILE, {})
    session = make_session()
    _build_id_cache.clear()
    alerts = []
    merino_failures = 0

    for entry in watchlist:
        url = normalize_product_url(entry["url"])
        color = entry["color"]
        size = entry["size"]
        any_color = entry_watches_any_color(entry)
        alert_on = parse_alert_on(entry)
        color_label = "any color" if any_color else color
        print(f"Checking {url} ({color_label}, {size})", flush=True)

        try:
            product = fetch_product(session, url)
            snapshots = lookup_variants(product, color, size, any_color=any_color)
        except ProductUnavailableError as exc:
            print(f"  Skipping removed product: {exc}", flush=True)
            continue
        except Exception as exc:
            record_failure(health, f"{url}: {exc}")
            continue

        for snapshot in snapshots:
            key = watch_key(entry, snapshot["color_label"])
            previous = state.get(key)
            stock_text = "in stock" if snapshot["in_stock"] else snapshot["stock_status"]
            outlet_text = " · outlet" if snapshot.get("on_outlet") else ""
            print(
                f"  {snapshot['name']} {snapshot['color_label']} {snapshot['size_label']}: "
                f"${format_price(snapshot['price'])} · {stock_text}{outlet_text}",
                flush=True,
            )

            if "price_drop" in alert_on and should_alert_price(previous, snapshot["price"]):
                alerts.append(format_alert_line(url, snapshot, "price_drop"))
                print("  ALERT: price drop", flush=True)

            if "low_stock" in alert_on and should_alert_low_stock(previous, snapshot):
                alerts.append(format_alert_line(url, snapshot, "low_stock"))
                print("  ALERT: low stock / out of stock", flush=True)

            if "restock" in alert_on and should_alert_restock(previous, snapshot):
                alerts.append(format_alert_line(url, snapshot, "restock"))
                print("  ALERT: back in stock", flush=True)

            if "outlet_price" in alert_on and should_alert_outlet_price(previous, snapshot):
                alerts.append(format_alert_line(url, snapshot, "outlet_price"))
                print("  ALERT: outlet price", flush=True)

            state[key] = {
                "price": snapshot["price"],
                "list_price": snapshot["list_price"],
                "stock_status": snapshot["stock_status"],
                "in_stock": snapshot["in_stock"],
                "on_outlet": snapshot.get("on_outlet", False),
                "variant_id": snapshot["variant_id"],
                "name": snapshot["name"],
                "color_label": snapshot["color_label"],
                "size_label": snapshot["size_label"],
                "url": url,
            }

    if merino_enabled:
        try:
            merino_failures = run_merino_discovery(session, alerts)
        except Exception as exc:
            record_failure(health, f"merino discovery: {exc}")
            merino_failures = FAILURE_ALERT_THRESHOLD

    if merino_failures >= FAILURE_ALERT_THRESHOLD:
        record_failure(health, f"merino discovery had {merino_failures} product failures")
    else:
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
        send_notification(f"Arc'teryx monitor crashed: {exc}")
        sys.exit(1)
