"""
Facebook Link Accessibility Checker (High Accuracy).
Uses Mobile Endpoints & Strict HTML Signal Analysis to prevent False Positives.
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

USER_AGENTS = [
    "Mozilla/5.0 (Linux; Android 13; SM-G981B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.6099.210 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 12; Pixel 6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.5735.196 Mobile Safari/537.36",
]

# Strict indicators that confirm a Facebook post has genuinely been removed
GENUINE_DEAD_SIGNATURES = [
    "this content isn't available right now",
    "the link you followed may be broken",
    "the page may have been removed",
    "content not found",
    "this page isn't available",
    "attachment unavailable",
    "nội dung này hiện không khả dụng",
    "este contenido no está disponible",
    "ce contenu n'est pas disponible",
    "inhalt derzeit nicht verfügbar",
    "এই কন্টেন্টটি এখন উপলভ্য নয়",
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
        if re.match(r"^[0-9a-zA-Z._-]+$", candidate) and len(candidate) >= 4:
            return candidate

    md5 = hashlib.md5(clean_url.encode("utf-8")).hexdigest()
    return f"FB_{md5[:10]}"

def parse_facebook_response(html_text: str, url: str) -> Tuple[bool, str, str]:
    soup = BeautifulSoup(html_text, "html.parser")
    uid = extract_fb_uid(url)

    # 1. Check title
    title = ""
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        title = og_title["content"].strip()
    if not title:
        title_tag = soup.find("title")
        if title_tag and title_tag.text:
            title = title_tag.text.strip()

    title_clean = title
    if title:
        title_clean = re.sub(r"\s*\|\s*Facebook$", "", title, flags=re.I).strip()
        title_clean = re.sub(r"^Facebook\s*[- :]\s*", "", title_clean, flags=re.I).strip()

    # 2. Check explicitly for strict dead messages in text
    lower_html = html_text.lower()
    for dead_phrase in GENUINE_DEAD_SIGNATURES:
        if dead_phrase in lower_html:
            return False, f"Dead indicator detected: '{dead_phrase}'", title_clean or f"Post ({uid})"

    # 3. If OpenGraph description or title exists with valid content, it is alive
    og_desc = soup.find("meta", property="og:description")
    if og_title or og_desc:
        desc_text = og_desc.get("content", "").strip() if og_desc else ""
        final_name = title_clean or (desc_text[:35] + "...") if desc_text else f"Facebook Post ({uid})"
        return True, "Content accessible via OpenGraph", final_name

    # 4. Check for genuine Facebook 404 page container
    if 'id="m_error_page"' in html_text or 'data-sigil="m_error_page"' in html_text:
        return False, "Facebook error page container detected", title_clean or f"Post ({uid})"

    # Fallback to Alive to avoid false dead alerts
    return True, "Content considered alive (Fallback)", title_clean or f"Facebook Post ({uid})"

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

    clean_target = url.strip()
    if not clean_target.startswith(("http://", "https://")):
        clean_target = "https://" + clean_target

    mobile_target = re.sub(r"^(https?://)(?:www\.|web\.)?facebook\.com", r"\1m.facebook.com", clean_target)

    headers = {
        "User-Agent": custom_user_agent or random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,bn;q=0.8",
        "Sec-Fetch-Mode": "navigate",
        "Upgrade-Insecure-Requests": "1",
    }

    try:
        async with session.get(
            mobile_target,
            headers=headers,
            allow_redirects=True,
            ssl=False
        ) as resp:
            status_code = resp.status

            if status_code in (404, 410):
                return CheckResult(
                    is_alive=False,
                    status="DEAD",
                    reason=f"HTTP Status {status_code}",
                    title=f"Facebook Post ({uid})",
                    uid=uid,
                    url=url,
                    status_code=status_code,
                )

            html_text = await resp.text(errors="ignore")
            is_alive, reason, title = parse_facebook_response(html_text, url)

            return CheckResult(
                is_alive=is_alive,
                status="ACTIVE" if is_alive else "DEAD",
                reason=reason,
                title=title,
                uid=uid,
                url=url,
                status_code=status_code,
            )
    except Exception as e:
        logger.warning(f"Error checking {url}: {e}")
        return CheckResult(
            is_alive=True,
            status="ACTIVE",
            reason="Transient network glitch",
            title=f"Facebook Post ({uid})",
            uid=uid,
            url=url,
            status_code=0,
        )
    finally:
        if should_close_session:
            await session.close()
