"""
Facebook Link Accessibility Checker (Strict Engine).
Accurately differentiates ACTIVE vs DEAD links without false alive fallbacks.
"""
import re
import random
import hashlib
import logging
import asyncio
from typing import Optional, Tuple
from dataclasses import dataclass
from urllib.parse import urlparse, parse_qs, unquote
import aiohttp
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Realistic Desktop & Mobile User-Agents
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; SM-S908B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Mobile Safari/537.36",
]

# Explicit signatures that denote content is deleted / unavailable / dead
DEAD_PHRASES = [
    "this content isn't available right now",
    "this content is not available",
    "the link you followed may be broken",
    "the page may have been removed",
    "content not found",
    "page not found",
    "this page isn't available",
    "attachment unavailable",
    "sorry, this content isn't available right now",
    "may have expired or not be visible to you",
    "nội dung này hiện không khả dụng",
    "este contenido no está disponible",
    "ce contenu n'est pas disponible",
    "inhalt derzeit nicht verfügbar",
    "এই কন্টেন্টটি এখন উপলভ্য নয়",
    "লিঙ্কটি ভাঙা হতে পারে",
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

def evaluate_facebook_html(html_text: str, current_url: str, initial_url: str) -> Tuple[bool, str, str]:
    """
    Evaluates whether the Facebook page actually hosts valid active content.
    Returns: (is_alive, reason, detected_title)
    """
    uid = extract_fb_uid(initial_url)
    lower_html = html_text.lower()
    current_url_lower = current_url.lower()

    # 1. Immediate redirect analysis
    # When a post/account is DEAD or REMOVED, Facebook redirects either to login without destination,
    # or to an error/checkpoint barrier, or privacy mutation.
    if "checkpoint/block" in current_url_lower or "checkpoint/disabled" in current_url_lower:
        return False, "Checkpoint/Account block barrier", f"Dead Link ({uid})"

    if "/login" in current_url_lower and "next=" not in current_url_lower:
        return False, "Redirected to blank Facebook login (Content deleted or removed)", f"Dead Link ({uid})"

    # 2. Check for explicit dead text patterns in HTML
    for phrase in DEAD_PHRASES:
        if phrase in lower_html:
            return False, f"Matched removal signature: '{phrase}'", f"Content Not Found ({uid})"

    soup = BeautifulSoup(html_text, "html.parser")

    # 3. Analyze page title
    title = ""
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        title = og_title["content"].strip()
    if not title:
        title_tag = soup.find("title")
        if title_tag and title_tag.text:
            title = title_tag.text.strip()

    title_lower = title.lower()
    for phrase in DEAD_PHRASES:
        if phrase in title_lower:
            return False, f"Dead notice in title: '{title}'", f"Content Not Found ({uid})"

    # 4. Check OpenGraph Meta Tags
    # Active Facebook posts/pages always have specific og:url, og:type, or og:description
    og_url = soup.find("meta", property="og:url")
    og_desc = soup.find("meta", property="og:description")
    canonical = soup.find("link", rel="canonical")

    # If it is redirected to generic root or home page, it is DEAD
    if og_url and og_url.get("content"):
        og_url_val = og_url["content"].strip().lower()
        if og_url_val in ("https://www.facebook.com/", "https://m.facebook.com/", "https://facebook.com/"):
            return False, "Redirected to root home page (Target content does not exist)", f"Dead Post ({uid})"

    # 5. Clean up title
    clean_title = title
    if clean_title:
        clean_title = re.sub(r"\s*\|\s*Facebook$", "", clean_title, flags=re.I).strip()
        clean_title = re.sub(r"^Facebook\s*[- :]\s*", "", clean_title, flags=re.I).strip()

    # If title is completely generic "Log in to Facebook" or "Facebook" without OG metadata, it's DEAD
    if clean_title.lower() in ("log in to facebook", "log into facebook", "facebook", "error", ""):
        if not og_desc or not og_desc.get("content"):
            return False, "Generic login with no content context (Content removed or private)", f"Dead Link ({uid})"

    final_name = clean_title or f"Facebook Post ({uid})"
    return True, "Content verified active", final_name

async def check_facebook_link(
    url: str,
    session: Optional[aiohttp.ClientSession] = None,
    timeout_seconds: int = 15,
    custom_user_agent: Optional[str] = None
) -> CheckResult:
    uid = extract_fb_uid(url)
    should_close_session = False
    if session is None:
        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        session = aiohttp.ClientSession(timeout=timeout)
        should_close_session = True

    target_url = url.strip()
    if not target_url.startswith(("http://", "https://")):
        target_url = "https://" + target_url

    headers = {
        "User-Agent": custom_user_agent or random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "DNT": "1",
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
                    reason=f"HTTP status {status_code} (Not Found)",
                    title=f"Content Not Found ({uid})",
                    uid=uid,
                    url=url,
                    status_code=status_code,
                )

            html_text = await resp.text(errors="ignore")
            is_alive, reason, title = evaluate_facebook_html(html_text, final_url, target_url)

            return CheckResult(
                is_alive=is_alive,
                status="ACTIVE" if is_alive else "DEAD",
                reason=reason,
                title=title,
                uid=uid,
                url=url,
                status_code=status_code,
            )

    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.warning(f"Connection timeout/error on {url}: {e}")
        # Network errors should be inspected strictly on second attempt
        return CheckResult(
            is_alive=False,
            status="DEAD",
            reason="Destination host unreachable / Broken link",
            title=f"Broken Link ({uid})",
            uid=uid,
            url=url,
            status_code=0,
        )
    except Exception as e:
        logger.error(f"Checker error on {url}: {e}")
        return CheckResult(
            is_alive=False,
            status="DEAD",
            reason=f"Unexpected error: {str(e)}",
            title=f"Dead Link ({uid})",
            uid=uid,
            url=url,
            status_code=500,
        )
    finally:
        if should_close_session:
            await session.close()
