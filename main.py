import os
import json
import time
import random
import logging
import asyncio
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv

load_dotenv()

# Configuration
TARGET_URL     = "https://www.bloomberg.com/crypto"
OUTPUT_DIR     = Path("output")
OUTPUT_JSON    = OUTPUT_DIR / "bloomberg_crypto.json"
SCREENSHOT_DIR = OUTPUT_DIR
POLL_INTERVAL  = 60
MAX_RETRIES    = 3
HEADLESS       = False
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


# JavaScript Snippets (injected into pages)
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


# Pure helper (no I/O — shared by both code paths)
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

class _SyncAdapter:
    """Wraps a *synchronous* Playwright / Camoufox page."""

    label = "Camoufox"

    def __init__(self, page, context):
        self._page = page
        self._ctx  = context

    # — primitives -----------------------------------------------------------
    def sleep(self, secs):                      time.sleep(secs)
    def title(self):                             return self._page.title()
    @property
    def url(self):                               return self._page.url
    @property
    def frames(self):                            return self._page.frames
    @property
    def viewport_size(self):                     return self._page.viewport_size or {"width": 1366, "height": 768}

    def evaluate(self, js):                      return self._page.evaluate(js)
    def goto(self, url, **kw):                   return self._page.goto(url, **kw)
    def screenshot(self, **kw):                  return self._page.screenshot(**kw)
    def click(self, sel, **kw):                  return self._page.click(sel, **kw)
    def query_selector(self, sel):               return self._page.query_selector(sel)
    def query_selector_all(self, sel):           return self._page.query_selector_all(sel)
    def wait_for_selector(self, sel, **kw):      return self._page.wait_for_selector(sel, **kw)

    # — mouse ----------------------------------------------------------------
    def mouse_move(self, x, y):                  self._page.mouse.move(x, y)
    def mouse_down(self):                        self._page.mouse.down()
    def mouse_up(self):                          self._page.mouse.up()
    def mouse_wheel(self, dx, dy):               self._page.mouse.wheel(dx, dy)

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
        return _SyncAdapter(p, self._ctx)

    def close_page(self):
        self._page.close()


class _AsyncAdapter:
    """Wraps an *asynchronous* Playwright page — awaits internally."""

    label = "Playwright"

    def __init__(self, page, context):
        self._page = page
        self._ctx  = context

    # — primitives -----------------------------------------------------------
    async def sleep(self, secs):                  await asyncio.sleep(secs)
    async def title(self):                        return await self._page.title()
    @property
    def url(self):                                return self._page.url
    @property
    def frames(self):                             return self._page.frames
    @property
    def viewport_size(self):                      return self._page.viewport_size or {"width": 1366, "height": 768}

    async def evaluate(self, js):                 return await self._page.evaluate(js)
    async def goto(self, url, **kw):              return await self._page.goto(url, **kw)
    async def screenshot(self, **kw):             return await self._page.screenshot(**kw)
    async def click(self, sel, **kw):             return await self._page.click(sel, **kw)
    async def query_selector(self, sel):          return await self._page.query_selector(sel)
    async def query_selector_all(self, sel):      return await self._page.query_selector_all(sel)
    async def wait_for_selector(self, sel, **kw): return await self._page.wait_for_selector(sel, **kw)

    # — mouse ----------------------------------------------------------------
    async def mouse_move(self, x, y):             await self._page.mouse.move(x, y)
    async def mouse_down(self):                   await self._page.mouse.down()
    async def mouse_up(self):                     await self._page.mouse.up()
    async def mouse_wheel(self, dx, dy):          await self._page.mouse.wheel(dx, dy)

    # — frames ---------------------------------------------------------------
    async def frame_inner_text(self, frame, sel):  return await frame.inner_text(sel)
    async def frame_query_selector(self, frame, sel): return await frame.query_selector(sel)
    async def el_is_visible(self, el):             return await el.is_visible()
    async def el_bounding_box(self, el):           return await el.bounding_box()
    def el_content_frame(self, el):                return el.content_frame()  # sync in Playwright

    # — context-level --------------------------------------------------------
    async def new_page(self):
        p = await self._ctx.new_page()
        p.on("pageerror", lambda exc: None)  # suppress Bloomberg JS errors
        return _AsyncAdapter(p, self._ctx)

    async def close_page(self):
        await self._page.close()


def _is_async(adapter) -> bool:
    """Return True when the adapter is async (so callers know to await)."""
    return isinstance(adapter, _AsyncAdapter)

# Body text & challenge detection
def _get_full_body_text_sync(adapter) -> str:
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


async def _get_full_body_text_async(adapter) -> str:
    body = ""
    try:
        body = (await adapter.frame_inner_text(adapter.frames[0], "body"))[:1000].lower()
    except Exception:
        pass
    for frame in adapter.frames[1:]:
        try:
            body += " " + (await adapter.frame_inner_text(frame, "body"))[:500].lower()
        except Exception:
            pass
    return body


def _is_challenge_sync(adapter) -> bool:
    return _check_is_challenge_page(adapter.title(), adapter.url, _get_full_body_text_sync(adapter))


async def _is_challenge_async(adapter) -> bool:
    return _check_is_challenge_page(await adapter.title(), adapter.url, await _get_full_body_text_async(adapter))


# Hold-button finder
def _find_hold_button_sync(adapter):
    """Return (frame, element) or (None, None)."""
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


async def _find_hold_button_async(adapter):
    """Return (frame, element) or (None, None)."""
    for sel in HOLD_BUTTON_SELECTORS:
        try:
            el = await adapter.query_selector(sel)
            if el and await adapter.el_is_visible(el):
                return adapter, el
        except Exception:
            pass
    for frame in adapter.frames[1:]:
        for sel in HOLD_BUTTON_SELECTORS:
            try:
                el = await adapter.frame_query_selector(frame, sel)
                if el and await adapter.el_is_visible(el):
                    return frame, el
            except Exception:
                pass
    return None, None


# Press & Hold solver (core logic, shared)
def _press_hold_core(adapter, box, frame_obj, iframe_offset_fn, sleep_fn, mouse_move, mouse_down, mouse_up):
    """
    Core press-and-hold logic.  All I/O is injected via callables so the
    same code drives both sync and async paths.
    """
    cx = box["x"] + box["width"]  / 2
    cy = box["y"] + box["height"] / 2

    # Apply iframe offset if the button is inside an iframe
    offset = iframe_offset_fn(frame_obj)
    if offset:
        cx += offset["x"]
        cy += offset["y"]
        log.debug("Iframe offset: x=%.0f y=%.0f", offset["x"], offset["y"])

    log.info("🖱️  [%s] Press & Hold at (%.0f, %.0f) — holding…", adapter.label, cx, cy)

    vp = adapter.viewport_size
    start_x = random.randint(100, max(101, vp["width"]  - 200))
    start_y = random.randint(100, max(101, vp["height"] - 200))
    steps   = random.randint(25, 40)

    for i in range(steps + 1):
        t  = i / steps
        et = t * t * (3 - 2 * t)
        mx = start_x + (cx - start_x) * et + random.uniform(-1.5, 1.5)
        my = start_y + (cy - start_y) * et + random.uniform(-1.5, 1.5)
        mouse_move(mx, my)
        sleep_fn(random.uniform(0.01, 0.03))

    hold_time = random.uniform(12.0, 18.0)
    mouse_down()
    t_end = time.time() + hold_time
    while time.time() < t_end:
        mouse_move(cx + random.uniform(-1.2, 1.2), cy + random.uniform(-1.2, 1.2))
        sleep_fn(random.uniform(0.05, 0.12))

    mouse_up()
    log.info("✅ [%s] Released after %.1fs — waiting for navigation…", adapter.label, hold_time)
    sleep_fn(random.uniform(10.0, 14.0))
    return True


def _get_iframe_offset_sync(adapter, page_adapter, frame_obj):
    """Find the iframe element's bounding box offset (sync)."""
    if frame_obj is page_adapter:
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


async def _get_iframe_offset_async(adapter, page_adapter, frame_obj):
    """Find the iframe element's bounding box offset (async)."""
    if frame_obj is page_adapter:
        return None
    try:
        for candidate in await adapter.query_selector_all("iframe"):
            try:
                if adapter.el_content_frame(candidate) == frame_obj:
                    return await adapter.el_bounding_box(candidate)
            except Exception:
                pass
    except Exception:
        pass
    return None


def _solve_press_hold_sync(adapter) -> bool:
    frame_obj, btn = _find_hold_button_sync(adapter)
    if not btn:
        log.warning("⚠️  [%s] Could not locate Press & Hold button in any frame.", adapter.label)
        return False
    try:
        box = adapter.el_bounding_box(btn)
        if not box:
            log.warning("⚠️  [%s] Button has no bounding box.", adapter.label)
            return False
        return _press_hold_core(
            adapter, box, frame_obj,
            iframe_offset_fn=lambda f: _get_iframe_offset_sync(adapter, adapter, f),
            sleep_fn=time.sleep,
            mouse_move=adapter.mouse_move,
            mouse_down=adapter.mouse_down,
            mouse_up=adapter.mouse_up,
        )
    except Exception as e:
        log.warning("Press & Hold failed: %s", e)
        return False


async def _solve_press_hold_async(adapter) -> bool:
    frame_obj, btn = await _find_hold_button_async(adapter)
    if not btn:
        log.warning("⚠️  [%s] Could not locate Press & Hold button in any frame.", adapter.label)
        return False
    try:
        box = await adapter.el_bounding_box(btn)
        if not box:
            log.warning("⚠️  [%s] Button has no bounding box.", adapter.label)
            return False

        # For async, we need async wrappers around the callables
        async def _async_mouse_move(x, y): await adapter.mouse_move(x, y)
        async def _async_mouse_down():     await adapter.mouse_down()
        async def _async_mouse_up():       await adapter.mouse_up()

        # Since _press_hold_core uses sync calls, we handle async specially
        cx = box["x"] + box["width"]  / 2
        cy = box["y"] + box["height"] / 2

        offset = await _get_iframe_offset_async(adapter, adapter, frame_obj)
        if offset:
            cx += offset["x"]
            cy += offset["y"]
            log.debug("Iframe offset: x=%.0f y=%.0f", offset["x"], offset["y"])

        log.info("🖱️  [%s] Press & Hold at (%.0f, %.0f) — holding…", adapter.label, cx, cy)

        vp = adapter.viewport_size
        start_x = random.randint(100, max(101, vp["width"]  - 200))
        start_y = random.randint(100, max(101, vp["height"] - 200))
        steps   = random.randint(25, 40)

        for i in range(steps + 1):
            t  = i / steps
            et = t * t * (3 - 2 * t)
            mx = start_x + (cx - start_x) * et + random.uniform(-1.5, 1.5)
            my = start_y + (cy - start_y) * et + random.uniform(-1.5, 1.5)
            await adapter.mouse_move(mx, my)
            await asyncio.sleep(random.uniform(0.01, 0.03))

        hold_time = random.uniform(12.0, 18.0)
        await adapter.mouse_down()
        t_end = time.time() + hold_time
        while time.time() < t_end:
            await adapter.mouse_move(
                cx + random.uniform(-1.2, 1.2),
                cy + random.uniform(-1.2, 1.2),
            )
            await asyncio.sleep(random.uniform(0.05, 0.12))

        await adapter.mouse_up()
        log.info("✅ [%s] Released after %.1fs — waiting…", adapter.label, hold_time)
        await asyncio.sleep(random.uniform(10.0, 14.0))
        return True

    except Exception as e:
        log.warning("Press & Hold failed: %s", e)
        return False


# Challenge wait loop
def _cf_wait_sync(adapter, timeout: int = 90) -> bool:
    deadline = time.time() + timeout
    attempt  = 0
    while time.time() < deadline:
        if not _is_challenge_sync(adapter):
            return True
        attempt += 1
        body = _get_full_body_text_sync(adapter)
        if "press" in body and ("hold" in body or "robot" in body):
            log.warning("⏳ [%s] Press & Hold detected (attempt %d)…", adapter.label, attempt)
            if not _solve_press_hold_sync(adapter):
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

async def _cf_wait_async(adapter, timeout: int = 90) -> bool:
    deadline = time.time() + timeout
    attempt  = 0
    while time.time() < deadline:
        if not await _is_challenge_async(adapter):
            return True
        attempt += 1
        body = await _get_full_body_text_async(adapter)
        if "press" in body and ("hold" in body or "robot" in body):
            log.warning("⏳ [%s] Press & Hold detected (attempt %d)…", adapter.label, attempt)
            if not await _solve_press_hold_async(adapter):
                try:
                    await adapter.click("button", timeout=3000)
                except Exception:
                    pass
                await asyncio.sleep(3)
        else:
            log.warning("⏳ [%s] Bot challenge active — waiting… (attempt %d)", adapter.label, attempt)
            await asyncio.sleep(4)

    log.error("❌ [%s] Challenge did not resolve in time.", adapter.label)
    try:
        await adapter.screenshot(path=str(SCREENSHOT_DIR / "challenge_timeout.png"))
    except Exception:
        pass
    return False


# Article extraction (pure data → no sync/async split needed for processing)
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


def _extract_articles_sync(adapter) -> list[dict]:
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


async def _extract_articles_async(adapter) -> list[dict]:
    articles, seen_urls = [], set()
    now = datetime.now().isoformat(timespec="seconds")
    try:
        _process_json_ld_blocks(await adapter.evaluate(JS_EXTRACT_JSON_LD), seen_urls, articles, now)
    except Exception as e:
        log.debug("JSON-LD: %s", e)
    try:
        _process_dom_links(await adapter.evaluate(JS_EXTRACT_DOM_LINKS), seen_urls, articles, now)
    except Exception as e:
        log.debug("DOM extraction: %s", e)
    return articles


# Summary fetching

def _fetch_summary_sync(adapter, url: str) -> str:
    if not url or not url.startswith("http"):
        log.warning("   ⚠️  Skipping summary fetch — invalid URL: %r", url)
        return ""
    tab = None
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

        if _is_challenge_sync(tab):
            log.warning("   ⚠️  Challenge on article page — solving…")
            _cf_wait_sync(tab, timeout=60)

        ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
        try:
            tab.screenshot(path=str(SCREENSHOT_DIR / f"article_sync_{ts}.png"))
        except Exception:
            pass

        summary = (tab.evaluate(JS_EXTRACT_SUMMARY) or "")[:500]
        return summary
    except Exception as e:
        log.warning("   ⚠️  Could not fetch summary for %s: %s", url, e)
        return ""
    finally:
        if tab:
            try:
                tab.close_page()
            except Exception:
                pass


async def _fetch_summary_async(adapter, url: str) -> str:
    if not url or not url.startswith("http"):
        log.warning("   ⚠️  Skipping summary fetch — invalid URL: %r", url)
        return ""
    tab = None
    try:
        tab = await adapter.new_page()
        try:
            await tab.goto(url, wait_until="domcontentloaded", timeout=60_000)
        except Exception as nav_err:
            # Same Playwright Node.js driver crash guard as sync path.
            err_str = str(nav_err)
            if "Cannot read properties" in err_str or "location" in err_str:
                log.debug("   ⚠️  Navigation JS error (suppressed): %s", nav_err)
            else:
                raise
        await asyncio.sleep(random.uniform(1.5, 3.0))

        if await _is_challenge_async(tab):
            log.warning("   ⚠️  Challenge on article page — solving…")
            await _cf_wait_async(tab, timeout=60)

        ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
        try:
            await tab.screenshot(path=str(SCREENSHOT_DIR / f"article_async_{ts}.png"))
        except Exception:
            pass

        summary = (await tab.evaluate(JS_EXTRACT_SUMMARY) or "")[:500]
        return summary
    except Exception as e:
        log.warning("   ⚠️  Could not fetch summary for %s: %s", url, e)
        return ""
    finally:
        if tab:
            try:
                await tab.close_page()
            except Exception:
                pass


# High-level scrape orchestration
def _human_scroll_sync(adapter):
    """Simulate human-like scrolling and mouse jitter (sync)."""
    adapter.sleep(random.uniform(1.5, 3.0))
    for _ in range(10):
        adapter.mouse_wheel(0, random.randint(200, 450))
        adapter.sleep(random.uniform(0.1, 0.35))
    vp = adapter.viewport_size
    adapter.mouse_move(random.randint(100, vp["width"] - 100),
                       random.randint(100, vp["height"] - 100))
    adapter.sleep(random.uniform(0.8, 1.5))


async def _human_scroll_async(adapter):
    """Simulate human-like scrolling and mouse jitter (async)."""
    await adapter.sleep(random.uniform(1.5, 3.0))
    for _ in range(10):
        await adapter.mouse_wheel(0, random.randint(200, 450))
        await adapter.sleep(random.uniform(0.1, 0.35))
    vp = adapter.viewport_size
    await adapter.mouse_move(random.randint(100, vp["width"] - 100),
                             random.randint(100, vp["height"] - 100))
    await adapter.sleep(random.uniform(0.8, 1.5))


def _enrich_newest_article_sync(adapter, articles, existing_urls):
    """Fetch a summary for the single most-recent new article (sync)."""
    new = [a for a in articles if a["url"] not in existing_urls]
    if not new:
        log.info("ℹ️  [%s] All articles already known — skipping detail pages.", adapter.label)
        return
    latest = new[0]
    if not latest.get("summary"):
        latest["summary"] = _fetch_summary_sync(adapter, latest["url"])


async def _enrich_newest_article_async(adapter, articles, existing_urls):
    """Fetch a summary for the single most-recent new article (async)."""
    new = [a for a in articles if a["url"] not in existing_urls]
    if not new:
        log.info("ℹ️  [%s] All articles already known — skipping detail pages.", adapter.label)
        return
    latest = new[0]
    if not latest.get("summary"):
        latest["summary"] = await _fetch_summary_async(adapter, latest["url"])


# Camoufox (sync, run in thread)
def _scrape_camoufox_sync(cf_kwargs: dict, existing_urls: set) -> list[dict]:
    """Top-level guard — returns [] on any unhandled error."""
    try:
        return _scrape_camoufox_inner(cf_kwargs, existing_urls)
    except Exception as e:
        log.error("❌ [Camoufox] Unhandled error — returning empty list: %s", e)
        return []


def _scrape_camoufox_inner(cf_kwargs: dict, existing_urls: set) -> list[dict]:
    from camoufox.sync_api import Camoufox

    with Camoufox(**cf_kwargs) as browser:
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id="America/New_York",
        )
        # Suppress Bloomberg JS errors at the context level so ALL pages
        # (including summary tabs) are covered. This prevents the Node.js
        # Playwright driver crash: "Cannot read properties of undefined
        # (reading 'url')" when a pageerror has no location info.
        try:
            context.on("weberror", lambda _: None)
        except Exception:
            pass
        try:
            page = context.new_page()
            page.on("pageerror", lambda exc: None)  # suppress Bloomberg JS errors
            adapter = _SyncAdapter(page, context)

            log.info("🌐 [Camoufox] Navigating to %s", TARGET_URL)
            adapter.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60_000)
            time.sleep(random.uniform(2.0, 4.0))

            if not _cf_wait_sync(adapter):
                adapter.screenshot(path=str(SCREENSHOT_DIR / "cf_failed.png"))
                return []

            _human_scroll_sync(adapter)

            articles = _extract_articles_sync(adapter)
            log.info("📰 [Camoufox] Articles updated")

            _enrich_newest_article_sync(adapter, articles, existing_urls)
            return articles
        finally:
            context.close()


# Playwright (async fallback)
async def _scrape_playwright_async(existing_urls: set) -> list[dict]:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.firefox.launch(headless=HEADLESS)
        try:
            context = await browser.new_context(
                viewport={"width": 1920, "height": 1080},
                locale="en-US",
                timezone_id="America/New_York",
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            )
            try:
                page = await context.new_page()
                page.on("pageerror", lambda exc: None)  # suppress Bloomberg JS errors
                adapter = _AsyncAdapter(page, context)

                log.info("🌐 [Playwright] Navigating to %s", TARGET_URL)
                await adapter.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60_000)
                await asyncio.sleep(random.uniform(2.0, 4.0))

                if not await _cf_wait_async(adapter):
                    await adapter.screenshot(path=str(SCREENSHOT_DIR / "cf_failed.png"))
                    return []

                await _human_scroll_async(adapter)

                try:
                    await adapter.wait_for_selector("article, a[href*='/news/']", timeout=15_000)
                except Exception:
                    log.warning("Content selector timed-out — proceeding anyway")

                articles = await _extract_articles_async(adapter)
                log.info("📰 [Playwright] Articles updated")

                await _enrich_newest_article_async(adapter, articles, existing_urls)
                return articles
            finally:
                await context.close()
        finally:
            await browser.close()


# Dispatch & persistence
async def run_once(existing_urls: set) -> list[dict]:
    """Try Camoufox (in thread) first, then Playwright async fallback."""
    proxy_cfg = None
    if PROXY_SERVER:
        proxy_cfg = {"server": PROXY_SERVER}
        if PROXY_USER:
            proxy_cfg["username"] = PROXY_USER
            proxy_cfg["password"] = PROXY_PASS
        log.info("🔀 Using proxy: %s", PROXY_SERVER)

    # ── Camoufox (sync in thread) ────────────────────────────────
    try:
        from camoufox.sync_api import Camoufox  # noqa: F401 — import check
        log.info("🦊 Launching Camoufox (stealth Firefox)…")

        cf_kwargs = {"headless": HEADLESS}
        if proxy_cfg:
            cf_kwargs["proxy"] = proxy_cfg

        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor(max_workers=1) as pool:
            articles = await loop.run_in_executor(
                pool, _scrape_camoufox_sync, cf_kwargs, existing_urls,
            )
        return articles

    except ImportError:
        log.warning("⚠️  Camoufox not installed — falling back to Playwright Firefox")
    except Exception as e:
        log.error("Camoufox error: %s — falling back to Playwright", e)

    # ── Playwright async fallback ────────────────────────────────
    return await _scrape_playwright_async(existing_urls)


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


# Entry point
async def main():
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
                articles = await run_once(existing_urls)
                break
            except Exception as e:
                log.error("Attempt %d/%d failed: %s", attempt, MAX_RETRIES, e)
                if attempt < MAX_RETRIES:
                    wait = 15 * attempt
                    log.info("Retrying in %ds…", wait)
                    await asyncio.sleep(wait)

        if articles:
            save_output(articles)
        else:
            log.warning("No articles scraped this round.")

        log.info("😴 Sleeping %ds until next poll… \n", POLL_INTERVAL)
        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n⛔ Stopped by user.")
