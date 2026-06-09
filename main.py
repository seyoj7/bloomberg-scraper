import os
import json
import time
import random
import logging
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# Configuration
TARGET_URL     = "https://www.bloomberg.com/crypto"
OUTPUT_DIR     = Path("news_outputs")
OUTPUT_JSON    = OUTPUT_DIR / "bloomberg_crypto.json"
SCREENSHOT_DIR = OUTPUT_DIR
POLL_INTERVAL  = 300
MAX_RETRIES    = 3
HEADLESS       = True
LOG_LEVEL      = logging.INFO

PROXY_SERVER = os.getenv("PROXY_SERVER")
PROXY_USER   = os.getenv("PROXY_USER")
PROXY_PASS   = os.getenv("PROXY_PASS")

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    level=LOG_LEVEL,
)
log = logging.getLogger("bloomberg")

OUTPUT_DIR.mkdir(exist_ok=True)
SCREENSHOT_DIR.mkdir(exist_ok=True)


# ─── JavaScript Snippets ──────────────────────────────────────────────────────

JS_EXTRACT_JSON_LD = """
    () => {
        const scripts = document.querySelectorAll('script[type="application/ld+json"]');
        const out = [];
        scripts.forEach(s => {
            try { out.push(JSON.parse(s.textContent)); } catch(e) {}
        });
        return out;
    }
"""

JS_EXTRACT_DOM_LINKS = """
    () => {
        const results = [];
        const links = document.querySelectorAll('a[href*="/news/"], a[href*="/articles/"]');
        links.forEach(a => {
            const href = a.href || '';
            if (!href.includes('bloomberg.com')) return;

            let title = '';

            // Priority 1: aria-label (Bloomberg sets this to the real headline on hero cards)
            const ariaLabel = a.getAttribute('aria-label')?.trim();
            if (ariaLabel && ariaLabel.length >= 10) {
                title = ariaLabel;
            }

            // Priority 2: title attribute
            if (!title) {
                const titleAttr = a.getAttribute('title')?.trim();
                if (titleAttr && titleAttr.length >= 10) {
                    title = titleAttr;
                }
            }

            // Priority 3: heading element innerText (only if it has real word content)
            if (!title) {
                const heading = a.querySelector('h1,h2,h3,h4');
                if (heading) {
                    // Clone and remove any img elements to avoid alt text leaking in
                    const clone = heading.cloneNode(true);
                    clone.querySelectorAll('img').forEach(img => img.remove());
                    const headingText = clone.innerText?.trim() || '';
                    // Accept if it has multiple words (real headline, not an image label)
                    if (headingText.length >= 10 && headingText.includes(' ')) {
                        title = headingText;
                    }
                }
            }

            // Priority 4: anchor innerText (strip images first)
            if (!title) {
                const clone = a.cloneNode(true);
                clone.querySelectorAll('img, picture, figure').forEach(el => el.remove());
                const anchorText = clone.innerText?.trim() || '';
                if (anchorText.length >= 10 && anchorText.includes(' ')) {
                    title = anchorText;
                }
            }

            if (title) {
                results.push({ title, url: href });
            }
        });
        return results;
    }
"""

JS_EXTRACT_SUMMARY = """
    () => {
        // 1) JSON-LD description
        const scripts = document.querySelectorAll('script[type="application/ld+json"]');
        for (const s of scripts) {
            try {
                const d = JSON.parse(s.textContent);
                const items = Array.isArray(d) ? d : [d];
                for (const item of items) {
                    if (item.description && item.description.length > 30)
                        return item.description;
                }
            } catch(e) {}
        }
        // 2) og:description meta tag
        const og = document.querySelector('meta[property="og:description"]');
        if (og && og.content && og.content.length > 30) return og.content;
        // 3) name=description meta tag
        const meta = document.querySelector('meta[name="description"]');
        if (meta && meta.content && meta.content.length > 30) return meta.content;
        // 4) First substantial paragraphs from article body
        const selectors = [
            'article p', '[class*="body"] p', '[class*="article"] p',
            '[class*="story"] p', 'main p'
        ];
        for (const sel of selectors) {
            const paras = [...document.querySelectorAll(sel)]
                .map(p => p.innerText.trim())
                .filter(t => t.length > 60);
            if (paras.length > 0)
                return paras.slice(0, 3).join(' ');
        }
        return '';
    }
"""

HOLD_BUTTON_SELECTORS = [
    "[id*='hold-button']",
    "[class*='hold-button']",
    "[class*='captcha__button']",
    "[id*='captcha']",
    "button[type='button']",
    "button",
]


# ─── Adapter ─────────────────────────────────────────────────────────────────

class _Adapter:
    """Thin wrapper around a synchronous Camoufox page."""

    label = "Camoufox"

    def __init__(self, page, context):
        self._page = page
        self._ctx  = context

    # — primitives -----------------------------------------------------------
    def sleep(self, secs):                        time.sleep(secs)
    def title(self):                              return self._page.title()
    @property
    def url(self):                                return self._page.url
    @property
    def frames(self):                             return self._page.frames
    @property
    def viewport_size(self):                      return self._page.viewport_size or {"width": 1366, "height": 768}

    def evaluate(self, js):                       return self._page.evaluate(js)
    def goto(self, url, **kw):                    return self._page.goto(url, **kw)
    def screenshot(self, **kw):                   return self._page.screenshot(**kw)
    def click(self, sel, **kw):                   return self._page.click(sel, **kw)
    def query_selector(self, sel):                return self._page.query_selector(sel)
    def query_selector_all(self, sel):            return self._page.query_selector_all(sel)
    def wait_for_selector(self, sel, **kw):       return self._page.wait_for_selector(sel, **kw)

    # — mouse ----------------------------------------------------------------
    def mouse_move(self, x, y):                   self._page.mouse.move(x, y)
    def mouse_down(self):                         self._page.mouse.down()
    def mouse_up(self):                           self._page.mouse.up()
    def mouse_wheel(self, dx, dy):                self._page.mouse.wheel(dx, dy)

    # — frames ---------------------------------------------------------------
    def frame_inner_text(self, frame, sel):       return frame.inner_text(sel)
    def frame_query_selector(self, frame, sel):   return frame.query_selector(sel)
    def el_is_visible(self, el):                  return el.is_visible()
    def el_bounding_box(self, el):                return el.bounding_box()
    def el_content_frame(self, el):               return el.content_frame()

    # — context-level --------------------------------------------------------
    def new_page(self):
        p = self._ctx.new_page()
        p.on("pageerror", lambda exc: None)
        return _Adapter(p, self._ctx)

    def close_page(self):
        self._page.close()


# ─── Shared helpers ───────────────────────────────────────────────────────────

def _check_is_challenge_page(title: str, url: str, body: str) -> bool:
    title = title.lower()
    return (
        "just a moment" in title
        or "please wait" in title
        or "cf-chl" in url
        or "challenges.cloudflare.com" in url
        or "datadome" in url
        or "press & hold" in body
        or "press and hold" in body
        or "unusual activity" in body
        or "not a robot" in body
        or "human verification" in body
        or "access denied" in title
        or "security check" in title
    )


def _get_full_body_text(adapter) -> str:
    body = ""
    try:
        body = adapter.frame_inner_text(adapter.frames[0], "body")[:1000].lower()
    except Exception:
        pass
    for frame in adapter.frames[1:]:
        try:
            body += " " + adapter.frame_inner_text(frame, "body")[:500].lower()
        except Exception:
            pass
    return body


def _is_challenge(adapter) -> bool:
    return _check_is_challenge_page(adapter.title(), adapter.url, _get_full_body_text(adapter))


def _find_hold_button(adapter):
    """Return (frame_or_adapter, element) or (None, None)."""
    # Main frame
    for sel in HOLD_BUTTON_SELECTORS:
        try:
            el = adapter.query_selector(sel)
            if el and adapter.el_is_visible(el):
                return adapter, el
        except Exception:
            pass
    # Child frames
    for frame in adapter.frames[1:]:
        for sel in HOLD_BUTTON_SELECTORS:
            try:
                el = adapter.frame_query_selector(frame, sel)
                if el and adapter.el_is_visible(el):
                    return frame, el
            except Exception:
                pass
    return None, None


def _get_iframe_offset(adapter, frame_obj):
    """Return the iframe element's bounding box offset, or None."""
    if frame_obj is adapter:
        return None
    try:
        for candidate in adapter.query_selector_all("iframe"):
            try:
                if adapter.el_content_frame(candidate) == frame_obj:
                    return adapter.el_bounding_box(candidate)
            except Exception:
                pass
    except Exception:
        pass
    return None


def _solve_press_hold(adapter) -> bool:
    frame_obj, btn = _find_hold_button(adapter)
    if not btn:
        log.warning("⚠️  [%s] Could not locate Press & Hold button in any frame.", adapter.label)
        return False
    try:
        box = adapter.el_bounding_box(btn)
        if not box:
            log.warning("⚠️  [%s] Button has no bounding box.", adapter.label)
            return False

        cx = box["x"] + box["width"]  / 2
        cy = box["y"] + box["height"] / 2

        offset = _get_iframe_offset(adapter, frame_obj)
        if offset:
            cx += offset["x"]
            cy += offset["y"]
            log.debug("Iframe offset: x=%.0f y=%.0f", offset["x"], offset["y"])

        log.info("🖱️  [%s] Press & Hold at (%.0f, %.0f) — holding…", adapter.label, cx, cy)

        vp      = adapter.viewport_size
        start_x = random.randint(100, max(101, vp["width"]  - 200))
        start_y = random.randint(100, max(101, vp["height"] - 200))
        steps   = random.randint(25, 40)

        for i in range(steps + 1):
            t  = i / steps
            et = t * t * (3 - 2 * t)
            mx = start_x + (cx - start_x) * et + random.uniform(-1.5, 1.5)
            my = start_y + (cy - start_y) * et + random.uniform(-1.5, 1.5)
            adapter.mouse_move(mx, my)
            adapter.sleep(random.uniform(0.01, 0.03))

        hold_time = random.uniform(12.0, 15.0)
        adapter.mouse_down()
        t_end = time.time() + hold_time
        while time.time() < t_end:
            adapter.mouse_move(
                cx + random.uniform(-1.2, 1.2),
                cy + random.uniform(-1.2, 1.2),
            )
            adapter.sleep(random.uniform(0.05, 0.12))

        adapter.mouse_up()
        log.info("✅ [%s] Released after %.1fs — waiting for navigation…", adapter.label, hold_time)
        adapter.sleep(random.uniform(10.0, 14.0))
        return True

    except Exception as e:
        log.warning("Press & Hold failed: %s", e)
        return False


def _cf_wait(adapter, timeout: int = 90) -> bool:
    deadline = time.time() + timeout
    attempt  = 0
    while time.time() < deadline:
        if not _is_challenge(adapter):
            return True
        attempt += 1
        body = _get_full_body_text(adapter)
        if "press" in body and ("hold" in body or "robot" in body):
            log.warning("⏳ [%s] Press & Hold detected (attempt %d)…", adapter.label, attempt)
            if not _solve_press_hold(adapter):
                try:
                    adapter.click("button", timeout=3000)
                except Exception:
                    pass
                time.sleep(3)
        else:
            log.warning("⏳ [%s] Bot challenge active — waiting… (attempt %d)", adapter.label, attempt)
            time.sleep(4)

    log.error("❌ [%s] Challenge did not resolve in time.", adapter.label)
    try:
        adapter.screenshot(path=str(SCREENSHOT_DIR / "challenge_timeout.png"))
    except Exception:
        pass
    return False


# ─── Article extraction ───────────────────────────────────────────────────────

def _process_json_ld_blocks(ld_blocks, seen_urls, articles, now):
    for block in ld_blocks:
        items = block if isinstance(block, list) else [block]
        for item in items:
            if item.get("@type") in ("NewsArticle", "Article"):
                url   = item.get("url", "")
                title = item.get("headline", "")
                if url and title and len(title) >= 10 and url not in seen_urls:
                    seen_urls.add(url)
                    articles.append({
                        "title":      title,
                        "url":        url,
                        "summary":    item.get("description", "")[:300],
                        "timestamp":  item.get("datePublished", ""),
                        "scraped_at": now,
                    })


def _process_dom_links(raw_links, seen_urls, articles, now):
    for item in raw_links:
        url   = item.get("url", "")
        title = item.get("title", "")
        if url and title and url not in seen_urls:
            seen_urls.add(url)
            articles.append({
                "title":      title,
                "url":        url,
                "summary":    "",
                "timestamp":  "",
                "scraped_at": now,
            })


def _extract_articles(adapter) -> list[dict]:
    articles, seen_urls = [], set()
    now = datetime.now().isoformat(timespec="seconds")
    try:
        _process_json_ld_blocks(adapter.evaluate(JS_EXTRACT_JSON_LD), seen_urls, articles, now)
    except Exception as e:
        log.debug("JSON-LD: %s", e)
    try:
        _process_dom_links(adapter.evaluate(JS_EXTRACT_DOM_LINKS), seen_urls, articles, now)
    except Exception as e:
        log.debug("DOM extraction: %s", e)
    return articles


# ─── Summary fetching ─────────────────────────────────────────────────────────

def _fetch_summary(adapter, url: str) -> tuple[str, str | None]:
    """Return (summary_text, screenshot_path_or_None) for the given article URL."""
    if not url or not url.startswith("http"):
        log.warning("   ⚠️  Skipping summary fetch — invalid URL: %r", url)
        return "", None
    tab = None
    screenshot_path: str | None = None
    try:
        tab = adapter.new_page()
        try:
            tab.goto(url, wait_until="domcontentloaded", timeout=60_000)
        except Exception as nav_err:
            # Playwright's Node.js driver can crash with:
            #   "Cannot read properties of undefined (reading 'url')"
            # when Bloomberg pages throw uncaught JS errors with no
            # source location. Treat this as a non-fatal navigation error.
            err_str = str(nav_err)
            if "Cannot read properties" in err_str or "location" in err_str:
                log.debug("   ⚠️  Navigation JS error (suppressed): %s", nav_err)
            else:
                raise
        time.sleep(random.uniform(1.5, 3.0))

        if _is_challenge(tab):
            log.warning("   ⚠️  Challenge on article page — solving…")
            _cf_wait(tab, timeout=60)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        try:
            _path = str(SCREENSHOT_DIR / f"article_{ts}.png")
            tab.screenshot(path=_path)
            screenshot_path = _path
        except Exception:
            pass

        summary = (tab.evaluate(JS_EXTRACT_SUMMARY) or "")[:500]
        return summary, screenshot_path
    except Exception as e:
        log.warning("   ⚠️  Could not fetch summary for %s: %s", url, e)
        return "", None
    finally:
        if tab:
            try:
                tab.close_page()
            except Exception:
                pass


# ─── High-level scrape orchestration ─────────────────────────────────────────

def _human_scroll(adapter):
    """Simulate human-like scrolling and mouse jitter."""
    adapter.sleep(random.uniform(1.5, 3.0))
    for _ in range(10):
        adapter.mouse_wheel(0, random.randint(200, 450))
        adapter.sleep(random.uniform(0.1, 0.35))
    vp = adapter.viewport_size
    adapter.mouse_move(
        random.randint(100, vp["width"] - 100),
        random.randint(100, vp["height"] - 100),
    )
    adapter.sleep(random.uniform(0.8, 1.5))


def _enrich_newest_article(adapter, articles, existing_urls):
    """Fetch a summary and screenshot for the single most-recent new article."""
    new = [a for a in articles if a["url"] not in existing_urls]
    if not new:
        log.info("ℹ️  [%s] All articles already known — skipping detail pages.", adapter.label)
        return
    latest = new[0]
    if not latest.get("summary"):
        summary, screenshot_path = _fetch_summary(adapter, latest["url"])
        latest["summary"] = summary
        if screenshot_path:
            latest["screenshot"] = screenshot_path
            log.debug("📸 Screenshot saved → %s", screenshot_path)


# ─── Camoufox scraper ────────────────────────────────────────────────────────

def run_once(existing_urls: set) -> list[dict]:
    """Run one full scrape cycle with Camoufox."""
    from camoufox.sync_api import Camoufox

    proxy_cfg = None
    if PROXY_SERVER:
        proxy_cfg = {"server": PROXY_SERVER}
        if PROXY_USER:
            proxy_cfg["username"] = PROXY_USER
            proxy_cfg["password"] = PROXY_PASS
        log.info("🔀 Using proxy: %s", PROXY_SERVER)

    cf_kwargs = {"headless": HEADLESS}
    if proxy_cfg:
        cf_kwargs["proxy"] = proxy_cfg

    log.info("🦊 Launching Camoufox (stealth Firefox)…")
    try:
        with Camoufox(**cf_kwargs) as browser:
            context = browser.new_context(
                viewport={"width": 1920, "height": 1080},
                locale="en-US",
                timezone_id="America/New_York",
            )
            for evt in ("weberror", "pageerror"):
                try:
                    context.on(evt, lambda _: None)
                except Exception:
                    pass
            try:
                page = context.new_page()
                page.on("pageerror", lambda exc: None)
                adapter = _Adapter(page, context)

                log.info("🌐 [%s] Navigating to %s", adapter.label, TARGET_URL)
                try:
                    adapter.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60_000)
                except Exception as nav_err:
                    err_str = str(nav_err)
                    if "Cannot read properties" in err_str or "location" in err_str:
                        log.debug("⚠️  Main-page nav JS error (suppressed): %s", nav_err)
                    else:
                        raise
                time.sleep(random.uniform(2.0, 4.0))

                if not _cf_wait(adapter):
                    adapter.screenshot(path=str(SCREENSHOT_DIR / "cf_failed.png"))
                    return []

                _human_scroll(adapter)

                try:
                    adapter.wait_for_selector("article, a[href*='/news/']", timeout=15_000)
                except Exception:
                    log.warning("Content selector timed-out — proceeding anyway")

                articles = _extract_articles(adapter)
                log.info("📰 [%s] Articles updated", adapter.label)

                _enrich_newest_article(adapter, articles, existing_urls)
                return articles
            finally:
                context.close()
    except Exception as e:
        log.error("❌ [Camoufox] Unhandled error — returning empty list: %s", e)
        return []


# ─── Persistence ─────────────────────────────────────────────────────────────

def load_existing_json() -> list[dict]:
    if OUTPUT_JSON.exists():
        try:
            return json.loads(OUTPUT_JSON.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def save_output(articles: list[dict]) -> int:
    existing      = load_existing_json()
    existing_urls = {a["url"] for a in existing}
    new_articles  = [a for a in articles if a["url"] not in existing_urls]

    if not new_articles:
        log.info("ℹ️  No new articles found.")
        return 0

    all_articles = new_articles + existing  # newest first
    OUTPUT_JSON.write_text(
        json.dumps(all_articles, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    log.info("✅ Saved %d new articles → %s", len(new_articles), OUTPUT_JSON)
    return len(new_articles)


# ─── Entry point ─────────────────────────────────────────────────────────────

def main():
    log.info("🚀 Bloomberg Crypto Scraper started")
    log.info("   Poll interval  : %ds", POLL_INTERVAL)
    print("\nPress Ctrl+C to stop.\n")

    loop_count = 0
    while True:
        loop_count += 1
        log.info("🔄 Loop %d", loop_count)

        existing_urls = {a["url"] for a in load_existing_json()}
        articles      = []

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                articles = run_once(existing_urls)
                break
            except Exception as e:
                log.error("Attempt %d/%d failed: %s", attempt, MAX_RETRIES, e)
                if attempt < MAX_RETRIES:
                    wait = 15 * attempt
                    log.info("Retrying in %ds…", wait)
                    time.sleep(wait)

        if articles:
            save_output(articles)
        else:
            log.warning("No articles scraped this round.")

        log.info("😴 Sleeping %ds until next poll… \n", POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⛔ Stopped by user.")
