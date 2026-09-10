"""
Facebook Graph API health checker.

This module ONLY talks to the official https://graph.facebook.com REST API
using a user-supplied access token. It never scrapes facebook.com pages and
never tries to bypass login walls - it can only see objects (pages, posts,
photos, videos...) that the token's owner actually has permission to read.
That means it's suitable for monitoring your OWN content, not arbitrary
public links.
"""
import re
import urllib.parse
from dataclasses import dataclass
from typing import Optional

import aiohttp

import config

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

STATUS_ACTIVE = "ACTIVE"
STATUS_DEAD = "DEAD"
STATUS_UNKNOWN = "UNKNOWN"  # transient error - caller should NOT change stored status


@dataclass
class CheckResult:
    status: str                 # STATUS_ACTIVE / STATUS_DEAD / STATUS_UNKNOWN
    name: Optional[str] = None  # object's display name/title, if available
    detail: Optional[str] = None  # human-readable reason, for logs/admin alerts
    is_auth_error: bool = False   # True if the problem is OUR token, not the object


# ---------------------------------------------------------------------------
# Object-id extraction
# ---------------------------------------------------------------------------
# The Graph API identifies objects by numeric ID, "{page_id}_{post_id}" for
# classic posts, or a page's username. We try to pull one of those out of
# whatever the user pastes; if we can't, we fall back to treating the whole
# input as a literal object id (which also lets users paste IDs directly).

_NUMERIC = re.compile(r"^\d+$")
_PAGE_POST = re.compile(r"^\d+_\d+$")


def extract_object_id(raw_input: str) -> str:
    text = raw_input.strip()

    # Already a bare object id, e.g. "1234567890" or "1234567890_998877"
    if _NUMERIC.match(text) or _PAGE_POST.match(text):
        return text

    if not text.lower().startswith("http"):
        # Not a URL and not numeric -> assume it's a page username/slug
        return text

    parsed = urllib.parse.urlparse(text)
    path = parsed.path.strip("/")
    query = urllib.parse.parse_qs(parsed.query)

    # permalink.php?story_fbid=X&id=Y  ->  Y_X
    if "permalink.php" in path and "story_fbid" in query and "id" in query:
        return f"{query['id'][0]}_{query['story_fbid'][0]}"

    parts = path.split("/")

    # .../{page}/posts/{post_id}  or  .../{page}/videos/{video_id}
    for marker in ("posts", "videos", "photos", "reel", "reels"):
        if marker in parts:
            idx = parts.index(marker)
            if idx + 1 < len(parts):
                candidate = parts[idx + 1]
                page_slug = parts[0] if idx > 0 else None
                if _NUMERIC.match(candidate) and page_slug and not _NUMERIC.match(page_slug):
                    # We only know the page's *username*, not its numeric id -
                    # Graph can't combine those into a post id, so just probe
                    # the post id on its own (works for many public post ids).
                    return candidate
                return candidate

    # profile.php?id=123
    if "profile.php" in path and "id" in query:
        return query["id"][0]

    # groups/{group_id}/permalink/{post_id}
    if "groups" in parts:
        idx = parts.index("groups")
        if idx + 2 < len(parts) and parts[idx + 2] == "permalink":
            return parts[idx + 3] if idx + 3 < len(parts) else parts[idx + 1]
        if idx + 1 < len(parts):
            return parts[idx + 1]

    # Plain profile/page: facebook.com/{username}
    if parts and parts[0] not in ("share", "watch", ""):
        return parts[0]

    # Give up gracefully - return the raw input, the API call will just fail
    # with a clear "unsupported request" error that we surface to the user.
    return text


# ---------------------------------------------------------------------------
# Graph API call
# ---------------------------------------------------------------------------

async def check_object(session: aiohttp.ClientSession, object_id: str) -> CheckResult:
    """
    Query the Graph API for a single object. Never raises - all failure modes
    are folded into a CheckResult so the caller has one code path.
    """
    url = f"{config.GRAPH_API_BASE}/{urllib.parse.quote(object_id, safe='_')}"
    params = {"fields": "id,name", "access_token": config.FB_ACCESS_TOKEN}

    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            data = await resp.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, Exception) as exc:  # network/timeout = transient
        return CheckResult(status=STATUS_UNKNOWN, detail=f"network error: {exc}")

    if resp.status == 200 and "id" in data:
        return CheckResult(status=STATUS_ACTIVE, name=data.get("name"))

    error = data.get("error", {}) if isinstance(data, dict) else {}
    code = error.get("code")
    subcode = error.get("error_subcode")
    message = error.get("message", "Unknown Graph API error")

    if code in config.GRAPH_AUTH_ERROR_CODES:
        # Our token is invalid/expired/lacking permission - this is OUR problem,
        # not evidence the object is dead. Surface it distinctly.
        return CheckResult(status=STATUS_UNKNOWN, detail=message, is_auth_error=True)

    if code in config.GRAPH_DEAD_ERROR_CODES or subcode in config.GRAPH_DEAD_ERROR_SUBCODES:
        return CheckResult(status=STATUS_DEAD, detail=message)

    # Rate limiting (4, 17, 32) and anything else unrecognized: treat as
    # transient so a hiccup never falsely flips a link to DEAD.
    return CheckResult(status=STATUS_UNKNOWN, detail=message)
