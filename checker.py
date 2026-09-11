"""
checker.py — Playwright-based Facebook Link Health Checker
Returns a CheckResult dataclass with status ACTIVE | DEAD | ERROR
"""

from __future__ import annotations

import os
import re
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse, parse_qs

from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    PlaywrightContextManager,
    TimeoutError as PWTimeout,
    Error as PWError,
)

logger = logging.getLogger(__name__)

BROWSER_TIMEOUT = int(os.getenv("BROWSER_TIMEOUT", "30000"))
PROXY_URL = os.getenv("PROXY_URL", "").strip() or None

# Resource types to block (saves RAM & bandwidth on cloud)
BLOCKED_RESOURCE_TYPES = {"image", "media", "font", "stylesheet", "other"}

BLOCKED_URL_PATTERNS = [
    r"\.mp4", r"\.webm", r"\.m3u8", r"\.ts",
    r"facebook\.com/ajax/bz",
    r"connect\.facebook\.net/signals",
    r"an\.facebook\.com",
    r"pixel\.facebook\.com",
    r"google-analytics\.com",
    r"doubleclick\.net",
    r"fbcdn\.net/v/",                # video CDN
]
_BLOCKED_RE = re.compile("|".join(BLOCKED_URL_PATTERNS), re.IGNORECASE)

# Dead-page signatures (multilingual)
DEAD_TEXT_SIGNATURES = [
    # English
    "this content isn't available right now",
    "this content is no longer available",
    "the link you followed may have expired",
    "the page you're looking for isn't available",
    "the page may have been removed",
    "this page isn't available",
    "sorry, this page isn't available",
    "this post is no longer available",
    "content not found",
    "page not found",
    "video unavailable",
    "this reel isn't available",
    "this video isn't available",
    "this story isn't available",
    "account not available",
    "profile unavailable",
    "this account has been disabled",
    "this profile is not available",
    # Bengali / Bangla
    "এই কন্টেন্টটি এখন উপলভ্য নয়",
    "এই পৃষ্ঠাটি উপলব্ধ নয়",
    "লিঙ্কটির মেয়াদ শেষ হয়ে থাকতে পারে",
    "পৃষ্ঠাটি হয়তো সরিয়ে নেওয়া হয়েছে",
    "কন্টেন্ট পাওয়া যায়নি",
    # Arabic
    "هذا المحتوى غير متوفر الآن",
    "الصفحة غير متوفرة",
    "ربما انتهت صلاحية الرابط",
    # Hindi
    "यह सामग्री अभी उपलब्ध नहीं है",
    "यह पृष्ठ उपलब्ध नहीं है",
    # Indonesian / Malay
    "konten ini sekarang tidak tersedia",
    "halaman ini tidak tersedia",
    # Spanish
    "este contenido no está disponible ahora mismo",
    "esta página no está disponible",
    # French
    "ce contenu n'est pas disponible actuellement",
    "cette page n'est pas disponible",
    # Portuguese
    "este conteúdo não está disponível agora",
    "esta página não está disponível",
    # Turkish
    "bu içerik şu an kullanılamıyor",
    # General
    "isn't available",
    "not available",
    "removed",
    "no longer exists",
]

DEAD_TITLE_SIGNATURES = [
    "page not found",
    "content not found",
    "error",
    "not found",
    "isn't available",
    "not available",
]

# Selectors that indicate a genuine Facebook error page
DEAD_SELECTORS = [
    "#m_error_page",
    "[data-pagelet='Error']",
    "[data-pagelet='PageNotFound']",
    ".uiInterstitialContent",
]

# Login / checkpoint redirects — if we land here, treat as indeterminate (not DEAD)
LOGIN_URL_PATTERNS = [
    r"facebook\.com/login",
    r"facebook\.com/checkpoint",
    r"facebook\.com/recover",
]
_LOGIN_RE = re.compile("|".join(LOGIN_URL_PATTERNS), re.IGNORECASE)

# Redirect to facebook.com home / mbasic home with no og content = no-info
HOME_URL_PATTERNS = [
    r"^https?://(www\.|m\.|mbasic\.)?facebook\.com/?(\?.*)?$",
]
_HOME_RE = re.compile("|".join(HOME_URL_PATTERNS), re.IGNORECASE)


# Data classes
@dataclass
class CheckResult:
    url: str
    status: str          # "ACTIVE" | "DEAD" | "ERROR"
    name: str = ""       # og:title or page title
    reason: str = ""     # short description of why
    http_status: Optional[int] = None
    final_url: str = ""


# URL normalisation helpers
def normalise_facebook_url(raw_url: str) -> str:
    """Ensure the URL is a proper facebook.com URL."""
    raw_url = raw_url.strip()
    if not raw_url.startswith("http"):
        raw_url = "https://" + raw_url
    # Replace mbasic / m. with www. for full DOM content
    raw_url = re.sub(r"https?://(mbasic\.|m\.)?facebook\.com", "https://www.facebook.com", raw_url)
    return raw_url


def is_facebook_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return "facebook.com" in parsed.netloc
    except Exception:
        return False


# Core checker function
async def check_facebook_link(url: str) -> CheckResult:
    """
    Load the Facebook URL in a headless Chromium browser and return a CheckResult.
    Uses resource interception for speed; robust to transient network glitches.
    """
    url = normalise_facebook_url(url)
    proxy_config: Optional[dict] = None
    if PROXY_URL:
        proxy_config = {"server": PROXY_URL}

    browser: Optional[Browser] = None
    context: Optional[BrowserContext] = None

    try:
        async with async_playwright() as pw:
            launch_kwargs = dict(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-accelerated-2d-canvas",
                    "--no-first-run",
                    "--no-zygote",
                    "--disable-gpu",
                    "--disable-background-networking",
                    "--disable-default-apps",
                    "--disable-extensions",
                    "--disable-sync",
                    "--disable-translate",
                    "--hide-scrollbars",
                    "--metrics-recording-only",
                    "--mute-audio",
                    "--safebrowsing-disable-auto-update",
                    "--js-flags=--max-old-space-size=256",
                    "--single-process",
                ],
            )
            if proxy_config:
                launch_kwargs["proxy"] = proxy_config

            browser = await pw.chromium.launch(**launch_kwargs)

            context_kwargs = dict(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.6367.208 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
                locale="en-US",
                timezone_id="America/New_York",
                java_script_enabled=True,
                bypass_csp=True,
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": '"Windows"',
                },
            )
            context = await browser.new_context(**context_kwargs)

            # Stealth: remove webdriver footprint
            await context.add_init_script(
                """
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
                Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
                window.chrome = { runtime: {} };
                """
            )

            page: Page = await context.new_page()

            # Resource blocking
            async def _block_resources(route, request):
                if request.resource_type in BLOCKED_RESOURCE_TYPES:
                    await route.abort()
                elif _BLOCKED_RE.search(request.url):
                    await route.abort()
                else:
                    await route.continue_()

            await page.route("**/*", _block_resources)

            # Navigation
            http_status: Optional[int] = None
            final_url: str = url
            try:
                response = await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=BROWSER_TIMEOUT,
                )
                if response:
                    http_status = response.status
                    final_url = page.url
            except PWTimeout:
                logger.warning("Timeout loading %s — keeping ACTIVE (anti-glitch)", url)
                return CheckResult(url=url, status="ERROR", reason="timeout", final_url=url)
            except PWError as exc:
                if "net::" in str(exc):
                    logger.warning("Network error loading %s — keeping ACTIVE (anti-glitch)", url)
                    return CheckResult(url=url, status="ERROR", reason=f"network: {exc}", final_url=url)
                raise

            # Give JS a moment to render any error containers
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass  # Non-fatal

            # HTTP status checks
            if http_status in (404, 410):
                return CheckResult(
                    url=url, status="DEAD",
                    reason=f"HTTP {http_status}",
                    http_status=http_status,
                    final_url=final_url,
                )

            # Login / checkpoint redirect
            if _LOGIN_RE.search(final_url):
                logger.info("Login wall detected for %s — treating as ACTIVE", url)
                return CheckResult(
                    url=url, status="ACTIVE",
                    reason="login wall (indeterminate)",
                    http_status=http_status,
                    final_url=final_url,
                    name="",
                )

            # Home redirect with no OG content
            if _HOME_RE.match(final_url):
                og_title = await _get_og_meta(page, "og:title")
                og_desc  = await _get_og_meta(page, "og:description")
                if not og_title and not og_desc:
                    return CheckResult(
                        url=url, status="DEAD",
                        reason="redirected to FB home with no OG content",
                        http_status=http_status,
                        final_url=final_url,
                    )

            # Dead-selector checks
            for sel in DEAD_SELECTORS:
                try:
                    el = await page.query_selector(sel)
                    if el:
                        return CheckResult(
                            url=url, status="DEAD",
                            reason=f"Dead selector found: {sel}",
                            http_status=http_status,
                            final_url=final_url,
                        )
                except Exception:
                    pass

            # Title & body text dead-signature scan
            try:
                page_title = (await page.title()).strip().lower()
            except Exception:
                page_title = ""

            for sig in DEAD_TITLE_SIGNATURES:
                if sig in page_title:
                    return CheckResult(
                        url=url, status="DEAD",
                        reason=f"Dead title signature: '{sig}'",
                        http_status=http_status,
                        final_url=final_url,
                    )

            try:
                body_text = await page.evaluate("() => document.body ? document.body.innerText : ''")
                body_text_lower = body_text.lower() if body_text else ""
            except Exception:
                body_text_lower = ""

            for sig in DEAD_TEXT_SIGNATURES:
                if sig in body_text_lower:
                    return CheckResult(
                        url=url, status="DEAD",
                        reason=f"Dead body signature: '{sig}'",
                        http_status=http_status,
                        final_url=final_url,
                    )

            # Gather OG title for the name field
            og_title = await _get_og_meta(page, "og:title")
            display_name = og_title or page_title.title() or ""

            return CheckResult(
                url=url, status="ACTIVE",
                name=display_name,
                reason="all checks passed",
                http_status=http_status,
                final_url=final_url,
            )

    except Exception as exc:
        logger.exception("Unexpected error checking %s: %s", url, exc)
        return CheckResult(
            url=url, status="ERROR",
            reason=f"unexpected: {exc}",
            final_url=url,
        )
    finally:
        if context:
            try:
                await context.close()
            except Exception:
                pass
        if browser:
            try:
                await browser.close()
            except Exception:
                pass


async def _get_og_meta(page: Page, property_name: str) -> str:
    """Extract an OpenGraph meta tag value."""
    try:
        val = await page.evaluate(
            f"""
            () => {{
                const el = document.querySelector('meta[property="{property_name}"]');
                return el ? el.getAttribute('content') : '';
            }}
            """
        )
        return (val or "").strip()
    except Exception:
        return ""
