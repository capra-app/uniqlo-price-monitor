import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import unescape
from urllib.parse import urlparse

import requests

from fit import (
    filter_shoe_labels_by_fit,
    filter_sock_labels_by_fit,
    load_fit_profile,
    preferred_sizes,
    profile_gender,
)
from monitor import (
    DRY_RUN,
    FAILURE_ALERT_THRESHOLD,
    MAX_RETRIES,
    RETRY_BACKOFF_SEC,
    chunk_lines,
    format_linked_line,
    format_price,
    load_json_file,
    max_fiber_percent,
    save_json_file,
    send_notification,
)

STATE_FILE = os.getenv("SIERRA_STATE_FILE", "sierra_state.json")
HEALTH_FILE = os.getenv("SIERRA_HEALTH_FILE", "sierra_health.json")
MAX_WORKERS = int(os.getenv("SIERRA_MAX_WORKERS", "4"))
MIN_MERINO_WOOL_PERCENT = float(os.getenv("SIERRA_MIN_MERINO_WOOL_PERCENT", "50"))
MIN_SHOE_MATERIAL_PERCENT = float(os.getenv("SIERRA_MIN_SHOE_MATERIAL_PERCENT", "50"))
MIN_DOWN_FILL_PERCENT = float(os.getenv("SIERRA_MIN_DOWN_FILL_PERCENT", "70"))
MIN_DOWN_FILL_POWER = float(os.getenv("SIERRA_MIN_DOWN_FILL_POWER", "800"))
SHOE_MATERIALS = ("leather", "suede")
SHOE_MATERIAL_KEYWORDS = SHOE_MATERIALS + ("nubuck",)
DOWN_OUTERWEAR_TOKENS = (
    "jacket", "vest", "parka", "puffer", "coat", "hoodie",
    "anorak", "bomber", "insulated",
)
LEATHER_CATEGORY_URL = (
    "https://www.sierra.com/s~leather-men/men~d~5284/?perPage=96"
)

DEFAULT_CATEGORY_URLS = [
    "https://www.sierra.com/s~merino-wool-men/men~d~5284/?perPage=96",
    "https://www.sierra.com/s~down-men/men~d~5284/?perPage=96",
]

HARD_BLOCK_MARKERS = (
    "Something went wrong",
    "Looking to shop?",
    "/securityfailover/",
)
# Akamai bot-manager interstitial. This is solvable in a real browser if we wait;
# do NOT treat it as a terminal hard-block.
CHALLENGE_PAGE_MARKERS = (
    "sec-if-cpt-container",
    "scf-akamai-protected-by",
    "behavioral-content",
)
# Kept for HTTP-mode callers that cannot execute the challenge JS.
BOT_PAGE_MARKERS = HARD_BLOCK_MARKERS + CHALLENGE_PAGE_MARKERS

PRODUCT_LINK_RE = re.compile(
    r'href="((?:https://www\.sierra\.com)?/[^"]*~p~([a-z0-9]+)[^"]*)"',
    re.I,
)
EMBEDDED_LISTING_RE = re.compile(
    r'\{"id":"([A-Z0-9]+)","name":"([^"]+)","brand":"([^"]+)"'
    r'(?:[^}]*?"rrPrice":([0-9.]+))?[^}]*?"price":([0-9.]+)',
    re.I,
)
PRICE_RE = re.compile(r"\$([0-9]+(?:\.[0-9]{2})?)")
FABRIC_RE = re.compile(r"(?<![\w-])Fabric:\s*([^<\n]+)", re.I)
MATERIALS_RE = re.compile(r"(?<![\w-])Materials?:\s*([^<\n]+)", re.I)
UPPER_RE = re.compile(r"(?<![\w-])Upper:\s*([^<\n]+)", re.I)
INSULATION_RE = re.compile(r"(?<![\w-])Insulation:\s*([^<\n]+)", re.I)
FILL_RE = re.compile(r"(?<![\w-])Fill:\s*([^<\n]+)", re.I)
FILL_POWER_RE = re.compile(r"(?<![\w-])Fill\s+[Pp]ower:\s*(\d{3,4})\b")
NAME_FILL_POWER_RE = re.compile(r"\b(\d{3,4})\s*fill\s*power\b", re.I)
COMPOSITION_LINE_RES = (FABRIC_RE, MATERIALS_RE, UPPER_RE, INSULATION_RE, FILL_RE)
COMPOSITION_READY_RE = re.compile(
    r"(?:Fabric|Materials?|Upper|Insulation|Fill):\s*(?:\d+\s*%|[A-Za-z])",
    re.I,
)
COMPARE_AT_RE = re.compile(r"Compare at\s*\$([0-9]+(?:\.[0-9]{2})?)", re.I)
SIZE_TILE_RE = re.compile(r'class="[^"]*size-tile[^"]*"[^>]*>\s*([^<]+?)\s*<', re.I)
SIZE_TILE_SELECTOR_RE = re.compile(
    r'class="[^"]*size-tile-selector[^"]*"[^>]*>\s*([^<]+?)\s*<',
    re.I,
)
SIZE_VALUE_RE = re.compile(r'data-size-value="([^"]+)"', re.I)
SELECTION_BOX_RE = re.compile(
    r'class="selectionBox"[^>]*title="[^"]*">\s*([^<]+?)\s*<',
    re.I,
)
SKU_VARIANT_RE = re.compile(
    r'"(?:variant|sku)"\s*:\s*"([A-Z0-9]+)-(\d+)-([A-Z0-9.]+)"',
    re.I,
)
PRODUCT_CHILD_STOCK_RE = re.compile(
    r'"id"\s*:\s*"([A-Z0-9]+)"[^}]*"productChildStock"\s*:\s*(\d+)',
    re.I | re.S,
)
# Sierra PDPs embed a TAFFY SKU table with per-size availability. Analytics
# usually only mentions the default variant SKU, so SKU-token parsing alone
# under-reports in-stock sizes.
TAFFY_SKUS_RE = re.compile(
    r"var\s+skus\s*=\s*new\s+TAFFY\s*\(\s*\[(.*?)\]\s*\)",
    re.I | re.S,
)
TAFFY_RECORD_RE = re.compile(r"\{([^{}]+)\}")
SIZE_PROPERTY_LABEL_RE = re.compile(
    r'id="property(\d+)Label"[^>]*title="\s*SIZE\s*"'
    r'|title="\s*SIZE\s*"[^>]*id="property(\d+)Label"',
    re.I,
)
IN_STOCK_AVAILABILITY = frozenset({"instock"})
BRAND_NAME_RE = re.compile(
    r"\[([^\]]+)\]\(https://www\.sierra\.com/[^)]*~p~[a-z0-9]+",
    re.I,
)
TITLE_NAME_RE = re.compile(
    r"\]\(https://www\.sierra\.com/[^)]*~p~([a-z0-9]+)[^)]*\)\s*\n\[(?!Quick View)([^\]]+)\]",
    re.I,
)


def parse_category_urls():
    raw = os.getenv("SIERRA_CATEGORY_URLS", "").strip()
    if not raw:
        urls = list(DEFAULT_CATEGORY_URLS)
    else:
        urls = [part.strip() for part in raw.split(";") if part.strip()]
    if shoe_alerts_enabled() and LEATHER_CATEGORY_URL not in urls:
        urls.append(LEATHER_CATEGORY_URL)
    return urls


def shoe_alerts_enabled():
    return os.getenv("SIERRA_SHOE_ALERTS_ENABLED", "false").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def browser_fetch_enabled():
    return os.getenv("SIERRA_BROWSER_FETCH", "true").strip().lower() in ("1", "true", "yes")


def browser_headless_enabled():
    return os.getenv("SIERRA_BROWSER_HEADLESS", "false").strip().lower() in ("1", "true", "yes")


def has_hard_block_markers(html):
    return any(marker in (html or "") for marker in HARD_BLOCK_MARKERS)


def is_challenge_page(html):
    return any(marker in (html or "") for marker in CHALLENGE_PAGE_MARKERS)


def is_blocked_page(html):
    """True when HTML cannot yield products without further browser challenge work."""
    if not html:
        return True
    if has_hard_block_markers(html):
        return True
    if is_challenge_page(html):
        return True
    if len(html) < 5000:
        return True
    return False


def detail_page_has_composition(html):
    return bool(COMPOSITION_READY_RE.search(html))


def page_is_ready(url, html):
    if not html or has_hard_block_markers(html) or is_challenge_page(html):
        return False
    path = urlparse(url).path
    if "~p~" in path:
        return detail_page_has_composition(html)
    return "~p~" in html


class BrowserFetcher:
    def __init__(self):
        self._playwright = None
        self._browser = None
        self._page = None
        self._launch_kwargs = None

    def __enter__(self):
        from patchright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        channel = os.getenv("SIERRA_BROWSER_CHANNEL", "").strip() or None
        self._launch_kwargs = {
            "headless": browser_headless_enabled(),
            "args": ["--no-sandbox", "--disable-dev-shm-usage"],
        }
        if channel:
            self._launch_kwargs["channel"] = channel
        self._browser = self._playwright.chromium.launch(**self._launch_kwargs)
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

    def _reset_page(self):
        if self._page is not None:
            try:
                self._page.close()
            except Exception:
                pass
            self._page = None
        self._page = self._browser.new_page()

    def _selector_timeout_ms(self, url):
        if "~p~" in urlparse(url).path:
            return int(os.getenv("SIERRA_BROWSER_DETAIL_TIMEOUT_MS", "60000"))
        return int(os.getenv("SIERRA_BROWSER_SELECTOR_TIMEOUT_MS", "180000"))

    def _nudge_challenge(self):
        try:
            button = self._page.locator("#progress-button")
            if button.count() > 0 and button.first.is_visible(timeout=500):
                button.first.click(timeout=2000)
        except Exception:
            pass
        try:
            self._page.mouse.move(140, 180)
            self._page.mouse.wheel(0, 600)
        except Exception:
            pass

    def _warm_up(self):
        try:
            self._page.goto(
                "https://www.sierra.com/",
                wait_until="domcontentloaded",
                timeout=60000,
            )
            self._nudge_challenge()
            self._page.wait_for_timeout(2500)
        except Exception as exc:
            print(f"  Sierra warm-up skipped: {exc}", flush=True)

    def _fetch_once(self, url):
        self._page.goto(url, wait_until="domcontentloaded", timeout=90000)
        timeout_ms = self._selector_timeout_ms(url)
        poll_ms = int(os.getenv("SIERRA_BROWSER_POLL_MS", "2000"))
        # Only treat tiny non-challenge pages as dead after a short grace period.
        short_grace_ms = int(os.getenv("SIERRA_BROWSER_BLOCK_GRACE_MS", "8000"))
        deadline = time.monotonic() + (timeout_ms / 1000.0)
        started = time.monotonic()
        saw_challenge = False

        while True:
            html = self._page.content()
            if page_is_ready(url, html):
                return html
            if has_hard_block_markers(html):
                raise RuntimeError(f"Sierra fetch timed out or blocked: {url}")
            if is_challenge_page(html):
                if not saw_challenge:
                    print(f"  Waiting for Akamai challenge on {url}", flush=True)
                    saw_challenge = True
                self._nudge_challenge()
            elapsed_ms = (time.monotonic() - started) * 1000
            if (
                elapsed_ms >= short_grace_ms
                and len(html) < 5000
                and not is_challenge_page(html)
            ):
                raise RuntimeError(f"Sierra fetch timed out or blocked: {url}")
            if time.monotonic() >= deadline:
                break
            self._page.wait_for_timeout(poll_ms)

        html = self._page.content()
        if page_is_ready(url, html):
            return html
        raise RuntimeError(f"Sierra fetch timed out or blocked: {url}")

    def fetch(self, url):
        attempts = int(os.getenv("SIERRA_BROWSER_FETCH_ATTEMPTS", "3"))
        last_error = None
        for attempt in range(1, attempts + 1):
            try:
                if attempt == 1:
                    self._warm_up()
                return self._fetch_once(url)
            except Exception as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                print(
                    f"  Fetch attempt {attempt}/{attempts} failed for {url}: {exc}",
                    flush=True,
                )
                self._reset_page()
                time.sleep(RETRY_BACKOFF_SEC * (2 ** (attempt - 1)))
        raise last_error


def make_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.sierra.com/",
    })
    return session


def request_with_retry(session, url):
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            response = session.get(url, timeout=25)
            if response.status_code in {403, 429, 500, 502, 503, 504}:
                time.sleep(RETRY_BACKOFF_SEC * (2 ** attempt))
                continue
            response.raise_for_status()
            if is_blocked_page(response.text):
                raise RuntimeError("Sierra bot protection page returned")
            return response
        except requests.RequestException as exc:
            last_error = exc
            time.sleep(RETRY_BACKOFF_SEC * (2 ** attempt))
    if last_error:
        raise last_error
    raise RuntimeError(f"Failed to fetch {url}")


def fetch_html(fetcher, url):
    if isinstance(fetcher, BrowserFetcher):
        return fetcher.fetch(url)
    response = request_with_retry(fetcher, url)
    return response.text


def max_merino_wool_percent(text):
    if not text:
        return 0.0
    return max_fiber_percent(text, "merino wool")


def max_shoe_material_percent(text):
    if not text:
        return 0.0

    pct_sum = sum(max_fiber_percent(text, material) for material in SHOE_MATERIALS)
    for match in re.finditer(
        r"(\d+(?:\.\d+)?)\s*%\s*(?:\w+\s+)*(?:leather|suede|nubuck)\b",
        text,
        re.I,
    ):
        pct_sum = max(pct_sum, float(match.group(1)))

    if pct_sum >= MIN_SHOE_MATERIAL_PERCENT:
        return pct_sum

    for segment in re.split(r"\s*\|\s*", text):
        segment = segment.strip()
        if not segment or re.search(r"[\{#]|onetrust|overflow:", segment, re.I):
            continue
        seg_lower = segment.lower()
        if not any(keyword in seg_lower for keyword in SHOE_MATERIAL_KEYWORDS):
            continue
        if re.search(r"\d+\s*%", segment):
            continue
        if re.fullmatch(r"(?i)(?:leather|suede|nubuck)", segment):
            return 100.0
        if re.search(
            r"(?i)\b(?:leather|suede|nubuck)\b.*\b(?:and|/)\b.*\b(?:synthetic|textile|rubber|polyester|nylon|mesh)\b",
            segment,
        ):
            return 50.0
        if re.search(r"(?i)\b(?:leather|suede|nubuck)\b", segment):
            return 100.0

    return pct_sum


def _is_valid_composition_part(part):
    clean = part.strip()
    if not clean or len(clean) > 200:
        return False
    if re.search(r"[\{#}]|onetrust|overflow:|position:\s*fixed", clean, re.I):
        return False
    return True


def max_down_fill_percent(text):
    if not text:
        return 0.0

    percentages = []
    for match in re.finditer(
        r"(\d+(?:\.\d+)?)\s*%\s*(?:\w+\s+)*down\b",
        text,
        re.I,
    ):
        percentages.append(float(match.group(1)))
    return max(percentages) if percentages else 0.0


def parse_fill_power(html, name=""):
    values = []
    for match in FILL_POWER_RE.finditer(html or ""):
        values.append(int(match.group(1)))
    name_match = NAME_FILL_POWER_RE.search(name or "")
    if name_match:
        values.append(int(name_match.group(1)))
    return max(values) if values else 0


def listing_fill_power(item):
    name_match = NAME_FILL_POWER_RE.search(item.get("name", ""))
    if name_match:
        return int(name_match.group(1))
    return None


def is_down_outerwear_name(name):
    text = name.lower()
    return any(token in text for token in DOWN_OUTERWEAR_TOKENS)


def parse_composition(html):
    parts = []
    seen = set()
    for pattern in COMPOSITION_LINE_RES:
        for part in pattern.findall(html):
            clean = part.strip()
            if clean and clean not in seen and _is_valid_composition_part(clean):
                seen.add(clean)
                parts.append(clean)
    return " | ".join(parts)


def uses_shoe_material_rules(name):
    text = name.lower()
    if "sock" in text:
        return False
    if "merino" in text:
        return False
    return any(
        token in text
        for token in ("shoe", "boot", "sneaker", "trail runner", "clog", "sandal", "loafer")
    )


def is_leather_shoe_listing(item):
    if not uses_shoe_material_rules(item.get("name", "")):
        return False
    text = f"{item.get('name', '')} {item.get('brand', '')} {item.get('url', '')}".lower()
    return any(keyword in text for keyword in SHOE_MATERIAL_KEYWORDS)


def material_label(product):
    if uses_shoe_material_rules(product.get("name", "")):
        return f"{product.get('shoe_material_pct', 0):g}% leather/suede"
    down_fill_pct = product.get("down_fill_pct", 0)
    if down_fill_pct >= MIN_DOWN_FILL_PERCENT:
        fill_power = product.get("fill_power", 0)
        return f"{down_fill_pct:g}% down fill · {fill_power:g} fill power"
    return f"{product.get('merino_pct', 0):g}% merino"


def passes_material_rules(product):
    if uses_shoe_material_rules(product["name"]):
        if not shoe_alerts_enabled():
            return False, "shoe alerts disabled"
        shoe_material_pct = product.get("shoe_material_pct", 0)
        if shoe_material_pct < MIN_SHOE_MATERIAL_PERCENT:
            return False, (
                f"leather/suede {shoe_material_pct:g}% "
                f"< {MIN_SHOE_MATERIAL_PERCENT:g}%"
            )
        return True, "ok"

    down_fill_pct = product.get("down_fill_pct", 0)
    if down_fill_pct >= MIN_DOWN_FILL_PERCENT:
        fill_power = product.get("fill_power", 0)
        if fill_power < MIN_DOWN_FILL_POWER:
            if fill_power <= 0:
                return False, f"fill power missing (need {MIN_DOWN_FILL_POWER:g}+)"
            return False, (
                f"fill power {fill_power:g} < {MIN_DOWN_FILL_POWER:g}"
            )
        return True, "ok"

    if product["merino_pct"] >= MIN_MERINO_WOOL_PERCENT:
        return True, "ok"

    if down_fill_pct > 0:
        return False, (
            f"down fill {down_fill_pct:g}% < {MIN_DOWN_FILL_PERCENT:g}%"
        )
    return False, (
        f"merino {product['merino_pct']:g}% < {MIN_MERINO_WOOL_PERCENT:g}%"
    )


def slug_to_title(slug):
    slug = slug.split("~p~", 1)[0].strip("/")
    return unescape(slug.replace("-", " ").strip().title())


def product_id_from_url(url):
    match = re.search(r"~p~([a-z0-9]+)", url, re.I)
    return match.group(1).lower() if match else ""


def normalize_product_url(url):
    parsed = urlparse(url.strip().split("?")[0])
    path = parsed.path.rstrip("/") + "/"
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def is_mens_product(name, url):
    text = f"{name} {url}".lower()
    if re.search(r"\bfor women\b", text) and not re.search(r"\bfor men\b", text):
        return False
    if "women's" in text and "men's" not in text and "for men and women" not in text:
        return False
    return True


def is_sock_product(name, url=""):
    return "sock" in f"{name} {url}".lower()


def product_size_category(name):
    text = name.lower()
    if is_sock_product(name):
        return "socks"
    if any(token in text for token in ("shoe", "boot", "sneaker", "trail runner", "clog")):
        return "footwear"
    if "short sleeve" in text or "short-sleeve" in text:
        return "tops"
    if any(token in text for token in ("pant", "brief", "bottom", "jean", "trouser")):
        if "short" in text and "sleeve" not in text:
            return "bottoms"
        if "short" not in text:
            return "bottoms"
    return "tops"


def absolutize_product_href(href):
    href = href.strip().split("?")[0]
    if href.startswith("/"):
        return f"https://www.sierra.com{href}"
    return href


def parse_listing_products(html):
    products = {}

    for match in EMBEDDED_LISTING_RE.finditer(html):
        product_id = match.group(1).lower()
        if product_id in products:
            continue
        price = float(match.group(5))
        compare_at = float(match.group(4) or 0)
        products[product_id] = {
            "product_id": product_id,
            "url": "",
            "name": unescape(match.group(2).strip()),
            "brand": unescape(match.group(3).strip()),
            "price": price,
            "compare_at": compare_at if compare_at > price else 0.0,
        }

    for match in PRODUCT_LINK_RE.finditer(html):
        url = normalize_product_url(absolutize_product_href(match.group(1)))
        product_id = match.group(2).lower()
        item = products.get(product_id)
        if item is None:
            products[product_id] = {
                "product_id": product_id,
                "url": url,
                "name": slug_to_title(url),
                "brand": "",
                "price": 0.0,
                "compare_at": 0.0,
            }
            continue
        if not item.get("url"):
            item["url"] = url

    for match in TITLE_NAME_RE.finditer(html):
        product_id = match.group(1).lower()
        if product_id not in products:
            continue
        products[product_id]["name"] = unescape(match.group(2).strip())

    for product_id, item in list(products.items()):
        if item.get("url"):
            continue
        link_match = re.search(
            rf'href="((?:https://www\.sierra\.com)?/[^"]*~p~{re.escape(product_id)}[^"]*)"',
            html,
            re.I,
        )
        if link_match:
            item["url"] = normalize_product_url(
                absolutize_product_href(link_match.group(1))
            )

    for product_id, item in products.items():
        if item.get("price", 0) > 0:
            continue
        pattern = re.compile(
            rf"~p~{re.escape(product_id)}.*?\$([0-9]+(?:\.[0-9]{{2}})?)",
            re.I | re.S,
        )
        price_match = pattern.search(html)
        if price_match:
            item["price"] = float(price_match.group(1))
        compare_match = re.search(
            rf"~p~{re.escape(product_id)}.*?Compare at\s*\$([0-9]+(?:\.[0-9]{{2}})?)",
            html,
            re.I | re.S,
        )
        if compare_match:
            item["compare_at"] = float(compare_match.group(1))

    for match in BRAND_NAME_RE.finditer(html):
        brand = unescape(match.group(1).strip())
        for item in products.values():
            if brand.lower() in item["url"].lower() or brand.lower() in item["name"].lower():
                item["brand"] = brand

    return list(products.values())


def _append_size(sizes, value):
    clean = str(value).strip()
    if clean and clean not in sizes:
        sizes.append(clean)


def parse_sierra_child_stock(html, product_id):
    if not product_id:
        return None
    product_key = str(product_id).upper()
    for match in PRODUCT_CHILD_STOCK_RE.finditer(html):
        if match.group(1).upper() == product_key:
            return int(match.group(2))
    return None


def size_property_index(html):
    match = SIZE_PROPERTY_LABEL_RE.search(html or "")
    if not match:
        return 2
    return int(match.group(1) or match.group(2))


def parse_taffy_in_stock_sizes(html):
    """Return in-stock sizes from Sierra's TAFFY table, or None if absent."""
    match = TAFFY_SKUS_RE.search(html or "")
    if not match:
        return None

    prop_key = f"property{size_property_index(html)}"
    prop_re = re.compile(rf'{re.escape(prop_key)}\s*:\s*"([^"]*)"', re.I)
    css_re = re.compile(r'availabilityCss\s*:\s*"([^"]*)"', re.I)

    sizes = []
    for record in TAFFY_RECORD_RE.finditer(match.group(1)):
        chunk = record.group(1)
        css_match = css_re.search(chunk)
        if not css_match:
            continue
        if css_match.group(1).strip().lower() not in IN_STOCK_AVAILABILITY:
            continue
        size_match = prop_re.search(chunk)
        if not size_match:
            continue
        _append_size(sizes, size_match.group(1))
    return sizes


def parse_available_sizes(html, product_id=None):
    taffy_sizes = parse_taffy_in_stock_sizes(html)
    if taffy_sizes is not None:
        return taffy_sizes

    sizes = []
    product_key = str(product_id or "").upper()

    if product_key:
        sku_pattern = re.compile(
            rf"{re.escape(product_key)}-\d+-([A-Z0-9.]+)",
            re.I,
        )
        for match in sku_pattern.finditer(html):
            _append_size(sizes, match.group(1))

        if not sizes:
            for match in SKU_VARIANT_RE.finditer(html):
                if match.group(1).upper() != product_key:
                    continue
                _append_size(sizes, match.group(3))

        child_stock = parse_sierra_child_stock(html, product_key)
        if child_stock is not None and child_stock <= 0:
            return []
        if sizes:
            return sizes

    for pattern in (
        SIZE_TILE_RE,
        SIZE_TILE_SELECTOR_RE,
        SIZE_VALUE_RE,
        SELECTION_BOX_RE,
    ):
        for value in pattern.findall(html):
            _append_size(sizes, value)

    return sizes


def parse_product_detail(html, listing_item):
    composition = parse_composition(html)
    merino_pct = max_merino_wool_percent(composition)
    shoe_material_pct = max_shoe_material_percent(composition)
    down_fill_pct = max_down_fill_percent(composition)

    price = listing_item.get("price", 0.0)
    price_match = PRICE_RE.search(html)
    if price_match:
        price = float(price_match.group(1))

    compare_at = listing_item.get("compare_at", 0.0)
    compare_match = COMPARE_AT_RE.search(html)
    if compare_match:
        compare_at = float(compare_match.group(1))

    name = listing_item.get("name") or slug_to_title(listing_item["url"])
    title_match = re.search(r"<title>([^<|]+)", html, re.I)
    if title_match:
        title = unescape(title_match.group(1)).strip()
        if title and "sierra" not in title.lower():
            name = title.split(" - ")[0].strip() or name

    fill_power = parse_fill_power(html, name=name)

    return {
        "product_id": listing_item["product_id"],
        "url": listing_item["url"],
        "name": name,
        "brand": listing_item.get("brand", ""),
        "price": price,
        "compare_at": compare_at,
        "composition": composition,
        "merino_pct": merino_pct,
        "shoe_material_pct": shoe_material_pct,
        "down_fill_pct": down_fill_pct,
        "fill_power": fill_power,
        "sizes": parse_available_sizes(html, product_id=listing_item["product_id"]),
    }


def matching_sizes(profile, product):
    if not profile:
        return product.get("sizes", [])

    category = product_size_category(product["name"])
    available = product.get("sizes") or []
    if not available:
        return []

    if category == "socks":
        matches = filter_sock_labels_by_fit(
            profile,
            available,
            product.get("brand") or "sierra",
        )
        return [row["label"] for row in matches]

    if category == "footwear":
        matches = filter_shoe_labels_by_fit(
            profile,
            available,
            product.get("brand") or "sierra",
        )
        return [row["label"] for row in matches]

    targets = preferred_sizes(profile, category=category, brand="sierra")
    if not targets:
        return available

    target_set = {str(size).strip().upper() for size in targets}
    return [
        size for size in available
        if str(size).strip().upper() in target_set
    ]


def qualifies_for_alert(profile, product):
    if is_sock_product(product["name"], product.get("url", "")):
        return False, "socks excluded"

    if profile and profile_gender(profile) == "men":
        if not is_mens_product(product["name"], product["url"]):
            return False, "not men's"

    if not product.get("composition"):
        return False, "missing material composition"

    material_ok, material_reason = passes_material_rules(product)
    if not material_ok:
        return False, material_reason

    if profile:
        if not product.get("sizes"):
            return False, "missing size data"

        fit_sizes = matching_sizes(profile, product)
        if not fit_sizes:
            return False, "no matching sizes in stock"

        product["fit_sizes"] = fit_sizes
    else:
        product["fit_sizes"] = product.get("sizes") or []

    return True, "ok"


def should_alert_price(previous, current_price):
    if previous is None:
        return True
    return float(previous.get("price", 0) or 0) > float(current_price)


def format_alert_line(product):
    fit_sizes = product.get("fit_sizes") or []
    price_text = format_price(product["price"])
    if fit_sizes:
        size_text = "/".join(fit_sizes)
        label = f"{product['name']} · {size_text} · ${price_text}"
    else:
        label = f"{product['name']} · ${price_text}"
    return format_linked_line(label, product["url"])


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
    print(f"Sierra monitor failure ({health['consecutive_failures']}): {message}", flush=True)
    if health["consecutive_failures"] >= FAILURE_ALERT_THRESHOLD:
        send_notification(f"Sierra monitor error: {message}")


def likely_down_listing(item):
    name = item.get("name", "")
    name_lower = name.lower()
    url = item.get("url", "").lower()
    if any(token in name_lower for token in ("button-down", "button down", "tie-down")):
        return False

    fill_power = listing_fill_power(item)
    has_outerwear = is_down_outerwear_name(name)
    if (
        fill_power is not None
        and fill_power >= MIN_DOWN_FILL_POWER
        and has_outerwear
    ):
        return True

    if "s~down-" in url:
        return has_outerwear
    if re.search(r"\bdown\b", name_lower) and has_outerwear:
        return True
    return False


def should_inspect_listing(item):
    if is_sock_product(item.get("name", ""), item.get("url", "")):
        return False
    text = f"{item.get('name', '')} {item.get('brand', '')} {item.get('url', '')}".lower()
    if "merino" in text:
        return True
    if likely_down_listing(item):
        fill_power = listing_fill_power(item)
        if fill_power is not None and fill_power < MIN_DOWN_FILL_POWER:
            return False
        return True
    if is_leather_shoe_listing(item):
        return shoe_alerts_enabled()
    return False


def prefilter_merino_listings_enabled():
    return os.getenv("SIERRA_PREFILTER_MERINO", "true").strip().lower() in ("1", "true", "yes")


def listings_to_inspect(listing_items):
    if not prefilter_merino_listings_enabled():
        return listing_items
    return [item for item in listing_items if should_inspect_listing(item)]


def fetch_category_products(fetcher, category_url):
    html = fetch_html(fetcher, category_url)
    return parse_listing_products(html)


def fetch_product_detail(fetcher, listing_item):
    html = fetch_html(fetcher, listing_item["url"])
    return parse_product_detail(html, listing_item)


def collect_products(fetcher, category_urls, health):
    listing_items = []
    seen_ids = set()
    category_errors = []
    for category_url in category_urls:
        print(f"Fetching {category_url}", flush=True)
        try:
            batch = fetch_category_products(fetcher, category_url)
        except Exception as exc:
            category_errors.append(f"{category_url}: {exc}")
            print(f"  Category fetch failed: {exc}", flush=True)
            continue
        print(f"  Found {len(batch)} products", flush=True)
        for item in batch:
            if item["product_id"] not in seen_ids:
                seen_ids.add(item["product_id"])
                listing_items.append(item)

    if not listing_items:
        detail = "; ".join(category_errors) if category_errors else "no products parsed"
        record_failure(health, f"No Sierra products found in configured categories ({detail})")
        return None, None

    record_success(health)
    inspect_items = listings_to_inspect(listing_items)
    if prefilter_merino_listings_enabled():
        skipped = len(listing_items) - len(inspect_items)
        print(
            f"  Inspecting {len(inspect_items)} likely matches "
            f"(skipped {skipped} without merino, down outerwear, or leather/suede shoe keywords)",
            flush=True,
        )
    else:
        print(f"Unique products to inspect: {len(inspect_items)}", flush=True)

    detailed_products = []
    if browser_fetch_enabled():
        run_budget_sec = float(os.getenv("SIERRA_RUN_BUDGET_SEC", "2400"))
        deadline = time.monotonic() + run_budget_sec
        for index, item in enumerate(inspect_items):
            if time.monotonic() >= deadline:
                remaining = len(inspect_items) - index
                print(
                    f"  Run budget of {run_budget_sec:g}s reached; "
                    f"skipping {remaining} remaining detail fetches",
                    flush=True,
                )
                break
            try:
                detailed_products.append(fetch_product_detail(fetcher, item))
            except Exception as exc:
                print(f"  Error fetching {item['product_id']}: {exc}", flush=True)
    else:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(fetch_product_detail, fetcher, item): item
                for item in inspect_items
            }
            for future in as_completed(futures):
                item = futures[future]
                try:
                    detailed_products.append(future.result())
                except Exception as exc:
                    print(f"  Error fetching {item['product_id']}: {exc}", flush=True)
    return listing_items, detailed_products


def main():
    profile = load_fit_profile()
    category_urls = parse_category_urls()

    print(f"Sierra categories: {len(category_urls)}", flush=True)
    print(f"Min merino wool: {MIN_MERINO_WOOL_PERCENT:g}%", flush=True)
    print(f"Min down fill: {MIN_DOWN_FILL_PERCENT:g}%", flush=True)
    print(f"Min down fill power: {MIN_DOWN_FILL_POWER:g}", flush=True)
    print(f"Shoe alerts: {shoe_alerts_enabled()}", flush=True)
    print(f"Fit profile: {profile is not None}", flush=True)
    if profile:
        print(f"Gender: {profile_gender(profile)}", flush=True)
    print(f"Dry run: {DRY_RUN}", flush=True)
    print(f"Browser fetch: {browser_fetch_enabled()}", flush=True)

    health = load_health()
    state = load_json_file(STATE_FILE, {})
    baseline_run = not state
    alerts = []

    if browser_fetch_enabled():
        with BrowserFetcher() as fetcher:
            _, detailed_products = collect_products(fetcher, category_urls, health)
    else:
        session = make_session()
        _, detailed_products = collect_products(session, category_urls, health)

    if not detailed_products:
        # Akamai often hard-blocks GitHub Actions IPs. Health/discord already
        # track consecutive failures; keep the workflow green so Zara still runs.
        print(
            "Sierra ended with no usable products (likely bot block). "
            "Exiting non-fatally; health alerts still apply.",
            flush=True,
        )
        return

    active_ids = set()
    for product in detailed_products:
        product_id = product["product_id"]
        qualifies, reason = qualifies_for_alert(profile, product)
        fit_sizes = product.get("fit_sizes") or []
        stock_text = ", ".join(fit_sizes) if fit_sizes else "no listed sizes"
        print(
            f"  {product['name']}: ${format_price(product['price'])} · "
            f"{material_label(product)} · sizes {stock_text} · {reason}",
            flush=True,
        )
        if not qualifies:
            continue

        active_ids.add(product_id)
        previous = state.get(product_id)
        if not baseline_run:
            if previous is None or should_alert_price(previous, product["price"]):
                alerts.append(format_alert_line(product))
                reason_label = "new" if previous is None else "price drop"
                print(f"  ALERT ({reason_label}): {product['name']}", flush=True)

        state[product_id] = {
            "price": product["price"],
            "compare_at": product.get("compare_at", 0),
            "merino_pct": product["merino_pct"],
            "shoe_material_pct": product.get("shoe_material_pct", 0),
            "down_fill_pct": product.get("down_fill_pct", 0),
            "fill_power": product.get("fill_power", 0),
            "fit_sizes": fit_sizes,
            "name": product["name"],
            "url": product["url"],
        }

    stale_ids = [key for key in state if key not in active_ids]
    for key in stale_ids:
        del state[key]
    if stale_ids:
        print(f"Removed {len(stale_ids)} stale Sierra entries", flush=True)

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
        send_notification(f"Sierra monitor crashed: {exc}")
        sys.exit(1)
