"""
Facebook Link Accessibility Checker.
Inspects HTTP headers, redirects, HTML keyword signatures, and UID.
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
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.82 Mobile Safari/537.36"
]

DEAD_PATTERNS = [
    re.compile(r"This content isn't available right now", re.I),
    re.compile(r"The link you followed may be broken", re.I),
    re.compile(r"or the page may have been removed", re.I),
    re.compile(r"Content Not Found", re.I),
    re.compile(r"Page Not Found", re.I),
    re.compile(r"This page isn't available", re.I),
    re.compile(r"Attachment Unavailable", re.I),
    re.compile(r"Sorry, something went wrong", re.I),
    re.compile(r"Nội dung này hiện không khả dụng", re.I),
    re.compile(r"Este contenido no está disponible", re.I),
    re.compile(r"Ce contenu n'est pas disponible", re.I),
    re.compile(r"Inhalt derzeit nicht verfügbar", re.I),
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

def extract_title_and_status(html_text: str, url: str) -> Tuple[bool, str, str]:
    soup = BeautifulSoup(html_text, "html.parser")
    title = ""
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        title = og_title["content"].strip()

    if not title:
        title_tag = soup.find("title")
        if title_tag and title_tag.text:
            title = title_tag.text.strip()

    if title:
        title = re.sub(r"\s*\|\s*Facebook$", "", title, flags=re.I).strip()
        title = re.sub(r"^Facebook\s*[- :]\s*", "", title, flags=re.I).strip()

    if title:
        for pat in DEAD_PATTERNS:
            if pat.search(title):
                return False, f"Dead marker in page title: '{title}'", title or "Unknown"

    body_text = soup.get_text(separator=" ", strip=True)
    for pat in DEAD_PATTERNS:
        if pat.search(body_text):
            return False, "Facebook content removal notice detected in page body", title or "Unknown"

    if "checkpoint/block" in html_text or ("login.php?next=" in html_text and "privacy_mutation_token" in html_text):
        return False, "Redirected to checkpoint/security barrier", title or "Unknown"

    if not title:
        title = f"Facebook Post ({extract_fb_uid(url)})"

    return True, "Content accessible", title

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

    headers = {
        "User-Agent": custom_user_agent or random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "DNT": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }

    try:
        target_url = url.strip()
        if not target_url.startswith(("http://", "https://")):
            target_url = "https://" + target_url

        async with session.get(
            target_url,
            headers=headers,
            allow_redirects=True,
            ssl=False
        ) as resp:
            status_code = resp.status
            final_url = str(resp.url).lower()

            if status_code in (404, 410):
                return CheckResult(
                    is_alive=False,
                    status="DEAD",
                    reason=f"HTTP status code {status_code} (Not Found / Gone)",
                    title=f"Facebook Post ({uid})",
                    uid=uid,
                    url=url,
                    status_code=status_code,
                )

            if any(term in final_url for term in [
                "/login.php",
                "/login/",
                "checkpoint",
                "/help/",
                "?stype=lo",
                "login/?next="
            ]):
                return CheckResult(
                    is_alive=False,
                    status="DEAD",
                    reason="Redirected to Facebook login/checkpoint (Content removed or private)",
                    title=f"Facebook Post ({uid})",
                    uid=uid,
                    url=url,
                    status_code=status_code,
                )

            html_text = await resp.text(errors="ignore")
            is_alive, reason, title = extract_title_and_status(html_text, url)
            return CheckResult(
                is_alive=is_alive,
                status="ACTIVE" if is_alive else "DEAD",
                reason=reason,
                title=title,
                uid=uid,
                url=url,
                status_code=status_code,
            )
    except aiohttp.ClientError as e:
        logger.warning(f"Network error checking {url}: {e}")
        return CheckResult(
            is_alive=True,
            status="ACTIVE",
            reason=f"Network error (temporary): {type(e).__name__}",
            title=f"Facebook Post ({uid})",
            uid=uid,
            url=url,
            status_code=0,
        )
    except asyncio.TimeoutError:
        logger.warning(f"Timeout checking {url}")
        return CheckResult(
            is_alive=True,
            status="ACTIVE",
            reason="Request timed out (temporary delay)",
            title=f"Facebook Post ({uid})",
            uid=uid,
            url=url,
            status_code=408,
        )
    except Exception as e:
        logger.error(f"Unexpected error checking {url}: {e}", exc_info=True)
        return CheckResult(
            is_alive=True,
            status="ACTIVE",
            reason=f"Check exception: {str(e)}",
            title=f"Facebook Post ({uid})",
            uid=uid,
            url=url,
            status_code=500,
        )
    finally:
        if should_close_session:
            await session.close()
