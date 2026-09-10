"""
Facebook Universal Link Health Engine (Strict Positive-Validation).
Eliminates the 'All Active' bug by requiring authentic metadata (og:title, og:desc, or App Links).
Dead/deleted posts that return blank or login-only shells are strictly marked as DEAD.
"""
import re
import hashlib
import logging
from typing import Optional, Tuple
from dataclasses import dataclass
from urllib.parse import urlparse, parse_qs
import aiohttp
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Android mobile user-agent gives cleaner OpenGraph metadata without complex scripts
CRAWLER_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 13; SM-S908B) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Mobile Safari/537.36"
)

# Text phrases explicitly rendered when content is removed or link broken
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

# Generic text shown ONLY when Facebook blocks or when content does NOT exist
GENERIC_LOGIN_TITLES = [
    "facebook",
    "log into facebook",
    "log in to facebook",
    "log in",
    "facebook – log in or sign up",
    "facebook - log in or sign up",
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

    # Switch to mobile endpoint which serves cleaner meta-tags
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

def parse_html_content(html_text: str, current_url: str, uid: str) -> Tuple[bool, str, str]:
    lower_html = html_text.lower()
    curr_url_lower = current_url.lower()

    # 1. Check for explicit dead phrases in HTML body
    for marker in DEFINITE_DEAD_MARKERS:
        if marker in lower_html:
            return False, f"Dead content signature: '{marker}'", f"Dead Content ({uid})"

    # 2. Check for checkpoint or account suspension
    if "checkpoint/block" in curr_url_lower or 'id="m_error_page"' in html_text or "/help/contact/" in curr_url_lower:
        return False, "Checkpoint/Security barrier (Link inaccessible)", f"Dead Content ({uid})"

    soup = BeautifulSoup(html_text, "html.parser")

    # 3. Extract OpenGraph and Title
    og_title_tag = soup.find("meta", property="og:title")
    og_title = og_title_tag["content"].strip() if og_title_tag and og_title_tag.get("content") else ""

    page_title_tag = soup.find("title")
    page_title = page_title_tag.text.strip() if page_title_tag and page_title_tag.text else ""

    clean_title = og_title or page_title
    clean_title = re.sub(r"\s*\|\s*Facebook$", "", clean_title, flags=re.I).strip()
    clean_title = re.sub(r"^Facebook\s*[- :]\s*", "", clean_title, flags=re.I).strip()

    # If title itself has removal words
    for marker in DEFINITE_DEAD_MARKERS:
        if marker in clean_title.lower():
            return False, f"Title indicates removal: '{clean_title}'", f"Dead Content ({uid})"

    og_desc_tag = soup.find("meta", property="og:description")
    og_desc = og_desc_tag["content"].strip() if og_desc_tag and og_desc_tag.get("content") else ""

    # Check AppLink tags (Active Facebook posts/profiles always link to mobile intent)
    app_link = soup.find("meta", property="al:android:url") or soup.find("meta", property="al:ios:url")

    # STRICT POSITIVE VALIDATION:
    # Does this page actually have real content?
    has_authentic_title = bool(clean_title and clean_title.lower() not in GENERIC_LOGIN_TITLES)
    has_authentic_desc = bool(og_desc and og_desc.lower() not in GENERIC_LOGIN_TITLES)
    has_valid_app_link = bool(app_link and app_link.get("content"))

    if has_authentic_title:
        return True, "Authentic title verified", clean_title

    if has_authentic_desc:
        return True, "Authentic description verified", og_desc[:35] + "..."

    if has_valid_app_link:
        return True, "App link target verified", f"Facebook Content ({uid})"

    # If it is just a generic Facebook shell with no actual content, it is DEAD!
    return False, "No authentic post/profile data found (Content deleted)", f"Dead Content ({uid})"

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
        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        session = aiohttp.ClientSession(timeout=timeout)
        should_close_session = True

    headers = {
        "User-Agent": custom_user_agent or CRAWLER_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,bn;q=0.8",
        "Sec-Fetch-Mode": "navigate",
        "Upgrade-Insecure-Requests": "1",
    }

    try:
        async with session.get(
            target_url,
            headers=headers,
            proxy=proxy_url or None,
            allow_redirects=True,
            ssl=False
        ) as resp:
            status_code = resp.status
            final_url = str(resp.url)

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

            html_text = await resp.text(errors="ignore")
            is_alive, reason, title = parse_html_content(html_text, final_url, uid)

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
        logger.warning(f"Error checking {target_url}: {e}")
        # When connection drops out completely, keep status alive to prevent false alerts
        return CheckResult(
            is_alive=True,
            status="ACTIVE",
            reason="Temporary connection delay (Preserved ACTIVE)",
            title=f"Facebook ({uid})",
            uid=uid,
            url=target_url,
            status_code=0,
        )
    finally:
        if should_close_session:
            await session.close()
