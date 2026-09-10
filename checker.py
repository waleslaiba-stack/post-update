"""
Facebook Link Accessibility Checker (Production Engine via Facebook oEmbed Protocol).
Eliminates datacenter scraping blocks & false positives.
Works with raw URLs, missing https/www, and extracts genuine live status.
"""
import re
import hashlib
import logging
import asyncio
from typing import Optional, Tuple
from dataclasses import dataclass
from urllib.parse import urlparse, parse_qs, quote
import aiohttp

logger = logging.getLogger(__name__)

# Official Facebook Graph & Plugin oEmbed endpoints for unauthenticated status inspection
OEMBED_ENDPOINTS = [
    "https://www.facebook.com/plugins/post/oembed.json/?url=",
    "https://www.facebook.com/plugins/video/oembed.json/?url="
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
    Cleans and standardizes raw inputs like 'facebook.com/username' or 'fb.watch/xyz'
    into a valid HTTPS Facebook URL.
    """
    url = raw_url.strip()
    # Strip unnecessary trailing punctuation
    url = url.rstrip(",.;!$*")

    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    elif url.startswith("http://"):
        url = "https://" + url[7:]

    # Normalize mobile/mbasic prefixes to standard www for oEmbed compatibility
    url = re.sub(r"^(https?://)(?:m\.|mbasic\.|web\.)facebook\.com", r"\1www.facebook.com", url)
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

async def check_via_oembed(normalized_url: str, session: aiohttp.ClientSession) -> Tuple[Optional[bool], str, str]:
    """
    Queries Facebook's native oEmbed API.
    Returns: (is_alive, reason, author/title)
    """
    headers = {
        "User-Agent": "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }

    encoded_url = quote(normalized_url, safe="")

    for endpoint in OEMBED_ENDPOINTS:
        target_api = f"{endpoint}{encoded_url}"
        try:
            async with session.get(target_api, headers=headers, timeout=aiohttp.ClientTimeout(total=8), ssl=False) as resp:
                if resp.status == 200:
                    try:
                        data = await resp.json()
                        author = data.get("author_name") or data.get("title") or "Facebook Post"
                        return True, "Verified ACTIVE via Facebook oEmbed API", author
                    except Exception:
                        return True, "Verified ACTIVE via Facebook oEmbed", "Facebook Post"
                elif resp.status in (404, 400):
                    # 404/400 from oEmbed specifically means content does not exist or was deleted
                    continue
        except Exception:
            continue

    return None, "oEmbed unconfirmed", ""

async def check_via_http_inspection(normalized_url: str, session: aiohttp.ClientSession) -> Tuple[bool, str, str]:
    """
    Fallback HTTP inspection using Facebook External Hit crawler identity.
    """
    uid = extract_fb_uid(normalized_url)
    headers = {
        "User-Agent": "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    try:
        async with session.get(normalized_url, headers=headers, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=10), ssl=False) as resp:
            status_code = resp.status
            final_url = str(resp.url).lower()

            if status_code in (404, 410):
                return False, f"HTTP {status_code} (Not Found)", f"Dead Content ({uid})"

            # Genuine Facebook redirection to error or dead-end home
            if "/help/" in final_url or "checkpoint" in final_url:
                return False, "Redirected to Facebook Help/Barrier (Deleted)", f"Dead Content ({uid})"

            html_text = await resp.text(errors="ignore")
            lower_html = html_text.lower()

            dead_signatures = [
                "this content isn't available right now",
                "the link you followed may be broken",
                "content not found",
                "page not found",
                "this page isn't available",
                "attachment unavailable",
                "এই কন্টেন্টটি এখন উপলভ্য নয়",
            ]

            for phrase in dead_signatures:
                if phrase in lower_html:
                    return False, f"Dead notice found: '{phrase}'", f"Content Removed ({uid})"

            # Check title for dead signs
            m_title = re.search(r"<title>(.*?)</title>", html_text, re.IGNORECASE)
            title = m_title.group(1).strip() if m_title else f"Facebook Post ({uid})"
            title = re.sub(r"\s*\|\s*Facebook$", "", title, flags=re.I).strip()

            if any(phrase in title.lower() for phrase in dead_signatures):
                return False, f"Dead notice in title: '{title}'", f"Content Removed ({uid})"

            return True, "Content accessible", title or f"Facebook Post ({uid})"

    except Exception as e:
        logger.warning(f"Inspection error on {normalized_url}: {e}")
        # Default to False if connection completely fails
        return False, "Host unreachable / Broken link", f"Dead Content ({uid})"

async def check_facebook_link(
    url: str,
    session: Optional[aiohttp.ClientSession] = None,
    timeout_seconds: int = 15,
    custom_user_agent: Optional[str] = None
) -> CheckResult:
    normalized_url = normalize_facebook_url(url)
    uid = extract_fb_uid(normalized_url)
    should_close_session = False

    if session is None:
        session = aiohttp.ClientSession()
        should_close_session = True

    try:
        # Step 1: Query Facebook oEmbed Endpoint (Most accurate for posts, videos, reels)
        is_live_oembed, oembed_reason, oembed_title = await check_via_oembed(normalized_url, session)

        if is_live_oembed is True:
            return CheckResult(
                is_alive=True,
                status="ACTIVE",
                reason=oembed_reason,
                title=oembed_title,
                uid=uid,
                url=normalized_url,
                status_code=200,
            )

        # Step 2: Fallback to Facebook External Hit HTTP verification
        is_alive, reason, title = await check_via_http_inspection(normalized_url, session)

        return CheckResult(
            is_alive=is_alive,
            status="ACTIVE" if is_alive else "DEAD",
            reason=reason,
            title=title,
            uid=uid,
            url=normalized_url,
            status_code=200 if is_alive else 404,
        )

    finally:
        if should_close_session:
            await session.close()
