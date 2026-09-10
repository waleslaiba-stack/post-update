"""
Facebook Universal Link Accessibility Checker (High-Stability Engine).
Features:
- Dual-Pass Inspection: Proxy Primary + Direct Fail-safe
- Accurate for Groups, Profiles, Posts, Reels, and Share Redirects
- Zero Hanging / Instant Status Feedback
"""
import os
import re
import hashlib
import logging
from typing import Optional, Tuple
from dataclasses import dataclass
from urllib.parse import urlparse, parse_qs
import aiohttp
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Standard Real-Device User-Agent to avoid scraping traps
REAL_DEVICE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

DEFINITE_DEAD_MARKERS = [
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
    "এই কন্টেন্টটি এখন উপলভ্য নয়",
    "এই পেজটি উপলভ্য নয়",
    "nội dung này hiện không khả dụng",
    "este contenido no está disponible",
    "ce contenido no está disponible",
    "inhalt derzeit nicht verfügbar",
]

GENERIC_TRASH = [
    "facebook",
    "log into facebook",
    "log in to facebook",
    "log in",
    "facebook - log in or sign up",
    "facebook – log in or sign up",
    "error",
    "",
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

def normalize_facebook_url(raw_url: str) -> str:
    url = raw_url.strip().rstrip(",.;!$*")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    elif url.startswith("http://"):
        url = "https://" + url[7:]
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

def inspect_html_health(html_text: str, final_url: str, uid: str) -> Tuple[bool, str, str]:
    lower_html = html_text.lower()
    curr_url_lower = final_url.lower()

    # Explicit dead signatures
    for marker in DEFINITE_DEAD_MARKERS:
        if marker in lower_html:
            return False, f"Dead content signature: '{marker}'", f"Dead Content ({uid})"

    # Checkpoint / Barrier detection
    if "checkpoint/block" in curr_url_lower or "/help/contact/" in curr_url_lower:
        return False, "Checkpoint/Help barrier redirect", f"Dead Content ({uid})"

    soup = BeautifulSoup(html_text, "html.parser")

    og_title_tag = soup.find("meta", property="og:title")
    og_title = og_title_tag["content"].strip() if og_title_tag and og_title_tag.get("content") else ""

    page_title_tag = soup.find("title")
    page_title = page_title_tag.text.strip() if page_title_tag and page_title_tag.text else ""

    clean_title = og_title or page_title
    clean_title = re.sub(r"\s*\|\s*Facebook$", "", clean_title, flags=re.I).strip()
    clean_title = re.sub(r"^Facebook\s*[- :]\s*", "", clean_title, flags=re.I).strip()

    for marker in DEFINITE_DEAD_MARKERS:
        if marker in clean_title.lower():
            return False, f"Title indicates removal: '{clean_title}'", f"Dead Content ({uid})"

    og_desc_tag = soup.find("meta", property="og:description")
    og_desc = og_desc_tag["content"].strip() if og_desc_tag and og_desc_tag.get("content") else ""

    # Canonical redirect to root domain means content was deleted
    og_url_tag = soup.find("meta", property="og:url")
    if og_url_tag and og_url_tag.get("content"):
        u = og_url_tag["content"].strip().lower()
        if u in ("https://www.facebook.com/", "https://www.facebook.com", "https://m.facebook.com/"):
            return False, "Redirected to root home page (Target does not exist)", f"Dead Content ({uid})"

    has_real_title = bool(clean_title and clean_title.lower() not in GENERIC_TRASH)
    has_real_desc = bool(og_desc and og_desc.lower() not in GENERIC_TRASH)

    if has_real_title:
        return True, "Authentic title verified", clean_title

    if has_real_desc:
        return True, "Authentic description verified", og_desc[:35] + "..."

    # If only login text is returned and no target details exist, the post is DEAD
    if any(b in curr_url_lower for b in ["/login", "login.php"]):
        return False, "Redirected to login with no post context (Content removed)", f"Dead Content ({uid})"

    return False, "No authentic post or profile metadata found (Content removed)", f"Dead Content ({uid})"

async def _fetch_request(url: str, session: aiohttp.ClientSession, proxy: Optional[str] = None) -> Tuple[int, str, str]:
    headers = {
        "User-Agent": REAL_DEVICE_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,bn;q=0.8",
        "Sec-Fetch-Mode": "navigate",
        "Upgrade-Insecure-Requests": "1",
    }
    async with session.get(
        url,
        headers=headers,
        proxy=proxy,
        allow_redirects=True,
        timeout=aiohttp.ClientTimeout(total=12),
        ssl=False
    ) as resp:
        html = await resp.text(errors="ignore")
        return resp.status, str(resp.url), html

async def check_facebook_link(
    url: str,
    session: Optional[aiohttp.ClientSession] = None,
    proxy_url: Optional[str] = None,
    timeout_seconds: int = 15,
    custom_user_agent: Optional[str] = None
) -> CheckResult:
    target_url = normalize_facebook_url(url)
    uid = extract_fb_uid(target_url)
    should_close_session = False

    if session is None:
        session = aiohttp.ClientSession()
        should_close_session = True

    proxy = proxy_url or os.getenv("PROXY_URL", "").strip() or None

    try:
        # Pass 1: Try with configured Proxy
        try:
            status_code, final_url, html_text = await _fetch_request(target_url, session, proxy=proxy)
        except Exception as proxy_err:
            logger.warning(f"Proxy attempt failed on {target_url}, falling back to Direct: {proxy_err}")
            # Pass 2: Direct fallback if proxy has issues
            status_code, final_url, html_text = await _fetch_request(target_url, session, proxy=None)

        if status_code in (404, 410):
            return CheckResult(
                is_alive=False,
                status="DEAD",
                reason=f"HTTP Status {status_code} (Not Found)",
                title=f"Deleted Content ({uid})",
                uid=uid,
                url=target_url,
                status_code=status_code,
            )

        is_alive, reason, title = inspect_html_health(html_text, final_url, uid)

        return CheckResult(
            is_alive=is_alive,
            status="ACTIVE" if is_alive else "DEAD",
            reason=reason,
            title=title or f"Facebook ({uid})",
            uid=uid,
            url=target_url,
            status_code=status_code,
        )

    except Exception as e:
        logger.error(f"Checker error on {target_url}: {e}")
        # Keep alive on complete network failure to avoid false dead triggers
        return CheckResult(
            is_alive=True,
            status="ACTIVE",
            reason="Network delay (Link kept ACTIVE)",
            title=f"Facebook ({uid})",
            uid=uid,
            url=target_url,
            status_code=0,
        )
    finally:
        if should_close_session:
            await session.close()
