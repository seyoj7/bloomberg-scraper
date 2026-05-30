"""
Bloomberg Crypto Scraper — Cloudflare Bypass Edition
=====================================================
Strategy:
  1. Camoufox (engine-level Firefox fingerprint spoofing) — runs sync in thread
  2. Playwright async Firefox fallback
  3. Human-like behavioral simulation (delays, mouse jitter, scrolling)
  4. Auto-detection of Cloudflare + DataDome challenges
  5. JSON output with deduplication
  6. Optional: proxy support via .env

Requirements:
  pip install camoufox[geoip] playwright python-dotenv
  camoufox fetch              (downloads browser binary on Windows)
  playwright install firefox  (fallback browser)

Run:
  python main.py
"""

# Optional: Residential proxy (strongly recommended for production)
# PROXY_SERVER=http://your-proxy-host:port
# PROXY_USER=username
# PROXY_PASS=password