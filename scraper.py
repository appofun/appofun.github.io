#!/usr/bin/env python3
"""
scraper.py — Google Play Portfolio Scraper
===========================================
Scrapes your Google Play developer page, builds app cards, injects them into
template.html, and writes a final index.html ready for GitHub Pages.

Run manually:  python scraper.py
Automated via: .github/workflows/update_apps.yml  (nightly cron)
"""

import os
import re
import sys
from datetime import datetime, timezone
from typing import List, Optional
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup


# ══════════════════════════════════════════════════════════════════════════════
#  ①  DEVELOPER CONFIGURATION  — the only section you need to edit
# ══════════════════════════════════════════════════════════════════════════════

# Your Google Play developer name exactly as it appears in the URL:
#   https://play.google.com/store/apps/developer?id=YOUR_DEVELOPER_NAME
# If you use a numeric dev ID instead, set USE_NUMERIC_ID = True and paste the
# number (e.g. "12345678901234567890") as DEVELOPER_PLAY_ID.
DEVELOPER_PLAY_ID = "8974289841252647548"
USE_NUMERIC_ID    = True

# These strings are injected into template.html via {{PORTFOLIO_TITLE}} etc.
PORTFOLIO_TITLE   = "MICROKODE"
PORTFOLIO_TAGLINE = "Crafting Premium Mobile Experiences"

# ──────────────────────────────────────────────────────────────────────────────
#  FALLBACK APPS
#  Shown when Google Play blocks the scraper (bot-check, network timeout, etc.)
#  These mirror your real published apps.
# ──────────────────────────────────────────────────────────────────────────────
FALLBACK_APPS: List[dict] = [
    {
        "title":  "Master Block Craft Galaxy",
        "url":    "https://play.google.com/store/apps/details?id=com.crafton2026mine.newai",
        "icon":   "https://placehold.co/240x240/18181b/4ade80?text=MB",
        "rating": "4.5",
    },
    {
        "title":  "CompassOne — Smart Compass",
        "url":    "https://play.google.com/store/apps/developer?id=8974289841252647548",
        "icon":   "https://placehold.co/240x240/18181b/4ade80?text=C1",
        "rating": "4.7",
    },
    {
        "title":  "AIS Weather",
        "url":    "https://play.google.com/store/apps/developer?id=8974289841252647548",
        "icon":   "https://placehold.co/240x240/18181b/4ade80?text=AW",
        "rating": "4.6",
    },
    {
        "title":  "TV Cast & Mirror",
        "url":    "https://play.google.com/store/apps/developer?id=8974289841252647548",
        "icon":   "https://placehold.co/240x240/18181b/4ade80?text=TV",
        "rating": "4.4",
    },
]


# ══════════════════════════════════════════════════════════════════════════════
#  ②  CONSTANTS  (no need to change these)
# ══════════════════════════════════════════════════════════════════════════════
BASE_PLAY_URL = "https://play.google.com"

if USE_NUMERIC_ID:
    DEVELOPER_PAGE_URL = f"{BASE_PLAY_URL}/store/apps/dev?id={DEVELOPER_PLAY_ID}"
else:
    DEVELOPER_PAGE_URL = f"{BASE_PLAY_URL}/store/apps/developer?id={DEVELOPER_PLAY_ID}"

# Appended to Google Play icon URLs to request a high-resolution version
ICON_HQ_SUFFIX = "=w240-h240-rw"

TEMPLATE_FILE = "template.html"
OUTPUT_FILE   = "index.html"
PLACEHOLDER   = "<!-- {{APPS_PLACEHOLDER}} -->"

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection":      "keep-alive",
    "Cache-Control":   "no-cache",
    "Sec-Fetch-Dest":  "document",
    "Sec-Fetch-Mode":  "navigate",
    "Sec-Fetch-Site":  "none",
}


# ══════════════════════════════════════════════════════════════════════════════
#  ③  SCRAPING HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def clean_icon_url(raw: str) -> str:
    """Strip any existing Google size suffix and append the HQ one."""
    if not raw:
        return ""
    # Remove trailing =wXXX-hXXX-... parameters (including the leading '=')
    cleaned = re.sub(r"=w\d+.*$", "", raw.strip())
    return cleaned + ICON_HQ_SUFFIX


def find_rating_near(element) -> Optional[str]:
    """
    Walk up to 7 ancestor elements looking for a Google Play rating
    expressed as an aria-label like 'Rated 4.5 stars out of 5'.
    """
    node = element
    for _ in range(7):
        if node is None:
            break
        hit = node.find(attrs={"aria-label": re.compile(r"Rated\s+\d", re.I)})
        if hit:
            m = re.search(r"(\d+\.?\d*)", hit.get("aria-label", ""))
            if m:
                return m.group(1)
        node = getattr(node, "parent", None)
    return None


def extract_icon_from_tag(link_tag) -> str:
    """Find the first Google CDN image inside a link tag."""
    for img in link_tag.find_all("img"):
        # img.src, data-src, or first entry in srcset
        for attr in ("src", "data-src"):
            val = img.get(attr, "")
            if "googleusercontent" in val or "play-lh" in val:
                return clean_icon_url(val)
        srcset = img.get("srcset", "")
        if srcset:
            first = srcset.split(",")[0].strip().split(" ")[0]
            if "googleusercontent" in first or "play-lh" in first:
                return clean_icon_url(first)
    return ""


def extract_title_from_tag(link_tag) -> str:
    """
    Try multiple signals to find the app's display name inside a link tag.
    Priority: aria-label > child span/div text > link text.
    Filters out price strings, ratings, and single-word noise.
    """
    # aria-label on the <a> itself (most reliable on Google Play)
    label = link_tag.get("aria-label", "").strip()
    if label and len(label) < 120:
        return label

    # Patterns to reject: prices, standalone numbers, star ratings, etc.
    _junk = re.compile(
        r"^\s*(\$[\d.]+|€[\d.]+|[\d.]+\s*(stars?|ratings?|reviews?|\*)|"
        r"free|install|rated|[\d,]+\+?)\s*$",
        re.I,
    )

    candidates = []
    for el in link_tag.find_all(["span", "div"]):
        txt = el.get_text(separator=" ", strip=True)
        if 3 < len(txt) < 120 and "\n" not in txt and not _junk.match(txt):
            candidates.append(txt)

    if candidates:
        # Prefer medium-length strings — app names are rarely under 4 chars
        # or over 60. Sort by length and pick the first reasonable one.
        filtered = [c for c in candidates if 4 <= len(c) <= 70]
        if filtered:
            return sorted(filtered, key=len)[0]
        return sorted(candidates, key=len)[0]

    # Last resort: full link text, strip junk lines
    lines = [l.strip() for l in link_tag.get_text(separator="\n").splitlines()
             if l.strip() and not _junk.match(l.strip())]
    if lines:
        return max(lines, key=len)[:80]

    return ""


# ══════════════════════════════════════════════════════════════════════════════
#  ④  MAIN SCRAPE FUNCTION
# ══════════════════════════════════════════════════════════════════════════════

def scrape_apps() -> Optional[List[dict]]:
    """
    Attempt to scrape the developer's Google Play page.
    Returns a list of app dicts on success, or None if scraping fails/is blocked.

    Two strategies are tried sequentially:
      A) BeautifulSoup DOM traversal  (works when Google returns full HTML)
      B) Raw regex fallback           (works when the DOM is minimally rendered)
    """
    print(f"[scraper] Connecting to: {DEVELOPER_PAGE_URL}")

    try:
        session = requests.Session()
        # Prime the session with a base request so cookies are established
        session.get(BASE_PLAY_URL, headers=REQUEST_HEADERS, timeout=10)

        response = session.get(
            DEVELOPER_PAGE_URL,
            headers=REQUEST_HEADERS,
            params={"hl": "en", "gl": "US"},
            timeout=20,
        )
        response.raise_for_status()

    except requests.exceptions.Timeout:
        print("[scraper] ✗  Request timed out.")
        return None
    except requests.exceptions.HTTPError as e:
        print(f"[scraper] ✗  HTTP error: {e}")
        return None
    except requests.exceptions.RequestException as e:
        print(f"[scraper] ✗  Network error: {e}")
        return None

    # Detect bot-check redirect (Google sometimes bounces to a CAPTCHA)
    if "play.google.com" not in response.url:
        print(f"[scraper] ✗  Redirected to {response.url} — likely a bot-check page.")
        return None

    content_len = len(response.text)
    print(f"[scraper]    HTTP {response.status_code} — {content_len:,} bytes received.")

    if content_len < 5_000:
        print("[scraper] ✗  Response too short — page may be blocked or empty.")
        return None

    # ── Strategy A: DOM traversal ──────────────────────────────────────────
    apps: List[dict] = []
    seen_ids: set = set()

    soup = BeautifulSoup(response.text, "html.parser")
    app_href_pattern = re.compile(r"/store/apps/details\?id=")

    for link_tag in soup.find_all("a", href=app_href_pattern):
        href = link_tag.get("href", "")

        # Extract the package name (app ID) from the URL
        qs = parse_qs(urlparse(href).query)
        app_id = qs.get("id", [None])[0]
        if not app_id or app_id in seen_ids:
            continue
        seen_ids.add(app_id)

        full_url = urljoin(BASE_PLAY_URL, href)
        icon     = extract_icon_from_tag(link_tag)
        title    = extract_title_from_tag(link_tag)
        rating   = find_rating_near(link_tag)

        if not title:
            # Derive a human-readable name from the package name as last resort
            title = app_id.split(".")[-1].replace("_", " ").title()

        apps.append({
            "title":  title,
            "url":    full_url,
            "icon":   icon,
            "rating": rating,
        })

    if apps:
        print(f"[scraper] ✓  Strategy A: extracted {len(apps)} app(s) via DOM traversal.")
        return apps

    # ── Strategy B: raw HTML regex ─────────────────────────────────────────
    print("[scraper]    Strategy A found nothing — falling back to raw regex …")
    raw = response.text

    raw_ids   = list(dict.fromkeys(re.findall(r"/store/apps/details\?id=([A-Za-z0-9_.]+)", raw)))
    raw_icons = re.findall(r"https://play-lh\.googleusercontent\.com/[A-Za-z0-9_\-]+", raw)
    icon_iter = iter(raw_icons)

    for app_id in raw_ids:
        if app_id in seen_ids:
            continue
        seen_ids.add(app_id)
        raw_icon = next(icon_iter, "")
        apps.append({
            "title":  app_id.split(".")[-1].replace("_", " ").title(),
            "url":    f"{BASE_PLAY_URL}/store/apps/details?id={app_id}",
            "icon":   clean_icon_url(raw_icon) if raw_icon else "",
            "rating": None,
        })

    if apps:
        print(f"[scraper] ✓  Strategy B: extracted {len(apps)} app(s) via regex.")
        return apps

    print("[scraper] ✗  Both strategies returned zero apps.")
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  ⑤  HTML CARD GENERATION
# ══════════════════════════════════════════════════════════════════════════════

def _svg_star() -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
        'fill="currentColor" class="w-3.5 h-3.5 text-amber-400 flex-shrink-0">'
        '<path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77'
        " 5.82 21.02 7 14.14 2 9.27l6.91-1.01L12 2z\"/></svg>"
    )


def _svg_play_arrow() -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
        'fill="currentColor" class="w-4 h-4 flex-shrink-0">'
        '<path d="M3 22V2l19 10L3 22z"/></svg>'
    )


def _svg_external_link() -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" '
        'fill="currentColor" class="w-3.5 h-3.5 opacity-60">'
        '<path fill-rule="evenodd" d="M5.22 14.78a.75.75 0 001.06 0l7.22-7.22v5.69'
        "a.75.75 0 001.5 0v-7.5a.75.75 0 00-.75-.75h-7.5a.75.75 0 000 1.5h5.69"
        'l-7.22 7.22a.75.75 0 000 1.06z" clip-rule="evenodd"/></svg>'
    )


def generate_card_html(app: dict) -> str:
    """Return a complete Tailwind-styled card div for one app."""
    title  = app.get("title") or "Unknown App"
    url    = app.get("url")   or "#"
    icon   = app.get("icon")  or "https://placehold.co/64x64/27272a/4ade80?text=App"
    rating = app.get("rating")

    safe_title = title.replace('"', "&quot;").replace("'", "&#39;")

    # Rating row (only rendered when a rating is available)
    rating_html = ""
    if rating:
        try:
            r_float = float(rating)
            rating_html = f"""
          <div class="flex items-center gap-1.5 mt-1.5">
            {_svg_star()}
            <span class="text-amber-400 text-xs font-bold tracking-tight">{r_float:.1f}</span>
            <span class="text-zinc-500 text-xs">/ 5</span>
          </div>"""
        except ValueError:
            pass  # malformed rating string — skip silently

    return f"""        <div class="card-wrapper group relative flex flex-col bg-zinc-900/70 backdrop-blur-md border border-zinc-800/80 rounded-3xl p-5 gap-5 transition-all duration-300 ease-out hover:-translate-y-2 hover:border-emerald-500/40 hover:shadow-[0_8px_48px_rgba(16,185,129,0.13)]">

          <!-- Top-edge glow line — appears on hover -->
          <div class="pointer-events-none absolute inset-x-0 top-0 h-px rounded-t-3xl bg-gradient-to-r from-transparent via-emerald-500/0 to-transparent transition-all duration-500 group-hover:via-emerald-500/70"></div>

          <!-- App icon + title + rating -->
          <div class="flex items-start gap-4">
            <div class="relative flex-shrink-0">
              <img
                src="{icon}"
                alt="{safe_title} icon"
                width="64" height="64"
                loading="lazy"
                class="w-16 h-16 rounded-2xl object-cover shadow-xl ring-1 ring-white/10"
                onerror="this.onerror=null;this.src='https://placehold.co/64x64/27272a/4ade80?text=App'"
              >
              <!-- Live / available indicator dot -->
              <span class="absolute -bottom-1 -right-1 w-3.5 h-3.5 bg-emerald-500 rounded-full ring-2 ring-zinc-900 shadow-sm shadow-emerald-500/60"></span>
            </div>

            <div class="flex-1 min-w-0 pt-0.5">
              <h3 class="text-white font-bold text-base leading-snug line-clamp-2">{title}</h3>
              {rating_html}
            </div>
          </div>

          <!-- Vertical spacer pushes CTA to card bottom -->
          <div class="flex-1"></div>

          <!-- CTA button -->
          <a
            href="{url}"
            target="_blank"
            rel="noopener noreferrer"
            class="flex items-center justify-center gap-2 w-full rounded-2xl border border-emerald-500/30 bg-emerald-500/10 px-4 py-3 text-sm font-semibold text-emerald-400 transition-all duration-200 hover:border-emerald-500 hover:bg-emerald-500 hover:text-zinc-900 active:scale-95"
          >
            {_svg_play_arrow()}
            Get it on Google Play
            {_svg_external_link()}
          </a>
        </div>"""


def build_cards_block(apps: List[dict]) -> str:
    if not apps:
        return (
            '        <p class="text-zinc-500 col-span-full text-center py-20 text-sm">'
            "No apps could be loaded at this time.</p>"
        )
    return "\n".join(generate_card_html(app) for app in apps)


# ══════════════════════════════════════════════════════════════════════════════
#  ⑥  PIPELINE ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    # 1. Attempt live scrape
    apps        = scrape_apps()
    data_source = "live scrape"

    if not apps:
        print("[scraper] ⚠  Falling back to FALLBACK_APPS defined in scraper.py.")
        apps        = FALLBACK_APPS
        data_source = "fallback (scraper.py)"

    print(f"[scraper]    Using {len(apps)} app(s) from: {data_source}.")

    # 2. Load template
    if not os.path.isfile(TEMPLATE_FILE):
        print(f"[scraper] ✗  {TEMPLATE_FILE} not found in: {os.getcwd()}", file=sys.stderr)
        sys.exit(1)

    with open(TEMPLATE_FILE, "r", encoding="utf-8") as fh:
        template = fh.read()

    if PLACEHOLDER not in template:
        print(
            f"[scraper] ✗  Placeholder '{PLACEHOLDER}' not found in {TEMPLATE_FILE}.\n"
            "         Make sure the template contains exactly that HTML comment.",
            file=sys.stderr,
        )
        sys.exit(1)

    # 3. Replace meta placeholders first (title, tagline, developer ID)
    updated_at = datetime.now(timezone.utc).strftime("%B %d, %Y at %H:%M UTC")

    output = template
    output = output.replace("{{PORTFOLIO_TITLE}}",   PORTFOLIO_TITLE)
    output = output.replace("{{PORTFOLIO_TAGLINE}}", PORTFOLIO_TAGLINE)
    output = output.replace("{{DEVELOPER_PLAY_ID}}", DEVELOPER_PLAY_ID)

    # 4. Build the card injection block
    cards_html = build_cards_block(apps)

    injection = (
        f"\n        <!-- ╔══ AUTO-GENERATED BLOCK ══╗ -->\n"
        f"        <!-- Last updated : {updated_at} -->\n"
        f"        <!-- Source       : {data_source} -->\n"
        f"        <!-- Apps counted : {len(apps)} -->\n"
        f"\n"
        f"{cards_html}\n"
        f"\n"
        f"        <!-- ╚══ END AUTO-GENERATED BLOCK ══╝ -->"
    )

    output = output.replace(PLACEHOLDER, injection)

    # 5. Write output file
    with open(OUTPUT_FILE, "w", encoding="utf-8") as fh:
        fh.write(output)

    print(
        f"[scraper] ✓  {OUTPUT_FILE} written successfully "
        f"({len(output):,} bytes, {len(apps)} app card(s))."
    )


if __name__ == "__main__":
    main()
