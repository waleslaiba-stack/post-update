"""
Facebook Link Accessibility Checker (Strict Metadata & Anti-Redirect Engine).
Solves Railway/Cloud False-Active bug by strictly inspecting og:url and content headers.
"""
import re
import hashlib
import logging
import asyncio
from typing import Optional, Tuple
from dataclasses import dataclass
from urllib.parse import urlparse, parse_qs
import aiohttp
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Android mobile user agent produces clean, predictable HTML structure
CRAWLER_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 13; SM-G981B) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.6099.210 Mobile Safari/537.36"
)

# Text that Facebook renders when content does not exist
DEFINITE_DEAD_MARKERS = [
    "this content isn't available right now",
    "the link you followed may be broken",
    "the page may have been removed",
    "content not found",
    "page not found",
    "this page isn't available",
    "attachment unavailable",
    "sorry, this content isn't available",
    "এই কন্টেন্টটি এখন উপলভ্য নয়",
    "nội dung này hiện không khả dụng",
    "este contenido no está disponible",
    "ce contenu n'est pas disponible",
    "inhalt derzeit nicht verfügbar"
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
    """
    Cleans raw user inputs: handles cases like 'facebook.com/username'
    and removes desktop clutter.
    """
    url = raw_url.strip().rstrip(",.;!$*")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    elif url.startswith("http://"):
        url = "https://" + url[7:]

    # Switch to m.facebook.com for predictable parsing
    url = re.sub(r"^(https?://)(?:www\.|web\.|mbasic\.)?facebook\.com", r"\1m.facebook.com", url)
    return url

def extract_fb_uid(url: str) -> str:
    clean_url = url.strip()
    parsed = urlparse(clean_url)
    qs = parse_qs(parsed.query)

    for param in ("story_fbid", "fbid", "v", "id"):
        if param in qs and qs[param]:
            val = qs[param][0].strip()
            if val and val.isdigit():
                return val

    patterns = [
        r"/posts/(pfbid[0-9a-zA-Z]+)",
        r"/posts/([0-9]+)",
        r"/permalink/([0-9]+)",
        r"/reel/([0-9]+)",
        r"/videos/([0-9]+)",
        r"/photos/[^/]+/([0-9]+)",
        r"/groups/[^/]+/permalink/([0-9]+)",
        r"/groups/[^/]+/posts/([0-9]+)",
        r"fb\.watch/([a-zA-Z0-9_-]+)",
    ]
    for pat in patterns:
        m = re.search(pat, clean_url)
        if m:
            return m.group(1)

    path_parts = [p for p in parsed.path.strip("/").split("/") if p and p not in ("pages", "profile.php")]
    if path_parts:
        candidate = path_parts[-1]
        if re.match(r"^[0-9a-zA-Z._-]+$", candidate) and len(candidate) >= 3:
            return candidate

    md5 = hashlib.md5(clean_url.encode("utf-8")).hexdigest()
    return f"FB_{md5[:10]}"

def analyze_facebook_html(html_text: str, current_url: str, initial_url: str) -> Tuple[bool, str, str]:
    uid = extract_fb_uid(initial_url)
    lower_html = html_text.lower()
    current_url_lower = current_url.lower()

    # Rule 1: Immediate Dead Markers in body text
    for marker in DEFINITE_DEAD_MARKERS:
        if marker in lower_html:
            return False, f"Dead content signature detected: '{marker}'", f"Removed Post ({uid})"

    # Rule 2: Redirected to generic Login / Checkpoint without landing on target
    if "checkpoint" in current_url_lower or "/help/" in current_url_lower:
        return False, "Redirected to Facebook barrier/help (Link does not exist)", f"Dead Content ({uid})"

    soup = BeautifulSoup(html_text, "html.parser")

    # Rule 3: Inspect Page Title
    page_title = ""
    og_title_tag = soup.find("meta", property="og:title")
    if og_title_tag and og_title_tag.get("content"):
        page_title = og_title_tag["content"].strip()
    if not page_title:
        title_tag = soup.find("title")
        if title_tag and title_tag.text:
            page_title = title_tag.text.strip()

    clean_title = re.sub(r"\s*\|\s*Facebook$", "", page_title, flags=re.I).strip()
    clean_title = re.sub(r"^Facebook\s*[- :]\s*", "", clean_title, flags=re.I).strip()

    for marker in DEFINITE_DEAD_MARKERS:
        if marker in clean_title.lower():
            return False, f"Dead marker in page title: '{clean_title}'", f"Removed Post ({uid})"

    # Rule 4: Metadata Authenticity Check
    # Active Facebook posts have specific OpenGraph tags: og:url, og:description, or canonical link
    og_url_tag = soup.find("meta", property="og:url")
    og_url = og_url_tag["content"].strip().lower() if og_url_tag and og_url_tag.get("content") else ""

    og_desc_tag = soup.find("meta", property="og:description")
    og_desc = og_desc_tag["content"].strip() if og_desc_tag and og_desc_tag.get("content") else ""

    # If redirected to generic Facebook root domain, the link is dead
    if og_url in ("https://www.facebook.com/", "https://m.facebook.com/", "https://facebook.com/"):
        return False, "Redirected to home domain (Post no longer exists)", f"Dead Content ({uid})"

    # If the title is just a blank login screen without specific post description, it is dead
    generic_titles = ["log in to facebook", "log into facebook", "facebook", "error", "welcome to facebook"]
    if clean_title.lower() in generic_titles:
        if not og_desc or og_desc.lower() in generic_titles:
            return False, "Login wall with no valid post context (Post deleted or private)", f"Dead Content ({uid})"

    # Rule 5: Error container checks
    if 'id="m_error_page"' in html_text or 'data-sigil="m_error_page"' in html_text:
        return False, "Facebook error page container detected", f"Dead Content ({uid})"

    final_name = clean_title or og_desc[:30] or f"Facebook Post ({uid})"
    return True, "Valid Facebook content detected", final_name

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
        "User-Agent": custom_user_agent or CRAWLER_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Fetch-Mode": "navigate",
        "Upgrade-Insecure-Requests": "1",
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

            # Strict 404 / 410 check
            if status_code in (404, 410):
                return CheckResult(
                    is_alive=False,
                    status="DEAD",
                    reason=f"HTTP status code {status_code} (Not Found)",
                    title=f"Deleted Post ({uid})",
                    uid=uid,
                    url=target_url,
                    status_code=status_code,
                )

            html_text = await resp.text(errors="ignore")
            is_alive, reason, title = analyze_facebook_html(html_text, final_url, target_url)

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
        logger.warning(f"Error checking link {url}: {e}")
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
