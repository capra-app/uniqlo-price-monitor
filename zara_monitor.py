import os
import re
import sys
import time
from urllib.parse import parse_qs, urlparse

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

WATCHLIST_FILE = os.getenv("ZARA_WATCHLIST_FILE", "zara_watchlist.json")
STATE_FILE = os.getenv("ZARA_STATE_FILE", "zara_state.json")
HEALTH_FILE = os.getenv("ZARA_HEALTH_FILE", "zara_health.json")
DEFAULT_STORE_ID = os.getenv("ZARA_STORE_ID", "11735")

PRODUCT_PATH_RE = re.compile(r"-p(\d+)\.html", re.I)
IN_STOCK_STATUSES = frozenset({"in_stock", "low_stock"})
DEFAULT_PRICE_EXPONENT = -2


def parse_watchlist():
    raw = os.getenv("ZARA_WATCHLIST", "").strip()
    if raw:
        return [{"url": part.strip()} for part in raw.split(";") if part.strip()]
    return load_json_file(WATCHLIST_FILE, [])


def parse_variant_id(url):
    values = parse_qs(urlparse(url).query).get("v1") or []
    return str(values[0]).strip() if values and values[0] else ""


def normalize_product_url(url):
    parsed = urlparse(url.strip())
    path = parsed.path
    variant_id = parse_variant_id(url)
    query = f"?v1={variant_id}" if variant_id else ""
    return f"{parsed.scheme}://{parsed.netloc}{path}{query}"


def extract_seo_product_id(url):
    match = PRODUCT_PATH_RE.search(urlparse(url).path)
    return match.group(1) if match else ""


def browser_fetch_enabled():
    return os.getenv("ZARA_BROWSER_FETCH", "false").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def browser_headless_enabled():
    return os.getenv("ZARA_BROWSER_HEADLESS", "false").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def store_id_for_url(url):
    configured = os.getenv("ZARA_STORE_ID", "").strip()
    if configured:
        return configured
    host_path = f"{urlparse(url).netloc}{urlparse(url).path}".lower()
    if "/co/" in host_path or host_path.startswith("www.zara.com/co"):
        return DEFAULT_STORE_ID
    return DEFAULT_STORE_ID


class BrowserFetcher:
    def __init__(self):
        self._playwright = None
        self._browser = None
        self._page = None

    def __enter__(self):
        from patchright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        channel = os.getenv("ZARA_BROWSER_CHANNEL", "").strip() or None
        launch_kwargs = {
            "headless": browser_headless_enabled(),
            "args": ["--no-sandbox", "--disable-dev-shm-usage"],
        }
        if channel:
            launch_kwargs["channel"] = channel
        self._browser = self._playwright.chromium.launch(**launch_kwargs)
        self._page = self._browser.new_page()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def close(self):
        if self._page is not None:
            self._page.close()
            self._page = None
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None

    def fetch_product(self, url):
        timeout_ms = int(os.getenv("ZARA_BROWSER_TIMEOUT_MS", "90000"))
        selector_timeout_ms = int(
            os.getenv("ZARA_BROWSER_SELECTOR_TIMEOUT_MS", "45000")
        )
        attempts = int(os.getenv("ZARA_BROWSER_FETCH_ATTEMPTS", "3"))
        last_error = None
        for attempt in range(1, attempts + 1):
            try:
                return self._fetch_product_once(url, timeout_ms, selector_timeout_ms)
            except Exception as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                print(
                    f"  Fetch attempt {attempt}/{attempts} failed for {url}: {exc}",
                    flush=True,
                )
                try:
                    self._page.close()
                except Exception:
                    pass
                self._page = self._browser.new_page()
        raise last_error

    def _dismiss_consent(self):
        selectors = (
            "#onetrust-accept-btn-handler",
            "button#onetrust-accept-btn-handler",
            "button:has-text('Aceptar todas')",
            "button:has-text('Aceptar')",
            "button:has-text('Accept All')",
            "button:has-text('Accept')",
        )
        for selector in selectors:
            try:
                locator = self._page.locator(selector)
                if locator.count() < 1:
                    continue
                button = locator.first
                if button.is_visible(timeout=1000):
                    button.click(timeout=2000)
                    self._page.wait_for_timeout(500)
                    return
            except Exception:
                continue

    def _read_product_payload(self):
        return self._page.evaluate(
            """() => {
                try {
                    const zara = window.zara;
                    if (!zara || !zara.viewPayload) return null;
                    return zara.viewPayload.product || null;
                } catch (err) {
                    return null;
                }
            }"""
        )

    def _fetch_product_once(self, url, timeout_ms, selector_timeout_ms):
        self._page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        self._dismiss_consent()
        deadline = time.monotonic() + (selector_timeout_ms / 1000.0)
        poll_ms = int(os.getenv("ZARA_BROWSER_POLL_MS", "1000"))
        while time.monotonic() < deadline:
            product = self._read_product_payload()
            if product:
                return product
            self._dismiss_consent()
            self._page.wait_for_timeout(poll_ms)
        product = self._read_product_payload()
        if product:
            return product
        raise RuntimeError(f"Zara product payload missing: {url}")


def make_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "es-CO,es;q=0.9,en;q=0.8",
        "Referer": "https://www.zara.com/co/es/",
        "x-client-id": "web-mob-za",
    })
    return session


def request_json(session, url):
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            response = session.get(url, timeout=25)
            if response.status_code in {403, 429, 500, 502, 503, 504}:
                time.sleep(RETRY_BACKOFF_SEC * (2 ** attempt))
                continue
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            last_error = exc
            time.sleep(RETRY_BACKOFF_SEC * (2 ** attempt))
    if last_error:
        raise last_error
    raise RuntimeError(f"Failed to fetch {url}")


def storefront_product_url(store_id, product_id):
    return (
        f"https://www.zara.com/api/storefront/1/stores/{store_id}"
        f"/products/id/{product_id}"
    )


def storefront_availability_url(store_id, product_id):
    return f"{storefront_product_url(store_id, product_id)}/availability"


def pricing_current(pricing):
    price = (pricing or {}).get("price") or {}
    current = price.get("current") or {}
    if current.get("value") is not None:
        return current
    if price.get("value") is not None:
        return price
    return {}


def pricing_previous(pricing):
    price = (pricing or {}).get("price") or {}
    for key in ("previous", "oldPrice", "original"):
        previous = price.get(key) or {}
        if isinstance(previous, dict) and previous.get("value") is not None:
            return previous
    previous = (pricing or {}).get("oldPrice") or (pricing or {}).get("previousPrice")
    if isinstance(previous, dict) and previous.get("value") is not None:
        return previous
    return {}


def availability_by_sku(payload):
    mapping = {}
    for size in (payload or {}).get("sizes") or []:
        sku = size.get("sku")
        if sku is None:
            continue
        mapping[str(sku)] = size.get("availability")
    return mapping


def storefront_color_from_component(scc, stock_by_sku=None):
    identifier = scc.get("identifier") or {}
    color = scc.get("color") or {}
    pricing = scc.get("pricing") or {}
    current = pricing_current(pricing)
    previous = pricing_previous(pricing)
    price_value = current.get("value")
    currency = current.get("currency") or {}
    sizes = []
    for size in scc.get("sizes") or []:
        size_id = size.get("identifier") or {}
        sku = size_id.get("sku")
        availability = size.get("availability")
        if stock_by_sku and sku is not None and str(sku) in stock_by_sku:
            availability = stock_by_sku[str(sku)]
        size_price = pricing_current(size.get("pricing") or {}).get("value")
        sizes.append({
            "name": ((size.get("nomenclature") or {}).get("name") or "").strip(),
            "sku": sku,
            "availability": availability,
            "price": size_price if size_price is not None else price_value,
        })
    return {
        "name": (color.get("name") or "").strip(),
        "productId": identifier.get("id") or color.get("commercialComponentId"),
        "price": price_value,
        "oldPrice": previous.get("value"),
        "pricing": {
            "price": {
                "value": price_value,
                "currency": currency,
            },
            "oldPrice": {"value": previous.get("value")} if previous else {},
        },
        "extraInfo": {
            "highlightPrice": bool(
                previous.get("value") is not None
                and price_value is not None
                and float(previous["value"]) > float(price_value)
            )
        },
        "sizes": sizes,
    }


def fetch_storefront_color(session, store_id, product_id):
    product_payload = request_json(
        session, storefront_product_url(store_id, product_id)
    )
    scc = product_payload.get("simplifiedCommercialComponent")
    if not scc:
        raise RuntimeError(f"Zara storefront product missing component: {product_id}")
    try:
        availability = request_json(
            session, storefront_availability_url(store_id, product_id)
        )
        stock = availability_by_sku(availability)
    except Exception:
        stock = None
    return scc, storefront_color_from_component(scc, stock)


def fetch_product_via_api(url, all_colors=False):
    variant_id = parse_variant_id(url)
    if not variant_id:
        raise RuntimeError(f"Zara URL missing v1 variant id: {url}")
    store_id = store_id_for_url(url)
    session = make_session()
    scc, primary_color = fetch_storefront_color(session, store_id, variant_id)
    colors = [primary_color]
    if all_colors:
        seen = {str(primary_color.get("productId") or variant_id)}
        for other in ((scc.get("color") or {}).get("otherColors") or []):
            other_id = str(other.get("commercialComponentId") or "").strip()
            if not other_id or other_id in seen:
                continue
            seen.add(other_id)
            _, color = fetch_storefront_color(session, store_id, other_id)
            colors.append(color)
    parent_id = (scc.get("identifier") or {}).get("productParentId") or variant_id
    return {
        "id": parent_id,
        "name": (scc.get("name") or "Zara item").strip(),
        "detail": {"colors": colors},
    }


def zara_amount(raw, exponent=DEFAULT_PRICE_EXPONENT):
    if raw is None:
        return None
    return float(raw) * (10 ** int(exponent))


def price_exponent(color):
    pricing = (color or {}).get("pricing") or {}
    price = pricing.get("price") or {}
    currency = price.get("currency") or {}
    if currency.get("exponent") is not None:
        return int(currency["exponent"])
    return DEFAULT_PRICE_EXPONENT


def color_old_price(color):
    if color.get("oldPrice") is not None:
        return color["oldPrice"]
    pricing = color.get("pricing") or {}
    old = pricing.get("oldPrice") or pricing.get("previousPrice") or {}
    if isinstance(old, dict) and old.get("value") is not None:
        return old["value"]
    return None


def product_colors(product):
    colors = ((product or {}).get("detail") or {}).get("colors") or []
    if not colors:
        raise RuntimeError("Zara product has no colors")
    return colors


def watch_all_colors(entry):
    value = entry.get("colors")
    if isinstance(value, str):
        return value.strip().lower() in {"*", "all"}
    return value is True


def select_colors(product, variant_id, all_colors=False):
    colors = product_colors(product)
    if all_colors:
        return colors
    if variant_id:
        for color in colors:
            if str(color.get("productId") or "") == str(variant_id):
                return [color]
        raise RuntimeError(f"Zara color {variant_id} not found")
    return [colors[0]]


def color_product_url(url, variant_id):
    parsed = urlparse(url.strip())
    query = f"?v1={variant_id}" if variant_id else ""
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}{query}"


def size_in_stock(size):
    return str(size.get("availability") or "").strip().lower() in IN_STOCK_STATUSES


def watched_sizes(color, wanted):
    sizes = color.get("sizes") or []
    if not wanted:
        return sizes
    wanted_set = {str(size).strip().upper() for size in wanted}
    return [
        size for size in sizes
        if str(size.get("name") or "").strip().upper() in wanted_set
    ]


def in_stock_labels(sizes):
    return [str(size.get("name") or "").strip() for size in sizes if size_in_stock(size)]


def is_on_sale(color):
    extra = color.get("extraInfo") or {}
    if extra.get("highlightPrice"):
        return True
    old_raw = color_old_price(color)
    if old_raw is None:
        return False
    current = color.get("price")
    if current is None:
        return False
    return float(old_raw) > float(current)


def color_snapshot(product, color, url, wanted_sizes=None):
    variant_id = str(color.get("productId") or parse_variant_id(url) or "")
    exponent = price_exponent(color)
    price = zara_amount(color.get("price"), exponent)
    if price is None:
        raise RuntimeError(f"No Zara price found for {url}")
    old_raw = color_old_price(color)
    list_price = zara_amount(old_raw, exponent) if old_raw is not None else None
    sizes = watched_sizes(color, wanted_sizes)
    fit_sizes = in_stock_labels(sizes)
    wanted = [str(size).strip() for size in (wanted_sizes or []) if str(size).strip()]
    return {
        "product_id": str(product.get("id") or extract_seo_product_id(url)),
        "variant_id": variant_id,
        "name": (product.get("name") or "Zara item").strip(),
        "color": (color.get("name") or "").strip(),
        "price": price,
        "list_price": list_price,
        "currency": (
            (((color.get("pricing") or {}).get("price") or {}).get("currency") or {}).get("code")
            or "COP"
        ),
        "on_sale": is_on_sale(color),
        "in_stock": bool(fit_sizes),
        "fit_sizes": fit_sizes,
        "wanted_sizes": wanted,
        "url": color_product_url(url, variant_id),
    }


def product_snapshot(product, url, wanted_sizes=None, all_colors=False):
    snapshots = product_snapshots(
        product,
        url,
        wanted_sizes=wanted_sizes,
        all_colors=all_colors,
    )
    return snapshots[0]


def product_snapshots(product, url, wanted_sizes=None, all_colors=False):
    variant_id = parse_variant_id(url)
    return [
        color_snapshot(product, color, url, wanted_sizes=wanted_sizes)
        for color in select_colors(product, variant_id, all_colors=all_colors)
    ]


def watch_key(snapshot):
    return snapshot["url"]


def format_cop(value):
    return f"{int(round(float(value))):,}".replace(",", ".")


def should_alert_sale(previous, snapshot):
    if not snapshot.get("on_sale"):
        return False
    if previous is None:
        return False
    return previous.get("on_sale") is not True


def should_alert_out_of_stock(previous, snapshot):
    if snapshot.get("in_stock"):
        return False
    if previous is None:
        return False
    return previous.get("in_stock") is True


def format_alert_line(snapshot, reason):
    name = snapshot["name"]
    color = snapshot.get("color")
    label = f"{name} · {color}" if color else name
    size_text = "/".join(snapshot.get("wanted_sizes") or snapshot.get("fit_sizes") or [])
    if size_text:
        label += f" · {size_text}"
    if reason == "out_of_stock":
        label += " · out of stock"
    else:
        label += f" · ${format_cop(snapshot['price'])}"
        list_price = snapshot.get("list_price")
        if list_price and list_price > snapshot["price"]:
            discount_pct = round((1 - (snapshot["price"] / list_price)) * 100)
            label += f" (was ${format_cop(list_price)}, -{discount_pct}%)"
        label += " · on sale"
    return format_linked_line(label, snapshot["url"])


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
    print(f"Zara monitor failure ({health['consecutive_failures']}): {message}", flush=True)
    if health["consecutive_failures"] >= FAILURE_ALERT_THRESHOLD:
        send_notification(f"Zara monitor error: {message}")


def fetch_product(url, all_colors=False, browser_fetcher=None):
    try:
        return fetch_product_via_api(url, all_colors=all_colors)
    except Exception as api_exc:
        if browser_fetcher is None:
            raise
        print(f"  API fetch failed, trying browser: {api_exc}", flush=True)
        product = browser_fetcher.fetch_product(url)
        if all_colors:
            # Browser payload already contains every color.
            return product
        return product


def main():
    watchlist = parse_watchlist()
    if not watchlist:
        print("No Zara watchlist entries configured.", flush=True)
        sys.exit(1)

    print(f"Watching {len(watchlist)} Zara item(s)", flush=True)
    print(f"Dry run: {DRY_RUN}", flush=True)
    print(f"Store ID: {os.getenv('ZARA_STORE_ID', DEFAULT_STORE_ID)}", flush=True)
    print(f"Browser fallback: {browser_fetch_enabled()}", flush=True)

    health = load_health()
    state = load_json_file(STATE_FILE, {})
    alerts = []
    checked = 0

    browser_fetcher = None
    try:
        if browser_fetch_enabled():
            browser_fetcher = BrowserFetcher().__enter__()

        for entry in watchlist:
            url = normalize_product_url(entry["url"])
            wanted_sizes = entry.get("sizes") or []
            all_colors = watch_all_colors(entry)
            print(f"Checking {url}", flush=True)
            try:
                product = fetch_product(
                    url,
                    all_colors=all_colors,
                    browser_fetcher=browser_fetcher,
                )
                snapshots = product_snapshots(
                    product,
                    url,
                    wanted_sizes=wanted_sizes,
                    all_colors=all_colors,
                )
            except Exception as exc:
                record_failure(health, f"{url}: {exc}")
                continue

            checked += 1
            for snapshot in snapshots:
                key = watch_key(snapshot)
                previous = state.get(key)
                sale_text = "on sale" if snapshot["on_sale"] else "regular"
                stock_text = (
                    ", ".join(snapshot["fit_sizes"])
                    if snapshot["fit_sizes"]
                    else "out of stock"
                )
                print(
                    f"  {snapshot['name']} · {snapshot['color']}: "
                    f"${format_cop(snapshot['price'])} · {sale_text} · {stock_text}",
                    flush=True,
                )

                if should_alert_sale(previous, snapshot):
                    alerts.append(format_alert_line(snapshot, "on_sale"))
                    print(f"  ALERT: on sale ({snapshot['color']})", flush=True)
                if should_alert_out_of_stock(previous, snapshot):
                    alerts.append(format_alert_line(snapshot, "out_of_stock"))
                    print(f"  ALERT: out of stock ({snapshot['color']})", flush=True)

                state[key] = {
                    "product_id": snapshot["product_id"],
                    "variant_id": snapshot["variant_id"],
                    "name": snapshot["name"],
                    "color": snapshot["color"],
                    "price": snapshot["price"],
                    "list_price": snapshot.get("list_price"),
                    "currency": snapshot["currency"],
                    "on_sale": snapshot["on_sale"],
                    "in_stock": snapshot["in_stock"],
                    "fit_sizes": snapshot["fit_sizes"],
                    "wanted_sizes": snapshot.get("wanted_sizes") or [],
                    "url": snapshot["url"],
                }
    finally:
        if browser_fetcher is not None:
            browser_fetcher.__exit__(None, None, None)

    if checked:
        record_success(health)
    save_json_file(STATE_FILE, state)

    if alerts:
        for content in chunk_lines(alerts):
            send_notification(content)
    else:
        print("No alerts.", flush=True)

    print("Done.", flush=True)
    if not checked:
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        import traceback

        print(f"Critical error: {exc}", flush=True)
        traceback.print_exc()
        send_notification(f"Zara monitor crashed: {exc}")
        sys.exit(1)
