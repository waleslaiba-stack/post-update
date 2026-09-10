"""
Facebook Universal Link Accessibility Checker with Proxy Engine.
Bypasses Cloud/Datacenter IP blocks via Rotating/Static Residential Proxy.
"""
import os
import re
import hashlib
import logging
from typing import Optional, Tuple
from dataclasses import dataclass
from urllib.parse import urlparse, parse_qs
import aiohttp
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

FB_PREVIEW_UA = "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"

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

def parse_html_response(html_text: str, final_url: str, uid: str) -> Tuple[bool, str, str]:
    lower_html = html_text.lower()

    for marker in DEFINITE_DEAD_MARKERS:
        if marker in lower_html:
            return False, f"Dead content notice: '{marker}'", ""

    if "checkpoint/block" in final_url.lower() or "/help/contact/" in final_url.lower():
        return False, "Checkpoint barrier / Account blocked", ""

    soup = BeautifulSoup(html_text, "html.parser")

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

    for marker in DEFINITE_DEAD_MARKERS:
        if marker in clean_title.lower():
            return False, f"Dead indicator in title: '{clean_title}'", ""

    if clean_title.lower() in GENERIC_TRASH_TITLES:
        og_desc_tag = soup.find("meta", property="og:description")
        og_desc = og_desc_tag["content"].strip() if og_desc_tag and og_desc_tag.get("content") else ""
        if not og_desc or og_desc.lower() in GENERIC_TRASH_TITLES:
            return False, "No preview content available (Post/Profile removed)", ""

    return True, "Authentic metadata verified", clean_title

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

    proxy = proxy_url or os.getenv("PROXY_URL", "").strip() or None

    headers = {
        "User-Agent": custom_user_agent or FB_PREVIEW_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,bn;q=0.8",
        "Sec-Fetch-Mode": "navigate",
    }

    try:
        async with session.get(
            target_url,
            headers=headers,
            proxy=proxy,
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
            is_alive, reason, title = parse_html_response(html_text, final_url, uid)

            return CheckResult(
                is_alive=is_alive,
                status="ACTIVE" if is_alive else "DEAD",
                reason=reason,
                title=title or f"Facebook ({uid})",
                uid=uid,
                url=target_url,
                status_code=status_code
            )

    except Exception as e:
        logger.warning(f"Connection glitch on {target_url}: {e}")
        # Network hiccups do NOT kill links falsely
        return CheckResult(
            is_alive=True,
            status="ACTIVE",
            reason="Network delay (Link kept ACTIVE)",
            title=f"Facebook ({uid})",
            uid=uid,
            url=target_url,
            status_code=0
        )
    finally:
        if should_close_session:
            await session.close()
