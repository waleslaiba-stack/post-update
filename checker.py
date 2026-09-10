"""
Facebook Universal Link Accessibility Checker (High Precision 2-Pass Engine).
Supports:
- Share redirect links (/share/p/, /share/v/, /share/r/)
- Direct Profiles (profile.php?id=... and facebook.com/username)
- Posts, Reels, Videos, Groups, Photos
- Dual Engine: Desktop Unshorten Check + Mobile HTML Fallback
"""
import re
import random
import hashlib
import logging
import asyncio
from typing import Optional, Tuple
from dataclasses import dataclass
from urllib.parse import urlparse, parse_qs
import aiohttp
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 13; SM-G981B) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.6367.82 Mobile Safari/537.36"
)

# Text phrases explicitly rendered ONLY when Facebook content is deleted/inaccessible
DEFINITE_DEAD_MARKERS = [
    "this content isn't available right now",
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
    """Standardizes input URL."""
    url = raw_url.strip().rstrip(",.;!$*")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    elif url.startswith("http://"):
        url = "https://" + url[7:]
    return url

def extract_universal_fb_id(url: str) -> str:
    """Extracts identifier from profiles, posts, shares, reels, or queries."""
    clean_url = url.strip()
    parsed = urlparse(clean_url)
    qs = parse_qs(parsed.query)

    # 1. Query parameters
    for param in ("id", "story_fbid", "fbid", "v"):
        if param in qs and qs[param]:
            return str(qs[param][0]).strip()

    # 2. Modern Share links (/share/p/ID, /share/v/ID, /share/r/ID)
    share_match = re.search(r"/share/[pvr]/([a-zA-Z0-9_-]+)", clean_url)
    if share_match:
        return share_match.group(1)

    # 3. Standard paths
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

    # 4. Profile / Page Username path
    path_parts = [p for p in parsed.path.strip("/").split("/") if p and p not in ("pages", "profile.php", "share")]
    if path_parts:
        candidate = path_parts[0]
        if re.match(r"^[0-9a-zA-Z._-]+$", candidate) and len(candidate) >= 3:
            return candidate

    md5 = hashlib.md5(clean_url.encode("utf-8")).hexdigest()
    return f"FB_{md5[:8]}"

def extract_page_title(soup: BeautifulSoup, html_text: str) -> str:
    """Attempts to find the authentic title/name of the profile or post."""
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        t = og_title["content"].strip()
        if t and t.lower() not in ("facebook", "log in to facebook"):
            return re.sub(r"\s*\|\s*Facebook$", "", t, flags=re.I).strip()

    title_tag = soup.find("title")
    if title_tag and title_tag.text:
        t = title_tag.text.strip()
        t = re.sub(r"\s*\|\s*Facebook$", "", t, flags=re.I).strip()
        if t and t.lower() not in ("facebook", "log in to facebook"):
            return t

    return ""

async def evaluate_html_content(html_text: str, current_url: str) -> Tuple[bool, str, str]:
    """Inspects Facebook page for genuine active content vs removal warnings."""
    lower_html = html_text.lower()
    curr_url_lower = current_url.lower()

    # Rule 1: Check for definite removal signatures
    for marker in DEFINITE_DEAD_MARKERS:
        if marker in lower_html:
            return False, f"Dead content signature: '{marker}'", ""

    # Rule 2: Check for checkpoint barrier or error container
    if "checkpoint/block" in curr_url_lower or 'id="m_error_page"' in html_text:
        return False, "Checkpoint/Error page barrier", ""

    soup = BeautifulSoup(html_text, "html.parser")
    title = extract_page_title(soup, html_text)

    # If title itself says dead
    for marker in DEFINITE_DEAD_MARKERS:
        if marker in title.lower():
            return False, f"Dead indicator in title: '{title}'", ""

    # Rule 3: OpenGraph & Canonical Verification
    og_desc = soup.find("meta", property="og:description")
    desc = og_desc.get("content", "").strip() if og_desc else ""

    # If authentic title or description is present, it is 100% active
    if title or desc:
        final_name = title or (desc[:35] + "...")
        return True, "Authentic metadata verified", final_name

    # If redirected to generic blank root domain without content
    og_url = soup.find("meta", property="og:url")
    if og_url and og_url.get("content"):
        u = og_url["content"].strip().lower()
        if u in ("https://www.facebook.com/", "https://m.facebook.com/"):
            return False, "Redirected to root home page (Target not found)", ""

    return False, "No valid content structure detected", ""

async def check_facebook_link(
    url: str,
    session: Optional[aiohttp.ClientSession] = None,
    timeout_seconds: int = 20,
    custom_user_agent: Optional[str] = None
) -> CheckResult:
    target_url = normalize_facebook_url(url)
    uid = extract_universal_fb_id(target_url)
    should_close_session = False

    if session is None:
        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        session = aiohttp.ClientSession(timeout=timeout)
        should_close_session = True

    try:
        # ================= PASS 1: Desktop Engine (Follows Share redirects) =================
        desktop_headers = {
            "User-Agent": custom_user_agent or DESKTOP_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-Fetch-Mode": "navigate",
            "Upgrade-Insecure-Requests": "1",
        }

        try:
            async with session.get(
                target_url,
                headers=desktop_headers,
                allow_redirects=True,
                ssl=False
            ) as resp:
                status_code = resp.status
                final_url = str(resp.url)

                if status_code in (404, 410):
                    return CheckResult(
                        is_alive=False,
                        status="DEAD",
                        reason=f"HTTP Status {status_code}",
                        title=f"Deleted Content ({uid})",
                        uid=uid,
                        url=target_url,
                        status_code=status_code
                    )

                html_text = await resp.text(errors="ignore")
                is_alive, reason, title = await evaluate_html_content(html_text, final_url)

                # If confirmed alive on Desktop Pass, return immediately
                if is_alive:
                    return CheckResult(
                        is_alive=True,
                        status="ACTIVE",
                        reason="Verified via Desktop Engine",
                        title=title or f"Facebook ({uid})",
                        uid=uid,
                        url=target_url,
                        status_code=status_code
                    )
        except Exception as e:
            logger.warning(f"Pass 1 Desktop check encountered issue: {e}")

        # ================= PASS 2: Mobile Engine (Lightweight Verification) =================
        # Converts URL to mobile view which has lower bot-detection walls
        mobile_target = re.sub(r"^(https?://)(?:www\.|web\.)facebook\.com", r"\1m.facebook.com", target_url)
        mobile_headers = {
            "User-Agent": MOBILE_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,bn;q=0.8",
            "Upgrade-Insecure-Requests": "1",
        }

        try:
            async with session.get(
                mobile_target,
                headers=mobile_headers,
                allow_redirects=True,
                ssl=False
            ) as resp:
                status_code = resp.status
                final_url = str(resp.url)

                if status_code in (404, 410):
                    return CheckResult(
                        is_alive=False,
                        status="DEAD",
                        reason=f"HTTP Status {status_code}",
                        title=f"Deleted Content ({uid})",
                        uid=uid,
                        url=target_url,
                        status_code=status_code
                    )

                html_text = await resp.text(errors="ignore")
                is_alive, reason, title = await evaluate_html_content(html_text, final_url)

                if is_alive:
                    return CheckResult(
                        is_alive=True,
                        status="ACTIVE",
                        reason="Verified via Mobile Engine",
                        title=title or f"Facebook ({uid})",
                        uid=uid,
                        url=target_url,
                        status_code=status_code
                    )
                else:
                    return CheckResult(
                        is_alive=False,
                        status="DEAD",
                        reason=reason or "Content removed or inaccessible",
                        title=f"Dead Content ({uid})",
                        uid=uid,
                        url=target_url,
                        status_code=status_code
                    )

        except Exception as e:
            logger.warning(f"Pass 2 Mobile check failed: {e}")
            return CheckResult(
                is_alive=False,
                status="DEAD",
                reason="Unreachable link or network error",
                title=f"Dead Content ({uid})",
                uid=uid,
                url=target_url,
                status_code=0
            )

    finally:
        if should_close_session:
            await session.close()
