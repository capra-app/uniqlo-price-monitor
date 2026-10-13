# uniqlo-price-monitor

Discord (and optional Telegram) alerts when **new** men's UNIQLO sale items appear in your sizes on uniqlo.com/us.

## What it watches

- **Lines:** UNIQLO U and UNIQLO C (via product search; Uniqlo's path-based collection API returned no results for these lines)
- **Wool / cashmere:** Men's products with **at least 60% wool** or **at least 60% cashmere** (discovered via search, verified on product detail)
- **Gender:** Men's only (plus unisex with men's sizing; women's items excluded)
- **Sizes:** S, M, 30, 31 (configurable)
- **Availability:** In stock online only (`IN_STOCK` / `LOW_STOCK`)
- **Sale signals:** promo below list price, or Uniqlo `discount` price flag

## Notifications

Set `DISCORD_WEBHOOK_URL` (GitHub Actions secret + optional local env).

Optional backup channel:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

You get a message when something:

- newly goes on sale (including first time seen)
- drops in price while already on sale
- comes back in stock on sale (after being marked out of stock)

You also get **one** message when new products appear that match your sizes and are in stock online, even if not on sale.

Each alert is one linked line, e.g. `Name · S/COLOR · $29.9 (was $49.9, -40%)` or `Name · $49.9` for new products. No visible URL or brackets. No message if nothing to report.

Workflow failures also post to Discord when configured.

## Configuration

Environment variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `TARGET_SIZES` | `S,M,30,31` | Comma-separated sizes |
| `SEARCH_QUERIES` | `UNIQLO U:uniqlo u,UNIQLO C:uniqlo c` | `Label:query` pairs for U/C |
| `WOOL_SEARCH_QUERIES` | `wool,merino wool` | Comma-separated wool discovery searches |
| `CASHMERE_SEARCH_QUERIES` | `cashmere` | Comma-separated cashmere discovery searches |
| `MIN_WOOL_PERCENT` | `60` | Minimum wool % for non-U/C products |
| `MIN_CASHMERE_PERCENT` | `60` | Minimum cashmere % for non-U/C products |
| `DRY_RUN` | `false` | Log only; no sends or state writes |
| `MAX_WORKERS` | `6` | Parallel API fetch workers |
| `MAX_RETRIES` | `3` | HTTP / webhook retries |
| `FAILURE_ALERT_THRESHOLD` | `2` | Consecutive failures before error alert |

## Run locally

```bash
pip install -r requirements.txt
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
python -m unittest discover -s tests -v
python -u monitor.py
```

Dry run:

```bash
DRY_RUN=true python -u monitor.py
```

## GitHub Actions

Runs **once daily** at 21:00 UTC (4:00 PM Colombia / COT) and on manual dispatch.

Manual runs support **dry run** (workflow input) to test without notifications or state commits.

After each real run, `last_seen_prices.json` and `monitor_health.json` are committed back with `[skip ci]`.

Required secret: `DISCORD_WEBHOOK_URL`

Optional secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`

## Fit profile

Copy `fit_profile.example.json` to `fit_profile.json` and fill in your measurements. The file stays local and is gitignored.

For GitHub Actions, store the same JSON in the repository secret `FIT_PROFILE_JSON`. The workflow writes it to `fit_profile.json` before each run so Sierra size filtering works in CI.

The fit helper (`fit.py`) uses a strict filter by default: it only recommends sizes when a product or brand size chart has enough data to justify the fit. Your profile is set to **men's** clothing. For shoes, it also checks women's listings: same size for unisex brands like Vivobarefoot, or men's size + 1.5 US/EU for other brands. **Socks** use `sock_size_charts` in your fit profile: each brand maps sock labels (S/M/L or numeric ranges) to men's US shoe sizes.

```bash
cp fit_profile.example.json fit_profile.json
```

Optional env:

| Variable | Default | Purpose |
|----------|---------|---------|
| `FIT_PROFILE_FILE` | `fit_profile.json` | Path to your local fit profile |
| `FIT_STRICTNESS` | `strict` | `strict`, `moderate`, or `loose` |

## Arc'teryx watchlist

`arcteryx_monitor.py` watches specific products on outlet.arcteryx.com or arcteryx.com and sends Discord/Telegram alerts when:

- the price drops (default)
- your size goes out of stock (default; Arc'teryx only exposes in-stock vs out-of-stock, not quantity)
- your size comes back in stock (`alert_on: ["restock"]`)
- a main-site variant hits outlet pricing (`alert_on: ["outlet_price"]`)

Configure items in `arcteryx_watchlist.json`:

```json
[
  {
    "url": "https://outlet.arcteryx.com/us/en/shop/mens/kragg-cotton-pant-9530",
    "color": "*",
    "size": "30R",
    "alert_on": ["restock"]
  },
  {
    "url": "https://arcteryx.com/us/en/shop/mens/kragg-cotton-pant-9530",
    "color": "*",
    "size": "30R",
    "alert_on": ["outlet_price"]
  }
]
```

Use `"color": "*"` to watch every color in the requested size. Sizes like `30R` match Arc'teryx labels such as `30-R`.

### Merino outlet discovery

By default, the monitor also scans the Arc'teryx outlet men's catalog for **merino** products with **S or M** in stock online. The first run records a baseline without alerts; later runs notify you when a new matching size/color appears.

| Variable | Default | Purpose |
|----------|---------|---------|
| `ARCTERYX_MERINO_DISCOVERY` | `true` | Enable outlet merino scanning |
| `ARCTERYX_MERINO_SIZES` | `S,M` | Comma-separated sizes to watch |
| `ARCTERYX_MERINO_STATE_FILE` | `arcteryx_merino_state.json` | Discovery state file |
| `ARCTERYX_MAX_WORKERS` | `6` | Parallel product fetches |

Or set `ARCTERYX_WATCHLIST` to semicolon-separated `url|color|size` entries.

Run locally:

```bash
DRY_RUN=true python -u arcteryx_monitor.py
python -u arcteryx_monitor.py
```

The GitHub Actions workflow runs the Uniqlo, Arc'teryx, IKEA, and Sierra monitors on the same schedule.

## IKEA watchlist

`ikea_monitor.py` watches specific product pages on ikea.com (any market path in the URL) and sends Discord/Telegram alerts when:

- the item newly goes on sale (`NEW_LOWER_PRICE`, last chance, etc.)
- the price drops

Configure items in `ikea_watchlist.json`:

```json
[
  {
    "url": "https://www.ikea.com/co/es/p/lindbyn-espejo-negro-80597232/"
  }
]
```

Or set `IKEA_WATCHLIST` to semicolon-separated URLs.

The first run records a baseline without alerts.

```bash
DRY_RUN=true python -u ikea_monitor.py
python -u ikea_monitor.py
```

## Zara watchlist

`zara_monitor.py` watches specific zara.com product pages (any market path in the URL) and sends Discord/Telegram alerts when:

- a watched color newly goes on sale
- a watched color/size goes from in stock to out of stock

Configure items in `zara_watchlist.json`. The `v1` query parameter selects the color unless `colors` is `*`. Optional `sizes` limits stock checks to those labels.

```json
[
  {
    "url": "https://www.zara.com/co/es/pantalon-regular-fit-100-lana-p02381381.html?v1=571694692&v2=2737921",
    "sizes": ["40"],
    "colors": "*"
  }
]
```

The first run records a baseline without alerts. Product data is fetched from Zara's storefront API (`ZARA_STORE_ID`, default Colombia `11735`). Optional browser fallback: `ZARA_BROWSER_FETCH=true`.

```bash
DRY_RUN=true python -u zara_monitor.py
python -u zara_monitor.py
```

## Sierra monitor

`sierra_monitor.py` scans Sierra.com men's merino and down listings and alerts when:

- fabric composition is at least **50% merino wool** for apparel (strict; products without material data are skipped; **socks are excluded**)
- **down outerwear** has at least **70% down fill** in the insulation line
- **shoes** (not socks) need at least **50% leather and/or suede** combined
- your sizes are in stock (tops/bottoms `M` and waist `30/31`; **shoes** matched to your shoe size US `9.5` / EU `43`)
- the item is new since the last run, or the price drops

Default categories: [men's merino wool](https://www.sierra.com/s~merino-wool-men/men~d~5284/?perPage=96) and [men's down](https://www.sierra.com/s~down-men/men~d~5284/?perPage=96)

| Variable | Default | Purpose |
|----------|---------|---------|
| `SIERRA_CATEGORY_URLS` | men's merino + down categories | Semicolon-separated category URLs |
| `SIERRA_MIN_MERINO_WOOL_PERCENT` | `50` | Minimum merino wool content (apparel) |
| `SIERRA_MIN_DOWN_FILL_PERCENT` | `70` | Minimum down fill in insulation (jackets/vests) |
| `SIERRA_MIN_SHOE_MATERIAL_PERCENT` | `50` | Minimum combined leather + suede for shoes |
| `SIERRA_STATE_FILE` | `sierra_state.json` | Price/state history |
| `SIERRA_HEALTH_FILE` | `sierra_health.json` | Failure tracking |
| `SIERRA_MAX_WORKERS` | `4` | Parallel product fetches (HTTP mode only) |
| `SIERRA_PREFILTER_MERINO` | `true` | Skip detail fetches unless listing mentions merino, down outerwear, or leather/suede shoes |
| `SIERRA_BROWSER_DETAIL_TIMEOUT_MS` | `45000` | Per-product-page render timeout in browser mode |
| `SIERRA_BROWSER_SELECTOR_TIMEOUT_MS` | `180000` | Per-category-page wait, including Akamai challenge solve time |
| `SIERRA_BROWSER_FETCH_ATTEMPTS` | `3` | Retries with a fresh page when Sierra bot-blocks a fetch |
| `SIERRA_RUN_BUDGET_SEC` | `2400` | Overall time budget for detail fetches before stopping |
| `SIERRA_BROWSER_FETCH` | `true` | Use Patchright browser to bypass Akamai bot protection |
| `SIERRA_BROWSER_HEADLESS` | `false` | Run browser headed (macOS CI uses real Chrome) |
| `SIERRA_BROWSER_CHANNEL` | _(empty)_ | Browser channel, e.g. `chrome` on GitHub Actions |

Sierra runs on `macos-latest` so Akamai challenges can complete; the other monitors stay on Ubuntu.

The first run records a baseline without sending alerts.

```bash
DRY_RUN=true python -u sierra_monitor.py
python -u sierra_monitor.py
```

## Tests

```bash
python -m unittest discover -s tests -v
```
