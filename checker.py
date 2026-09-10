"""
Facebook Universal Link Accessibility Checker (High Reliability Engine).
Strict Rule: A link is ONLY marked as DEAD if Facebook explicitly returns 404
or unambiguous removal text. Transient blocks, blank responses, or login prompts
will NEVER trigger a DEAD status.
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

FB_CRAWLER_UA = "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"

# Text phrases that Facebook renders ONLY when the post/profile is truly deleted or taken down
GENUINE_DEAD_PHRASES = [
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

def inspect_for_definite_death(html_text: str, current_url: str) -> Tuple[bool, str]:
    """
    Returns (True, reason) ONLY if there is 100% concrete proof the link is deleted.
    Otherwise returns (False, "") meaning it should be treated as ACTIVE.
    """
    lower_html = html_text.lower()
    curr_url_lower = current_url.lower()

    # Explicit removal messages inside HTML text
    for phrase in GENUINE_DEAD_PHRASES:
        if phrase in lower_html:
            return True, f"Found explicit dead phrase: '{phrase}'"

    # Redirection directly to help/checkpoint/barrier indicates account suspension/removal
    if "checkpoint/block" in curr_url_lower or "/help/contact/" in curr_url_lower:
        return True, "Redirected to Facebook account suspension/block barrier"

    soup = BeautifulSoup(html_text, "html.parser")
    title_tag = soup.find("title")
    title_text = title_tag.text.strip().lower() if title_tag and title_tag.text else ""

    for phrase in GENUINE_DEAD_PHRASES:
        if phrase in title_text:
            return True, f"Title states dead: '{title_text}'"

    return False, ""

def extract_valid_title(html_text: str, uid: str) -> str:
    soup = BeautifulSoup(html_text, "html.parser")
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        t = og_title["content"].strip()
        if t.lower() not in ("facebook", "log into facebook", "log in to facebook", ""):
            return re.sub(r"\s*\|\s*Facebook$", "", t, flags=re.I).strip()

    title_tag = soup.find("title")
    if title_tag and title_tag.text:
        t = title_tag.text.strip()
        t = re.sub(r"\s*\|\s*Facebook$", "", t, flags=re.I).strip()
        if t.lower() not in ("facebook", "log into facebook", "log in to facebook", ""):
            return t

    return f"Facebook ({uid})"

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
        "Accept-Language": "en-US,en;q=0.9,bn;q=0.8",
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

            # Strict 404 / 410 check
            if status_code in (404, 410):
                return CheckResult(
                    is_alive=False,
                    status="DEAD",
                    reason=f"HTTP {status_code} (Not Found)",
                    title=f"Deleted Content ({uid})",
                    uid=uid,
                    url=target_url,
                    status_code=status_code,
                )

            html_text = await resp.text(errors="ignore")
            is_dead, dead_reason = inspect_for_definite_death(html_text, final_url)

            if is_dead:
                return CheckResult(
                    is_alive=False,
                    status="DEAD",
                    reason=dead_reason,
                    title=f"Dead Content ({uid})",
                    uid=uid,
                    url=target_url,
                    status_code=status_code,
                )

            # If not explicitly proven dead, it is ACTIVE
            page_title = extract_valid_title(html_text, uid)
            return CheckResult(
                is_alive=True,
                status="ACTIVE",
                reason="Link verified accessible",
                title=page_title,
                uid=uid,
                url=target_url,
                status_code=status_code,
            )

    except Exception as e:
        logger.warning(f"Error checking {target_url}: {e}")
        # Network hiccups must KEEP links alive to prevent false alerts
        return CheckResult(
            is_alive=True,
            status="ACTIVE",
            reason="Transient check hesitation (Preserved ACTIVE)",
            title=f"Facebook ({uid})",
            uid=uid,
            url=target_url,
            status_code=0,
        )
    finally:
        if should_close_session:
            await session.close()
