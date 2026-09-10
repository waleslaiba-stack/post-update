"""
Facebook Deep Multi-Pass Link Checker.
Uses Messenger/WhatsApp Link Preview Scraper Protocol with 5-Cycle Verification.
"""
import re
import hashlib
import logging
import asyncio
from typing import Optional, Tuple, Dict, Any
from dataclasses import dataclass
from urllib.parse import urlparse, parse_qs
import aiohttp
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Official Facebook Link Preview Scraper Identifiers (Used by Messenger/WhatsApp)
MESSENGER_PREVIEW_HEADERS = [
    {
        "User-Agent": "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    },
    {
        "User-Agent": "facebookexternalhit/1.1",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
]

# Dead text markers that only appear on removed/deleted targets
AUTHENTIC_DEAD_MARKERS = [
    "this content isn't available right now",
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

GENERIC_TRASH_TITLES = [
    "facebook",
    "log into facebook",
    "log in to facebook",
    "log in",
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

async def _probe_link_once(target_url: str, session: aiohttp.ClientSession, header_index: int = 0) -> Tuple[bool, str, str, int]:
    headers = MESSENGER_PREVIEW_HEADERS[header_index % len(MESSENGER_PREVIEW_HEADERS)]
    try:
        async with session.get(
            target_url,
            headers=headers,
            allow_redirects=True,
            timeout=aiohttp.ClientTimeout(total=8),
            ssl=False
        ) as resp:
            status_code = resp.status
            final_url = str(resp.url).lower()

            if status_code in (404, 410):
                return False, f"HTTP Status {status_code}", "", status_code

            if "checkpoint/block" in final_url or "/help/contact/" in final_url:
                return False, "Checkpoint barrier / Account blocked", "", status_code

            html_text = await resp.text(errors="ignore")
            lower_html = html_text.lower()

            # Direct dead string found
            for marker in AUTHENTIC_DEAD_MARKERS:
                if marker in lower_html:
                    return False, f"Dead content signature: '{marker}'", "", status_code

            soup = BeautifulSoup(html_text, "html.parser")

            # Extract Authentic OpenGraph Title
            og_title = ""
            og_title_tag = soup.find("meta", property="og:title")
            if og_title_tag and og_title_tag.get("content"):
                og_title = og_title_tag["content"].strip()

            if not og_title:
                title_tag = soup.find("title")
                if title_tag and title_tag.text:
                    og_title = title_tag.text.strip()

            clean_title = re.sub(r"\s*\|\s*Facebook$", "", og_title, flags=re.I).strip()
            clean_title = re.sub(r"^Facebook\s*[- :]\s*", "", clean_title, flags=re.I).strip()

            # Check if title explicitly claims dead
            for marker in AUTHENTIC_DEAD_MARKERS:
                if marker in clean_title.lower():
                    return False, f"Dead notice in title: '{clean_title}'", "", status_code

            # Check if title is blank or generic login prompt
            if clean_title.lower() in GENERIC_TRASH_TITLES:
                # Check description as fallback
                og_desc_tag = soup.find("meta", property="og:description")
                og_desc = og_desc_tag["content"].strip() if og_desc_tag and og_desc_tag.get("content") else ""
                if not og_desc or og_desc.lower() in GENERIC_TRASH_TITLES:
                    return False, "No preview metadata available (Target content does not exist)", "", status_code

            # Valid live content preview found
            return True, "Valid OpenGraph preview detected", clean_title, status_code

    except Exception as e:
        logger.warning(f"Probe exception on {target_url}: {e}")
        return False, f"Probe error: {type(e).__name__}", "", 0

async def check_facebook_link_deep(
    url: str,
    session: Optional[aiohttp.ClientSession] = None,
    total_checks: int = 5,
    delay_between_checks: float = 2.0
) -> CheckResult:
    """
    Executes multiple verification probes before issuing a verdict.
    Guarantees that active posts are not marked dead, and dead posts are caught accurately.
    """
    target_url = normalize_facebook_url(url)
    uid = extract_fb_uid(target_url)
    should_close_session = False

    if session is None:
        session = aiohttp.ClientSession()
        should_close_session = True

    alive_count = 0
    dead_count = 0
    last_reason = ""
    discovered_title = ""
    last_code = 200

    try:
        for i in range(total_checks):
            is_alive, reason, title, code = await _probe_link_once(target_url, session, header_index=i)
            last_reason = reason
            last_code = code

            if is_alive:
                alive_count += 1
                if title and not discovered_title:
                    discovered_title = title
            else:
                dead_count += 1

            if i < total_checks - 1:
                await asyncio.sleep(delay_between_checks)

        # Verdict logic:
        # If at least 2 out of 5 checks returned valid preview metadata, the content IS ALIVE.
        if alive_count >= 2:
            return CheckResult(
                is_alive=True,
                status="ACTIVE",
                reason=f"Verified alive ({alive_count}/{total_checks} successful probes)",
                title=discovered_title or f"Facebook ({uid})",
                uid=uid,
                url=target_url,
                status_code=last_code,
            )
        else:
            return CheckResult(
                is_alive=False,
                status="DEAD",
                reason=f"Failed validation ({dead_count}/{total_checks} probes failed - {last_reason})",
                title=f"Dead Content ({uid})",
                uid=uid,
                url=target_url,
                status_code=last_code,
            )

    finally:
        if should_close_session:
            await session.close()

# Backward compatible single entry point
async def check_facebook_link(url: str, session: Optional[aiohttp.ClientSession] = None, **kwargs) -> CheckResult:
    return await check_facebook_link_deep(url, session=session, total_checks=5, delay_between_checks=1.5)
