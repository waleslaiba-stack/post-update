"""
Facebook Universal Link Accessibility Checker (Crawler Preview Engine).
Uses official Facebook external crawler identity to eliminate login redirects
and strictly validates real post/profile content vs dead/deleted links.
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

# Official Facebook Crawler UA: bypasses human login walls and receives pure preview metadata
FB_CRAWLER_UA = "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"

# Text explicitly meaning the content is GONE
DEAD_PHRASES = [
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
    "এই কন্টেন্টটি এখন উপলভ্য নয়",
    "এই পেজটি উপলভ্য নয়",
    "nội dung này hiện không khả dụng",
    "este contenido no está disponible",
    "ce contenido no está disponible",
    "inhalt derzeit nicht verfügbar",
    "content unavailable",
]

# Generic text that Facebook shows when a page is dead or blocked
GENERIC_LOGIN_PHRASES = [
    "log into facebook",
    "log in to facebook",
    "log in",
    "facebook - log in or sign up",
    "connect with friends and the world around you",
    "start sharing and connecting",
    "facebook",
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
        r"/posts/(pfbid[0-9a-zA-Z]+)",
        r"/posts/([0-9]+)",
        r"/reel/([0-9a-zA-Z_-]+)",
        r"/videos/([0-9]+)",
        r"/photos/[^/]+/([0-9]+)",
        r"/groups/[^/]+/permalink/([0-9]+)",
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

def is_generic_or_empty(text: str) -> bool:
    if not text:
        return True
    t_clean = text.lower().strip()
    for g in GENERIC_LOGIN_PHRASES:
        if g in t_clean:
            return True
    return False

def parse_html_response(html_text: str, final_url: str, uid: str) -> Tuple[bool, str, str]:
    lower_html = html_text.lower()

    # 1. Explicit dead string check in the HTML body
    for phrase in DEAD_PHRASES:
        if phrase in lower_html:
            return False, f"Dead marker: '{phrase}'", f"Removed Content ({uid})"

    # 2. Redirected to help or checkpoint
    if "/help/" in final_url.lower() or "checkpoint" in final_url.lower():
        return False, "Checkpoint/Help redirect", f"Dead Content ({uid})"

    soup = BeautifulSoup(html_text, "html.parser")

    # 3. Extract OpenGraph and Title
    og_title_tag = soup.find("meta", property="og:title")
    og_title = og_title_tag["content"].strip() if og_title_tag and og_title_tag.get("content") else ""

    og_desc_tag = soup.find("meta", property="og:description")
    og_desc = og_desc_tag["content"].strip() if og_desc_tag and og_desc_tag.get("content") else ""

    page_title_tag = soup.find("title")
    page_title = page_title_tag.text.strip() if page_title_tag and page_title_tag.text else ""

    # Clean Facebook brand suffixes
    clean_title = re.sub(r"\s*\|\s*Facebook$", "", og_title or page_title, flags=re.I).strip()
    clean_title = re.sub(r"^Facebook\s*[- :]\s*", "", clean_title, flags=re.I).strip()

    # 4. Check if title explicitly contains dead phrases
    for phrase in DEAD_PHRASES:
        if phrase in clean_title.lower():
            return False, f"Dead notice in title: '{clean_title}'", f"Removed Content ({uid})"

    # 5. Strict Active Validation:
    # If the title or description is generic Facebook login text, it means Facebook did NOT find the content!
    has_real_title = not is_generic_or_empty(clean_title)
    has_real_desc = not is_generic_or_empty(og_desc)

    if has_real_title:
        return True, "Authentic title verified", clean_title

    if has_real_desc:
        return True, "Authentic description verified", og_desc[:40] + "..."

    # If neither real title nor real description exists, content is DEAD
    return False, "No authentic post/profile metadata found", f"Dead Content ({uid})"

async def check_facebook_link(
    url: str,
    session: Optional[aiohttp.ClientSession] = None,
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
        "User-Agent": FB_CRAWLER_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Fetch-Mode": "navigate",
    }

    try:
        async with session.get(
            target_url,
            headers=headers,
            allow_redirects=True,
            ssl=False
        ) as resp:
            status_code = resp.status
            final_url = str(resp.url)

            # 404 / 410 is definitely DEAD
            if status_code in (404, 410):
                return CheckResult(
                    is_alive=False,
                    status="DEAD",
                    reason=f"HTTP Status {status_code}",
                    title=f"Deleted Content ({uid})",
                    uid=uid,
                    url=target_url,
                    status_code=status_code,
                )

            html_text = await resp.text(errors="ignore")
            is_alive, reason, title = parse_html_response(html_text, final_url, uid)

            return CheckResult(
                is_alive=is_alive,
                status="ACTIVE" if is_alive else "DEAD",
                reason=reason,
                title=title,
                uid=uid,
                url=target_url,
                status_code=status_code,
            )

    except Exception as e:
        logger.warning(f"Error checking link {target_url}: {e}")
        return CheckResult(
            is_alive=False,
            status="DEAD",
            reason=f"Connection failure: {type(e).__name__}",
            title=f"Dead Link ({uid})",
            uid=uid,
            url=target_url,
            status_code=0,
        )
    finally:
        if should_close_session:
            await session.close()
