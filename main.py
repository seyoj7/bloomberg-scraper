import json
import time
import random
import asyncio
import logging
from functools import partial
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

# Configuration

TARGET_URL        = "https://www.bloomberg.com/crypto"
OUTPUT_DIR        = Path("news_outputs")
OUTPUT_JSON       = OUTPUT_DIR / "bloomberg_crypto.json"
POSTED_URLS_FILE  = OUTPUT_DIR / "posted_urls.json"
SCREENSHOT_DIR    = OUTPUT_DIR
COOKIES_DIR       = Path("cookies")
COOKIES_FILE      = COOKIES_DIR / "bloomberg_cookies.json"
HEADLESS          = False
MAX_STORED_ARTICLES = 200
MAX_SCREENSHOTS   = 10

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Logging

logging.Formatter.converter = lambda *args: time.gmtime((args[-1] if args else time.time()) + 19800)
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%I:%M:%S %p",
    level=logging.INFO,
)
log = logging.getLogger("bloomberg.cog")

# Playwright patch

def _patch_playwright_core_bundle():
    """Patches Playwright's coreBundle.js to prevent FF TypeError crashes on undefined location."""
    try:
        import re
        import playwright

        pw_dir      = Path(playwright.__file__).parent
        core_bundle = pw_dir / "driver" / "package" / "lib" / "coreBundle.js"
        if not core_bundle.exists():
            return

        content = core_bundle.read_text("utf-8")
        changed = False

        patched = re.sub(r'pageError\.location\.', r'pageError.location?.', content)
        if patched != content:
            content = patched
            changed = True

        patched = re.sub(
            r'pageError\.location\?\.url(?!\s*\?\?)',
            r"pageError.location?.url ?? ''",
            content,
        )
        if patched != content:
            content = patched
            changed = True

        patched = re.sub(
            r'pageError\.location\?\.lineNumber(?!\s*\?\?)',
            r'pageError.location?.lineNumber ?? 0',
            content,
        )
        if patched != content:
            content = patched
            changed = True

        patched = re.sub(
            r'pageError\.location\?\.columnNumber(?!\s*\?\?)',
            r'pageError.location?.columnNumber ?? 0',
            content,
        )
        if patched != content:
            content = patched
            changed = True

        if changed:
            core_bundle.write_text(content, "utf-8")
            log.info("🔧 Patched Playwright coreBundle.js — guarded pageError.location with fallbacks.")
        else:
            log.debug("Playwright coreBundle.js already patched — skipping.")
    except Exception as e:
        log.warning(f"Failed to patch playwright: {e}")


_patch_playwright_core_bundle()

# Persistence helpers

def load_json(path: Path, default=None):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default if default is not None else []


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _load_cookies() -> list:
    if COOKIES_FILE.exists():
        try:
            data = json.loads(COOKIES_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception as e:
            log.warning("⚠️  Failed to read cookie file: %s", e)
    return []


def _save_cookies(cookies: list):
    COOKIES_DIR.mkdir(parents=True, exist_ok=True)
    COOKIES_FILE.write_text(json.dumps(cookies, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("🍪 Saved %d cookies → %s", len(cookies), COOKIES_FILE)


def _cleanup_old_screenshots():
    try:
        shots = sorted(
            list(SCREENSHOT_DIR.glob("article_*.png")) + list(SCREENSHOT_DIR.glob("article_*.jpg")),
            key=lambda p: p.stat().st_mtime,
        )
        to_delete = shots[:-MAX_SCREENSHOTS] if len(shots) > MAX_SCREENSHOTS else []
        for p in to_delete:
            try:
                p.unlink()
                log.debug("🗑️  Deleted old screenshot: %s", p.name)
            except Exception as e:
                log.warning("Failed to delete screenshot %s: %s", p.name, e)
    except Exception as e:
        log.warning("Screenshot cleanup error: %s", e)

# JavaScript snippets

JS_EXTRACT_ARTICLES = """
    () => {
        const articles = [];
        const seen = new Set();

        // 1. JSON-LD
        document.querySelectorAll('script[type="application/ld+json"]').forEach(s => {
            try {
                const data = JSON.parse(s.textContent);
                const items = Array.isArray(data) ? data : [data];
                items.forEach(item => {
                    if ((item['@type'] === 'NewsArticle' || item['@type'] === 'Article') && item.url && item.headline) {
                        if (!seen.has(item.url)) {
                            seen.add(item.url);
                            articles.push({
                                title: item.headline,
                                url: item.url,
                                summary: (item.description || '').substring(0, 300),
                                timestamp: item.datePublished || ''
                            });
                        }
                    }
                });
            } catch(e) {}
        });

        // 2. DOM Links
        document.querySelectorAll('a[href*="/news/"], a[href*="/articles/"]').forEach(a => {
            const href = a.href || '';
            if (!href.includes('bloomberg.com') || seen.has(href)) return;

            let title = a.getAttribute('aria-label') || a.getAttribute('title') || '';
            if (title.length < 10) {
                const clone = a.cloneNode(true);
                clone.querySelectorAll('img, picture, figure').forEach(el => el.remove());
                title = clone.innerText?.trim() || '';
            }

            if (title.length >= 10 && title.includes(' ')) {
                seen.add(href);
                articles.push({ title, url: href, summary: '', timestamp: '' });
            }
        });

        return articles;
    }
"""

JS_EXTRACT_SUMMARY = """
    () => {
        let desc = document.querySelector('meta[property="og:description"]')?.content ||
                   document.querySelector('meta[name="description"]')?.content || '';
        if (desc.length > 30) return desc;

        const p = [...document.querySelectorAll('article p, [class*="body"] p, [class*="story"] p')]
            .map(p => p.innerText.trim()).filter(t => t.length > 60);
        return p.length ? p.slice(0, 3).join(' ') : '';
    }
"""

# Bot-protection bypass

HOLD_BUTTON_SELECTORS = [
    "[id*='hold-button']",
    "[class*='hold-button']",
    "[class*='captcha__button']",
    "[id*='captcha']",
    "button[type='button']",
    "button",
]


async def _is_challenge_page(page) -> bool:
    try:
        title = (await page.title()).lower()
        url   = page.url.lower()
        if any(x in title for x in ["just a moment", "please wait", "access denied", "security check"]):
            return True
        if any(x in url for x in ["cf-chl", "challenges.cloudflare.com", "datadome"]):
            return True
        body = (await page.inner_text("body", timeout=2000)).lower()
        return any(x in body for x in ["press & hold", "press and hold", "unusual activity", "not a robot", "human verification"])
    except Exception:
        return False


async def _find_hold_button(page):
    """Search all frames for a visible hold/captcha button.
    Returns (frame, element) or (None, None)."""
    for sel in HOLD_BUTTON_SELECTORS:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                return page, el
        except Exception:
            pass
    for frame in page.frames[1:]:
        for sel in HOLD_BUTTON_SELECTORS:
            try:
                el = await frame.query_selector(sel)
                if el and await el.is_visible():
                    return frame, el
            except Exception:
                pass
    return None, None


async def _get_iframe_offset(page, frame_obj):
    """Return the bounding-box of the <iframe> that hosts *frame_obj*, or None."""
    if frame_obj is page:
        return None
    try:
        for candidate in await page.query_selector_all("iframe"):
            try:
                if await candidate.content_frame() == frame_obj:
                    return await candidate.bounding_box()
            except Exception:
                pass
    except Exception:
        pass
    return None


async def _solve_press_hold(page) -> bool:
    """Locate the Press & Hold button and simulate a human-like hold."""
    try:
        await page.wait_for_load_state("load", timeout=5000)
    except Exception:
        pass
    await asyncio.sleep(3.0)

    frame_obj, btn = await _find_hold_button(page)
    if not btn:
        log.warning("⚠️  Could not locate Press & Hold button in any frame.")
        return False

    try:
        box = await btn.bounding_box()
        if not box:
            log.warning("⚠️  Button has no bounding box.")
            return False

        cx = box["x"] + box["width"]  / 2
        cy = box["y"] + box["height"] / 2

        # Correct for iframe offset when the button lives inside a sub-frame
        offset = await _get_iframe_offset(page, frame_obj)
        if offset:
            cx += offset["x"]
            cy += offset["y"]
            log.debug("Iframe offset: x=%.0f y=%.0f", offset["x"], offset["y"])

        log.info("🖱️  Press & Hold at (%.0f, %.0f) — holding…", cx, cy)

        # Cubic ease-in-out mouse path from a random start position
        vp      = page.viewport_size or {"width": 1366, "height": 768}
        start_x = random.randint(100, max(101, vp["width"]  - 200))
        start_y = random.randint(100, max(101, vp["height"] - 200))
        steps   = random.randint(25, 40)

        for i in range(steps + 1):
            t  = i / steps
            et = t * t * (3 - 2 * t)          # smoothstep
            mx = start_x + (cx - start_x) * et + random.uniform(-1.5, 1.5)
            my = start_y + (cy - start_y) * et + random.uniform(-1.5, 1.5)
            await page.mouse.move(mx, my)
            await asyncio.sleep(random.uniform(0.01, 0.03))

        # Hold with micro-jitter
        hold_time = random.uniform(12.0, 15.0)
        await page.mouse.down()
        t_end = asyncio.get_event_loop().time() + hold_time
        while asyncio.get_event_loop().time() < t_end:
            await page.mouse.move(
                cx + random.uniform(-1.2, 1.2),
                cy + random.uniform(-1.2, 1.2),
            )
            await asyncio.sleep(random.uniform(0.05, 0.12))

        await page.mouse.up()
        log.info("✅ Released after %.1fs — waiting for navigation…", hold_time)
        await asyncio.sleep(random.uniform(10.0, 14.0))
        return True

    except Exception as e:
        log.warning("Press & Hold failed: %s", e)
        return False


async def _solve_cloudflare(page, max_wait=90) -> bool:
    loop     = asyncio.get_event_loop()
    deadline = loop.time() + max_wait
    attempt  = 0

    while loop.time() < deadline:
        if not await _is_challenge_page(page):
            return True
        attempt += 1

        if attempt > 3:
            log.error("❌ Maximum Press & Hold attempts (3) reached.")
            return False

        body_parts = []
        for frame in page.frames:
            try:
                body_parts.append((await frame.inner_text("body", timeout=2000)).lower()[:300])
            except Exception:
                pass
        log.debug("🔍 Challenge body excerpt: %.200s", " ".join(body_parts))

        log.warning("⏳ Challenge active — attempting Press & Hold (attempt %d)…", attempt)
        solved = await _solve_press_hold(page)
        if solved:
            log.info("✅ Press & Hold action completed. Proceeding to wait for page content.")
            return True

        # Fallback: plain click on any visible button, then wait
        try:
            await page.click("button", timeout=3000)
            log.debug("Fallback click sent.")
        except Exception:
            pass
        await asyncio.sleep(5)

    log.error("❌ Challenge did not resolve in time.")
    return False


async def _human_scroll(page):
    await asyncio.sleep(random.uniform(1.5, 3.0))
    for _ in range(5):
        await page.mouse.wheel(0, random.randint(200, 500))
        await asyncio.sleep(random.uniform(0.2, 0.5))


async def _is_valid_bloomberg_page(page) -> bool:
    """Return True only when the real Bloomberg crypto page is loaded (not a challenge/redirect)."""
    try:
        url   = page.url.lower()
        title = (await page.title()).lower()
        if not url.startswith("https://www.bloomberg.com/crypto"):
            return False
        if await _is_challenge_page(page):
            return False
        if any(x in title for x in ["bloomberg", "crypto"]):
            return True
        # Fallback: check for known page markers in DOM
        marker = await page.query_selector("article, a[href*='/news/'], a[href*='/articles/']")
        return marker is not None
    except Exception:
        return False

# Scraper

async def run_once(existing_urls: set) -> list[dict]:
    from camoufox.async_api import AsyncCamoufox

    log.info("🦊 Launching Camoufox...")
    articles = []
    tab      = None

    try:
        async with AsyncCamoufox(headless=HEADLESS, window=(1920, 1080)) as browser:
            context = await browser.new_context(viewport={"width": 1920, "height": 1080})
            context.set_default_timeout(60_000)
            context.set_default_navigation_timeout(60_000)

            page = await context.new_page()

            # Inject saved cookies before navigating
            saved_cookies = await asyncio.get_running_loop().run_in_executor(None, _load_cookies)
            if saved_cookies:
                await context.add_cookies(saved_cookies)
                log.info("🍪 Injected %d cookies.", len(saved_cookies))

            log.info("🌐 Navigating to %s", TARGET_URL)
            await page.goto(TARGET_URL, wait_until="domcontentloaded")
            await asyncio.sleep(random.uniform(2.0, 4.0))

            # Bypass any bot-protection challenge
            challenge_solved = False
            if await _is_challenge_page(page):
                log.warning("⏳ Challenge detected — solving…")
                if not await _solve_cloudflare(page):
                    log.error("❌ Could not bypass challenge — aborting run.")
                    return []
                challenge_solved = True

            # Verify we're on the real Bloomberg page
            if not await _is_valid_bloomberg_page(page):
                log.warning("⚠️  Page validation failed (challenge/redirect/invalid) — skipping cookie save.")
                return []

            # Save fresh cookies only if a challenge was just solved
            if challenge_solved:
                fresh_cookies = await context.cookies()
                await asyncio.get_running_loop().run_in_executor(None, _save_cookies, fresh_cookies)

            await _human_scroll(page)

            try:
                await page.wait_for_selector("article, a[href*='/news/']", timeout=15_000)
            except Exception:
                pass

            now          = datetime.now().isoformat(timespec="seconds")
            raw_articles = await page.evaluate(JS_EXTRACT_ARTICLES)

            for a in raw_articles:
                a["scraped_at"] = now
                articles.append(a)

            if articles:
                log.info("📰 Extracted %d articles", len(articles))
            else:
                log.warning("⚠️ No articles found on the page.")

            # Enrich newest new article only
            new = [a for a in articles if a["url"] not in existing_urls]
            if new:
                latest = new[0]
                if not latest.get("summary"):
                    try:
                        tab = await context.new_page()
                        await tab.goto(latest["url"], wait_until="domcontentloaded")
                        if await _solve_cloudflare(tab, max_wait=60):
                            await asyncio.sleep(random.uniform(1.5, 3.0))
                            ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
                            path = str(SCREENSHOT_DIR / f"article_{ts}.png")
                            await tab.screenshot(path=path, type="jpeg", quality=70)
                            latest["screenshot"] = path
                            latest["summary"]    = (await tab.evaluate(JS_EXTRACT_SUMMARY) or "")[:500]
                    except Exception as e:
                        log.warning("Failed to enrich summary: %s", e)
                        latest["enrich_failed"] = True
                    finally:
                        if tab:
                            try:
                                await tab.close()
                            except Exception:
                                pass
                            tab = None

    except Exception as e:
        log.error("❌ [Camoufox] Run error: %s", e)
    finally:
        _cleanup_old_screenshots()

    return articles

# Module state

_posted_urls  = set()
_is_first_run = True

# News cycle

async def _run_news_cycle():
    """One full scrape-and-post cycle. Silently skips if scraping returns no data."""
    global _posted_urls, _is_first_run

    loop          = asyncio.get_running_loop()
    existing_data = await loop.run_in_executor(None, load_json, OUTPUT_JSON, [])
    existing_urls = {a["url"] for a in existing_data}

    # Single scrape attempt
    log.info("📡 Bloomberg scraping…")
    try:
        articles = await asyncio.wait_for(run_once(existing_urls), timeout=300.0)
    except asyncio.TimeoutError:
        log.error("[Bloomberg] Scrape timed out — waiting for next cycle.")
        return
    except Exception as e:
        log.error(f"[Bloomberg] Scrape failed: {e}")
        return

    if not articles:
        log.warning("[Bloomberg] Scrape returned no data — waiting for next cycle.")
        return

    # Persist new articles (capped to MAX_STORED_ARTICLES)
    new_articles = [a for a in articles if a["url"] not in existing_urls]
    if new_articles:
        all_articles = (new_articles + existing_data)[:MAX_STORED_ARTICLES]
        await loop.run_in_executor(None, partial(save_json, OUTPUT_JSON, all_articles))

    terminal_new = [a for a in articles if a["url"] not in _posted_urls]

    if _is_first_run:
        _is_first_run = False
        _posted_urls.update(a["url"] for a in articles)
        _posted_urls.update(existing_urls)
        await loop.run_in_executor(None, partial(save_json, POSTED_URLS_FILE, list(_posted_urls)))
        terminal_new = terminal_new[:1] if terminal_new else []

    # Print to Terminal
    for a in terminal_new:
        if a.get("enrich_failed"):
            continue

        title = a.get("title", "").strip()
        if title.endswith("Read more"):
            title = title[:-9].strip()
        if title.lower().startswith("breaking"):
            title = "BREAKING " + title[8:].lstrip()
        elif title.lower().startswith("exclusive"):
            title = "EXCLUSIVE " + title[9:].lstrip()

        a["title"] = title

        if len(title.split()) <= 5 or not a.get("summary") or len(title) > 256:
            _posted_urls.add(a["url"])
            continue

        print("\n" + "="*50)
        print(f"📰 {title}")
        print(f"🔗 URL: {a.get('url')}")
        if a.get("timestamp"):
            print(f"🕒 Published: {a.get('timestamp')}")
        print("-" * 50)
        
        summary = a["summary"][:3997] + "…" if len(a["summary"]) > 4000 else a["summary"]
        print(f"{summary}")
        
        screenshot = a.get("screenshot")
        if screenshot and Path(screenshot).is_file():
            print(f"📸 Screenshot saved at: {screenshot}")
        print("="*50 + "\n")

        _posted_urls.add(a["url"])
        log.info(f"📰 Displayed: {a.get('title')}")

    await loop.run_in_executor(None, partial(save_json, POSTED_URLS_FILE, list(_posted_urls)))

# Main entry point

async def main():
    global _posted_urls, _is_first_run
    _posted_urls  = set(load_json(POSTED_URLS_FILE, []))
    _is_first_run = True
    
    log.info("Starting Bloomberg News scraper in terminal mode...")
    
    while True:
        try:
            await _run_news_cycle()
        except Exception as e:
            log.error("Unexpected error in news cycle.", exc_info=e)
            
        log.info("Waiting 300 seconds before next cycle...")
        await asyncio.sleep(300)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Scraper stopped by user.")
