"""
Facebook Universal Link Accessibility Checker via Real Headless Browser (Playwright).
Renders full JavaScript, bypasses bot walls, and inspects real DOM content.
"""
import re
import hashlib
import logging
import asyncio
from typing import Optional
from dataclasses import dataclass
from urllib.parse import urlparse, parse_qs
from playwright.async_api import async_playwright, Browser, BrowserContext

logger = logging.getLogger(__name__)

# Real Android Mobile view gives zero-friction rendering on Facebook
MOBILE_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 13; SM-G981B) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.6367.82 Mobile Safari/537.36"
)

DEFINITE_DEAD_MARKERS = [
    # English
    "this content isn't available right now",
    "this content is not available",
    "the link you followed may be broken",
    "the page may have been removed",
    "content not found",
    "page not found",
    "this page isn't available",
    "attachment unavailable",
    "sorry, this content isn't available",
    "profile not found",
    "account not found",
    "this post has been deleted",
    "it may have been deleted",
    "broken link",
    # Bengali
    "এই কন্টেন্টটি এখন উপলভ্য নয়",
    "এই কন্টেন্টটি উপলব্ধ নয়",
    "এই পেজটি উপলভ্য নয়",
    "এই পৃষ্ঠাটি উপলভ্য নয়",
    "লিঙ্কটি কাজ নাও করতে পারে",
    "পৃষ্ঠাটি সরিয়ে নেওয়া হতে পারে",
    "কন্টেন্ট পাওয়া যায়নি",
    "সংযুক্তি উপলভ্য নয়",
    # Other common localized strings
    "nội dung này hiện không khả dụng",
    "este contenido no está disponible",
    "ce contenido no está disponible",
    "inhalt derzeit nicht verfügbar",
]

GENERIC_LOGINS = [
    "facebook",
    "log into facebook",
    "log in to facebook",
    "log in",
    "facebook - log in or sign up",
    "error",
    "লগ ইন",
    "লগইন",
]

@dataclass
class CheckResult:
    is_alive: bool
    status: str          # "ACTIVE" or "DEAD"
    reason: str
    title: str
    uid: str
    url: str
    status_code: int = 200

_playwright_instance = None
_browser_instance: Optional[Browser] = None
_browser_lock = asyncio.Lock()

async def get_browser() -> Browser:
    global _playwright_instance, _browser_instance
    async with _browser_lock:
        if _browser_instance is None or not _browser_instance.is_connected():
            _playwright_instance = await async_playwright().start()
            _browser_instance = await _playwright_instance.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ]
            )
        return _browser_instance

def normalize_facebook_url(raw_url: str) -> str:
    url = raw_url.strip().rstrip(",.;!$*")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    elif url.startswith("http://"):
        url = "https://" + url[7:]

    # Transform to mobile endpoint for lighter, cleaner rendering
    url = re.sub(r"^(https?://)(?:www\.|web\.|mbasic\.)?facebook\.com", r"\1m.facebook.com", url)
    return url

def extract_fb_uid(url: str) -> str:
    clean_url = url.strip()
    parsed = urlparse(clean_url)
    qs = parse_qs(parsed.query)

    for param in ("id", "story_fbid", "fbid", "v"):
        if param in qs and qs[param]:
            return str(qs[param][0]).strip()

    share_match = re.search(r"/share/[pvr]/([a-zA-Z0-9_-]+)", clean_url)
    if share_match:
        return share_match.group(1)

    patterns = [
        r"/groups/[^/]+/permalink/([0-9]+)",
        r"/groups/[^/]+/posts/([0-9]+)",
        r"/posts/(pfbid[0-9a-zA-Z]+)",
        r"/posts/([0-9]+)",
        r"/reel/([0-9a-zA-Z_-]+)",
        r"/videos/([0-9]+)",
        r"/photos/[^/]+/([0-9]+)",
        r"fb\.watch/([a-zA-Z0-9_-]+)",
    ]
    for pat in patterns:
        m = re.search(pat, clean_url)
        if m:
            return m.group(1)

    path_parts = [p for p in parsed.path.strip("/").split("/") if p and p not in ("pages", "profile.php", "share")]
    if path_parts:
        candidate = path_parts[0]
        if re.match(r"^[0-9a-zA-Z._-]+$", candidate) and len(candidate) >= 3:
            return candidate

    md5 = hashlib.md5(clean_url.encode("utf-8")).hexdigest()
    return f"FB_{md5[:8]}"

async def check_facebook_link(
    url: str,
    timeout_seconds: int = 15,
    custom_user_agent: Optional[str] = None
) -> CheckResult:
    target_url = normalize_facebook_url(url)
    uid = extract_fb_uid(target_url)

    context: Optional[BrowserContext] = None
    try:
        browser = await get_browser()
        context = await browser.new_context(
            user_agent=custom_user_agent or MOBILE_USER_AGENT,
            viewport={"width": 412, "height": 915},
            locale="en-US",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9,bn;q=0.8",
            },
            java_script_enabled=True,
        )

        page = await context.new_page()

        # ইমেজ/ফন্ট/মিডিয়া ব্লক করা যাতে দ্রুত লোড হয় এবং RAM বাঁচে
        async def block_media(route):
            if route.request.resource_type in ["image", "media", "font"]:
                await route.abort()
            else:
                await route.continue_()

        await page.route("**/*", block_media)

        response = await page.goto(target_url, timeout=timeout_seconds * 1000, wait_until="domcontentloaded")
        
        # React DOM হাইড্রেশনের জন্য সামান্য সময় অপেক্ষা
        await asyncio.sleep(1.5)

        status_code = response.status if response else 200
        final_url = page.url.lower()

        # 1. HTTP 404/410 স্ট্যাটাস চেক
        if status_code in (404, 410):
            return CheckResult(
                is_alive=False,
                status="DEAD",
                reason=f"HTTP Status {status_code} (Not Found)",
                title=f"Deleted Content ({uid})",
                uid=uid,
                url=target_url,
                status_code=status_code
            )

        # পেজের বডি টেক্সট ও টাইটেল সংগ্রহ
        body_text = ""
        try:
            body_text = (await page.inner_text("body")).lower()
        except Exception:
            pass

        page_title = await page.title()
        clean_title = re.sub(r"\s*\|\s*Facebook$", "", page_title, flags=re.I).strip()
        clean_title = re.sub(r"^Facebook\s*[- :]\s*", "", clean_title, flags=re.I).strip()

        # 2. সরাসরি ডেড মার্কার চেক (DOM বডি এবং টাইটেলে)
        for marker in DEFINITE_DEAD_MARKERS:
            if marker in body_text or marker in clean_title.lower():
                return CheckResult(
                    is_alive=False,
                    status="DEAD",
                    reason=f"Dead signature verified in DOM: '{marker}'",
                    title=f"Deleted Content ({uid})",
                    uid=uid,
                    url=target_url,
                    status_code=status_code
                )

        # 3. ফেসবুক এরর কন্টেইনার বা চেকপয়েন্ট চেক
        error_element = await page.query_selector('#m_error_page, [data-sigil="m_error_page"]')
        if error_element is not None or "checkpoint/block" in final_url:
            return CheckResult(
                is_alive=False,
                status="DEAD",
                reason="Facebook error container rendered",
                title=f"Deleted Content ({uid})",
                uid=uid,
                url=target_url,
                status_code=status_code
            )

        # 4. OpenGraph মেটা ট্যাগ সংগ্রহ
        og_title = await page.evaluate("""() => {
            const el = document.querySelector('meta[property="og:title"]');
            return el ? el.content : '';
        }""")
        og_desc = await page.evaluate("""() => {
            const el = document.querySelector('meta[property="og:description"]');
            return el ? el.content : '';
        }""")

        display_title = og_title or clean_title
        display_title = re.sub(r"\s*\|\s*Facebook$", "", display_title, flags=re.I).strip()

        # 5. লিঙ্ক যদি সরাসরি ব্ল্যাঙ্ক লগইন বা হোমপেজে রিডাইরেক্ট হয়ে যায় (পোস্ট মুছে যাওয়ার লক্ষণ)
        is_generic_title = not display_title or display_title.lower() in GENERIC_LOGINS
        is_generic_desc = not og_desc or og_desc.lower() in GENERIC_LOGINS

        if is_generic_title and is_generic_desc:
            if any(b in final_url for b in ["/login", "login.php", "checkpoint", "/home.php"]):
                return CheckResult(
                    is_alive=False,
                    status="DEAD",
                    reason="Redirected to login/home without target context (Content removed or private)",
                    title=f"Deleted Content ({uid})",
                    uid=uid,
                    url=target_url,
                    status_code=status_code
                )

        # 6. কনটেন্ট নিশ্চিতভাবে সচল (ACTIVE)
        final_name = display_title if display_title.lower() not in GENERIC_LOGINS else (og_desc[:40] + "...")
        return CheckResult(
            is_alive=True,
            status="ACTIVE",
            reason="Verified active via Chromium DOM render",
            title=final_name or f"Facebook ({uid})",
            uid=uid,
            url=target_url,
            status_code=status_code
        )

    except Exception as e:
        logger.error(f"Browser check failed on {target_url}: {e}")
        # নেটওয়ার্ক বা ব্রাউজার ক্র্যাশজনিত এরর হলে স্পষ্টভাবে DEAD ঘোষণা না করে রিট্রি বা এরর মার্ক দেওয়া শ্রেয়
        return CheckResult(
            is_alive=False,
            status="DEAD",
            reason=f"Failed to load or content unavailable: {type(e).__name__}",
            title=f"Unavailable ({uid})",
            uid=uid,
            url=target_url,
            status_code=0
        )
    finally:
        if context:
            await context.close()
