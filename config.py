"""
Central configuration loaded from environment variables (.env in local/dev,
Railway "Variables" tab in production).
"""
import os
from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Set it in your .env file or Railway project variables."
        )
    return val


# --- Telegram ---
BOT_TOKEN: str = _require("BOT_TOKEN")
ADMIN_ID: int = int(_require("ADMIN_ID"))

# --- Facebook Graph API ---
FB_ACCESS_TOKEN: str = _require("FB_ACCESS_TOKEN")
GRAPH_API_VERSION: str = os.getenv("GRAPH_API_VERSION", "v19.0")
GRAPH_API_BASE: str = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

# --- App behavior ---
DB_PATH: str = os.getenv("DB_PATH", "bot.db")
CHECK_INTERVAL_SECONDS: int = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))
DEFAULT_TIMEZONE: str = os.getenv("DEFAULT_TIMEZONE", "Asia/Dhaka")

# Timezones selectable from the UI
SUPPORTED_TIMEZONES = {
    "🇧🇩 Bangladesh (Dhaka)": "Asia/Dhaka",
    "🇮🇳 India (Kolkata)": "Asia/Kolkata",
    "🇸🇦 Saudi Arabia (Riyadh)": "Asia/Riyadh",
    "🇦🇪 UAE (Dubai)": "Asia/Dubai",
    "🇬🇧 UK (London)": "Europe/London",
    "🇺🇸 US (New York)": "America/New_York",
}

# Graph error codes that reliably mean "this object is gone / not visible to us"
# rather than a transient network / rate-limit / auth problem.
# See: https://developers.facebook.com/docs/graph-api/guides/error-handling
GRAPH_DEAD_ERROR_CODES = {100, 21, 2500}
GRAPH_DEAD_ERROR_SUBCODES = {33, 2069030}  # "does not exist" / object deleted variants

# Codes that mean OUR token/config is broken, not that the object is dead.
# These should never flip a link to DEAD - they should alert the admin instead.
GRAPH_AUTH_ERROR_CODES = {190, 200, 10}
