"""
MyFitnessPal MCP Server

A Model Context Protocol (MCP) server that provides tools for interacting
with MyFitnessPal data including food diary, exercises, measurements, goals,
and food search.

Authentication:
  Cookies are the single auth method. The server loads from ~/.mfp_mcp/cookies.json.
  If a Netscape-format cookie export exists at ~/Downloads/www.myfitnesspal.com_cookies.txt
  and is newer than cookies.json, it is auto-imported on the next tool call.
"""

import json
import importlib
import logging
import sys
from datetime import date, datetime, timedelta
from http.cookiejar import CookieJar, Cookie
from pathlib import Path
from typing import Optional, Dict, Any, List
from enum import Enum
from collections import OrderedDict
import time

import requests as req_lib
import lxml.html
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field, ConfigDict, field_validator

# Configure logging to stderr (required for stdio transport)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("mfp_mcp")

# Initialize MCP server
mcp = FastMCP("myfitnesspal_mcp")

# Configuration paths
CONFIG_DIR = Path.home() / ".mfp_mcp"
COOKIES_FILE = CONFIG_DIR / "cookies.json"
NETSCAPE_COOKIES_FILE = Path.home() / "Downloads" / "www.myfitnesspal.com_cookies.txt"
BROWSER_IMPORT_ORDER = ("brave", "chrome", "chromium", "firefox")
COOKIE_REFRESH_WINDOW = timedelta(hours=6)
CRITICAL_COOKIE_NAMES = (
    "__Secure-next-auth.session-token",
    "_mfp_session",
    "cf_clearance",
    "__cf_bm",
    "__Host-next-auth.csrf-token",
)
AUTO_REFRESH_COOKIE_NAMES = (
    "__Secure-next-auth.session-token",
    "_mfp_session",
    "cf_clearance",
    "__Host-next-auth.csrf-token",
)


# ============================================================================
# Authentication Helper Functions
# ============================================================================

_NO_COOKIES_MSG = (
    "No valid cookies found.\n"
    "Export cookies from your browser (Netscape format) to:\n"
    f"  {NETSCAPE_COOKIES_FILE}\n"
    "or manually place a cookies.json in:\n"
    f"  {COOKIES_FILE}"
)


def normalize_expires(expires) -> Optional[int]:
    """Normalize an expires value to Unix seconds. Handles ms and µs timestamps."""
    if expires is None or expires == 0:
        return expires
    expires = int(expires)
    if expires > 32503680000:       # year 3000 in seconds
        if expires > 32503680000000:  # likely microseconds
            expires //= 1_000_000
        else:                         # likely milliseconds
            expires //= 1_000
    return expires


def ensure_config_dir():
    """Ensure the config directory exists."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def normalize_cookie_records(raw_cookies: Any) -> List[Dict[str, Any]]:
    """
    Normalize cookies from either the legacy dict format or the richer list format.
    """
    normalized = []

    if isinstance(raw_cookies, dict):
        for name, value in raw_cookies.items():
            normalized.append(
                {
                    "name": name,
                    "value": value,
                    "domain": ".myfitnesspal.com",
                    "path": "/",
                    "secure": True,
                    "expires": None,
                    "discard": False,
                    "source": "legacy-json",
                }
            )
        return normalized

    if not isinstance(raw_cookies, list):
        return normalized

    for item in raw_cookies:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        value = item.get("value")
        if not name or value is None:
            continue
        expires = item.get("expires")
        normalized.append(
            {
                "name": str(name),
                "value": str(value),
                "domain": item.get("domain") or ".myfitnesspal.com",
                "path": item.get("path") or "/",
                "secure": bool(item.get("secure", True)),
                "expires": normalize_expires(
                    int(expires)
                    if isinstance(expires, (int, float))
                    or (isinstance(expires, str) and expires.isdigit())
                    else None
                ),
                "discard": bool(item.get("discard", False)),
                "source": item.get("source", "saved-cookie-record"),
            }
        )

    return normalized


def cookiejar_to_records(cookiejar: CookieJar, source: str) -> List[Dict[str, Any]]:
    """Convert a CookieJar into serializable cookie records."""
    records = []
    for cookie in cookiejar:
        records.append(
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path or "/",
                "secure": bool(cookie.secure),
                "expires": normalize_expires(cookie.expires),
                "discard": bool(cookie.discard),
                "source": source,
            }
        )
    return records


def cookie_records_to_dict(cookie_records: List[Dict[str, Any]]) -> Dict[str, str]:
    """Collapse cookie records into the name -> value format used by callers."""
    return {record["name"]: record["value"] for record in cookie_records}


def summarize_cookie_records(
    cookie_records: List[Dict[str, Any]], saved_at: Optional[datetime] = None
) -> Dict[str, Any]:
    """Summarize cookie freshness, focusing on the cookies required for writes."""
    now = time.time()
    by_name = {record["name"]: record for record in cookie_records}
    missing = [name for name in CRITICAL_COOKIE_NAMES if name not in by_name]
    auto_refresh_missing = [
        name for name in AUTO_REFRESH_COOKIE_NAMES if name not in by_name
    ]
    expiring_soon = []
    expired = []
    auto_refresh_expiring_soon = []
    auto_refresh_expired = []

    for name in CRITICAL_COOKIE_NAMES:
        record = by_name.get(name)
        if not record:
            continue
        expires = record.get("expires")
        if expires in (None, 0):
            continue
        expires_norm = normalize_expires(expires)
        expires_dt = datetime.fromtimestamp(expires_norm)
        if expires_dt <= datetime.now():
            expired.append(name)
            if name in AUTO_REFRESH_COOKIE_NAMES:
                auto_refresh_expired.append(name)
        elif expires_dt - datetime.now() <= COOKIE_REFRESH_WINDOW:
            expiring_soon.append(name)
            if name in AUTO_REFRESH_COOKIE_NAMES:
                auto_refresh_expiring_soon.append(name)

    return {
        "count": len(cookie_records),
        "missing_critical": missing,
        "expired_critical": expired,
        "expiring_soon_critical": expiring_soon,
        "saved_at": saved_at.isoformat() if saved_at else None,
        "needs_refresh": bool(auto_refresh_missing or auto_refresh_expired),
        "refresh_recommended": bool(
            auto_refresh_missing or auto_refresh_expired or auto_refresh_expiring_soon
        ),
        "important_cookies": {
            name: {
                "present": name in by_name,
                "expires_at": (
                    datetime.fromtimestamp(normalize_expires(by_name[name]["expires"])).isoformat()
                    if name in by_name and by_name[name].get("expires") not in (None, 0)
                    else None
                ),
                "session_cookie": bool(
                    name in by_name and by_name[name].get("expires") in (None, 0)
                ),
            }
            for name in CRITICAL_COOKIE_NAMES
        },
        "checked_at": datetime.fromtimestamp(now).isoformat(),
    }


def save_cookies(cookie_records: List[Dict[str, Any]], source: str = "unknown"):
    """Save cookie records to ~/.mfp_mcp/cookies.json."""
    ensure_config_dir()
    cookie_data = {
        "cookies": cookie_records,
        "source": source,
        "saved_at": datetime.now().isoformat(),
    }
    with open(COOKIES_FILE, "w") as f:
        json.dump(cookie_data, f, indent=2)
    logger.info(f"Saved {len(cookie_records)} cookies to {COOKIES_FILE} from {source}")


def import_netscape_cookies() -> Optional[List[Dict[str, Any]]]:
    """
    Parse a Netscape-format cookie file into structured cookie records.
    Returns None if the file doesn't exist or is empty.
    """
    if not NETSCAPE_COOKIES_FILE.exists():
        return None

    cookies = []
    with open(NETSCAPE_COOKIES_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                expires_raw = parts[4]
                expires = int(expires_raw) if expires_raw.isdigit() and expires_raw != "0" else None
                cookies.append(
                    {
                        "domain": parts[0],
                        "path": parts[2] or "/",
                        "secure": parts[3] == "TRUE",
                        "expires": expires,
                        "discard": expires is None,
                        "name": parts[5],
                        "value": parts[6],
                        "source": "netscape-export",
                    }
                )

    if cookies:
        logger.info(
            f"Parsed {len(cookies)} cookies from Netscape file "
            f"{NETSCAPE_COOKIES_FILE}"
        )
        return normalize_cookie_records(cookies)
    return None


def import_browser_cookies(
    preferred_browser: Optional[str] = None,
) -> tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """
    Attempt to import cookies directly from a browser profile.
    """
    if importlib.util.find_spec("browser_cookie3") is None:
        return None, None

    import browser_cookie3

    browsers = [preferred_browser] if preferred_browser else list(BROWSER_IMPORT_ORDER)
    for browser_name in browsers:
        if not browser_name:
            continue
        loader = getattr(browser_cookie3, browser_name, None)
        if loader is None:
            continue
        try:
            jar = loader(domain_name="myfitnesspal.com")
            records = normalize_cookie_records(
                cookiejar_to_records(jar, source=f"browser:{browser_name}")
            )
            if records:
                logger.info(
                    f"Imported {len(records)} cookies directly from {browser_name}"
                )
                return records, browser_name
        except Exception as exc:
            logger.info(f"Could not import cookies from {browser_name}: {exc}")

    return None, None


def maybe_refresh_cookie_records(
    cookie_records: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Refresh stale or near-expiry cookies from a live browser session when possible."""
    summary = summarize_cookie_records(cookie_records)
    if not summary["needs_refresh"]:
        return cookie_records

    imported, browser_name = import_browser_cookies(preferred_browser="brave")
    if imported and browser_name:
        save_cookies(imported, source=f"browser:{browser_name}")
        return imported

    logger.warning(
        "Cookie refresh recommended. Missing=%s expired=%s expiring_soon=%s",
        summary["missing_critical"],
        summary["expired_critical"],
        summary["expiring_soon_critical"],
    )
    return cookie_records


def load_cookie_records() -> Optional[List[Dict[str, Any]]]:
    """
    Load session cookies from the single source of truth (cookies.json).
    Auto-imports from Netscape cookie file if it's newer than cookies.json.

    Returns:
        Structured cookie records if available and fresh, None otherwise.
    """
    # Check if Netscape export is newer → auto-import
    if NETSCAPE_COOKIES_FILE.exists():
        netscape_mtime = NETSCAPE_COOKIES_FILE.stat().st_mtime
        cookies_mtime = COOKIES_FILE.stat().st_mtime if COOKIES_FILE.exists() else 0

        if netscape_mtime > cookies_mtime:
            logger.info("Netscape cookie file is newer than cookies.json — importing...")
            imported = import_netscape_cookies()
            if imported:
                save_cookies(imported, source="netscape-export")
                return maybe_refresh_cookie_records(imported)

    # Load from cookies.json
    if not COOKIES_FILE.exists():
        imported, browser_name = import_browser_cookies(preferred_browser="brave")
        if imported and browser_name:
            save_cookies(imported, source=f"browser:{browser_name}")
            return imported
        return None

    try:
        with open(COOKIES_FILE, "r") as f:
            cookie_data = json.load(f)

        saved_at = datetime.fromisoformat(cookie_data.get("saved_at", "2000-01-01"))
        age = datetime.now() - saved_at
        if age > timedelta(days=30):
            logger.warning(f"Stored cookies expired ({age.days} days old)")
            imported, browser_name = import_browser_cookies(preferred_browser="brave")
            if imported and browser_name:
                save_cookies(imported, source=f"browser:{browser_name}")
                return imported
            return None

        cookies = normalize_cookie_records(cookie_data.get("cookies"))
        if cookies:
            summary = summarize_cookie_records(cookies, saved_at=saved_at)
            logger.info(
                f"Loaded {len(cookies)} cookies from {COOKIES_FILE} "
                f"(saved: {saved_at.strftime('%Y-%m-%d %H:%M')})"
            )
            if summary["needs_refresh"]:
                logger.info(
                    "Cookie refresh recommended. Missing=%s expired=%s expiring_soon=%s",
                    summary["missing_critical"],
                    summary["expired_critical"],
                    summary["expiring_soon_critical"],
                )
        return maybe_refresh_cookie_records(cookies)
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        logger.warning(f"Failed to load cookies: {e}")
        return None


def cookie_records_to_cookiejar(cookie_records: List[Dict[str, Any]]) -> CookieJar:
    """Convert cookie records to a CookieJar for myfitnesspal.Client."""
    jar = CookieJar()

    for record in cookie_records:
        domain = record.get("domain") or ".myfitnesspal.com"
        path = record.get("path") or "/"
        cookie = Cookie(
            version=0,
            name=record["name"],
            value=record["value"],
            port=None,
            port_specified=False,
            domain=domain,
            domain_specified=True,
            domain_initial_dot=domain.startswith('.'),
            path=path,
            path_specified=True,
            secure=bool(record.get("secure", True)),
            expires=record.get("expires"),
            discard=bool(record.get("discard", record.get("expires") in (None, 0))),
            comment=None,
            comment_url=None,
            rest={"HttpOnly": None},
            rfc2109=False,
        )
        jar.set_cookie(cookie)

    return jar


def get_mfp_client():
    """
    Get an authenticated MyFitnessPal client using stored cookies.

    Returns:
        myfitnesspal.Client: Authenticated client instance

    Raises:
        RuntimeError: If no valid cookies are available
    """
    import myfitnesspal

    cookie_records = load_cookie_records()
    if not cookie_records:
        raise RuntimeError(_NO_COOKIES_MSG)

    try:
        cookiejar = cookie_records_to_cookiejar(cookie_records)
        client = myfitnesspal.Client(cookiejar=cookiejar)
        _ = client.get_date(date.today())
        return client
    except Exception as e:
        raise RuntimeError(
            f"Cookies loaded but authentication failed: {e}\n\n"
            "Your session may have expired. Re-export cookies from your browser."
        )


def get_raw_session() -> req_lib.Session:
    """
    Get a requests.Session authenticated with MFP cookies.
    Uses the same cookie source as get_mfp_client() (cookies.json).
    """
    cookie_records = load_cookie_records()
    if not cookie_records:
        raise RuntimeError(_NO_COOKIES_MSG)

    session = req_lib.Session()
    session.headers["User-Agent"] = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
    )

    for record in cookie_records:
        session.cookies.set(
            record["name"],
            record["value"],
            domain=record.get("domain") or ".myfitnesspal.com",
            path=record.get("path") or "/",
            secure=bool(record.get("secure", True)),
        )

    return session


def raw_search_foods(session: req_lib.Session, query: str, date_str: str, limit: int = 10):
    """
    Search MFP foods by scraping the HTML search page.
    Returns list of dicts with food_id (original_id), name, brand, calories,
    verified, serving_id (first weight_id), and all serving_ids.
    """
    r = session.get(
        "https://www.myfitnesspal.com/food/search",
        params={"search": query, "meal": "0", "date": date_str},
    )
    r.raise_for_status()
    doc = lxml.html.document_fromstring(r.text)

    results = []
    for a in doc.xpath("//a[@data-external-id]")[:limit]:
        weight_ids = [w for w in a.get("data-weight-ids", "").split(",") if w]
        # Extract brand/calories from sibling nutritional-info paragraph
        li = a.getparent()
        while li is not None and li.tag != "li":
            li = li.getparent()
        brand = ""
        calories = None
        serving = ""
        if li is not None:
            info_el = li.xpath(".//p[@class='search-nutritional-info']")
            if info_el:
                info_text = info_el[0].text_content().strip().split(",")
                if len(info_text) >= 3:
                    brand = ", ".join(info_text[0:-2]).strip()
                    serving = info_text[-2].strip()
                    try:
                        calories = float(info_text[-1].replace("calories", "").strip())
                    except ValueError:
                        pass
                elif len(info_text) == 2:
                    serving = info_text[0].strip()
                    try:
                        calories = float(info_text[-1].replace("calories", "").strip())
                    except ValueError:
                        pass

        results.append({
            "name": a.text_content().strip(),
            "brand": brand,
            "serving": serving,
            "calories": calories,
            "food_id": a.get("data-original-id"),
            "mfp_id": a.get("data-external-id"),
            "verified": a.get("data-verified") == "true",
            "serving_id": weight_ids[0] if weight_ids else None,
            "serving_ids": weight_ids,
        })

    return results


def raw_add_food(
    session: req_lib.Session,
    food_id: str,
    serving_id: str,
    date_str: str,
    meal_index: str,
    quantity: float,
):
    """
    Add a food to the MFP diary using the web form endpoint.
    Uses food_id (original_id) and serving_id (weight_id).
    """
    meal_name = {
        "0": "Breakfast",
        "1": "Lunch",
        "2": "Dinner",
        "3": "Snacks",
    }.get(meal_index, "Breakfast")
    before_entries = raw_get_diary_entries(session, date_str, "")
    before_ids = {
        entry["entry_id"]
        for entry in before_entries
        if entry.get("meal") == meal_name and entry.get("entry_id")
    }

    # Get CSRF token from search page
    r = session.get(
        "https://www.myfitnesspal.com/food/search",
        params={"search": "food", "meal": "0", "date": date_str},
    )
    r.raise_for_status()
    doc = lxml.html.document_fromstring(r.text)

    tokens = doc.xpath(
        "//form[@id='food-nutritional-details-form']"
        "//input[@name='authenticity_token']/@value"
    )
    if not tokens:
        tokens = doc.xpath("//input[@name='authenticity_token']/@value")
    if not tokens:
        raise RuntimeError("Could not find CSRF token")
    csrf = tokens[0]

    r = session.post(
        "https://www.myfitnesspal.com/food/add",
        data={
            "authenticity_token": csrf,
            "food_entry[food_id]": food_id,
            "food_entry[date]": date_str,
            "food_entry[meal_id]": meal_index,
            "food_entry[quantity]": str(quantity),
            "food_entry[weight_id]": serving_id,
        },
        headers={
            "Referer": "https://www.myfitnesspal.com/food/search",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        allow_redirects=True,
    )
    redirect_chain = [
        (resp.status_code, resp.headers.get("Location", "")) for resp in r.history
    ]
    if not (r.history and r.history[0].status_code == 302):
        raise RuntimeError(
            "Add request did not produce the expected redirect. "
            f"status={r.status_code} redirects={redirect_chain}"
        )
    r.raise_for_status()

    after_entries = raw_get_diary_entries(session, date_str, "")
    after_ids = {
        entry["entry_id"]
        for entry in after_entries
        if entry.get("meal") == meal_name and entry.get("entry_id")
    }
    if len(after_ids) <= len(before_ids):
        raise RuntimeError(
            "Add request returned a redirect but no new diary entry appeared. "
            "Your cookies may be stale; refresh them and try again."
        )


def raw_get_diary_entries(
    session: req_lib.Session, date_str: str, username: str
) -> List[Dict[str, Any]]:
    """
    Scrape the MFP diary HTML to extract entry IDs, food names, and meal associations.
    The myfitnesspal library discards entry IDs; this function recovers them.

    Returns list of dicts: [{"entry_id": "123", "name": "Chicken Breast", "meal": "Lunch"}, ...]
    """
    url = f"https://www.myfitnesspal.com/food/diary/{username}" if username else "https://www.myfitnesspal.com/food/diary"
    r = session.get(url, params={"date": date_str})
    r.raise_for_status()
    doc = lxml.html.document_fromstring(r.text)

    entries = []
    current_meal = None
    meal_headers = doc.xpath("//tr[@class='meal_header']")

    for meal_header in meal_headers:
        tds = meal_header.findall("td")
        current_meal = tds[0].text.strip().capitalize() if tds else None
        this = meal_header

        while True:
            this = this.getnext()
            if this is None:
                break
            if this.attrib.get("class") is not None:
                break

            columns = this.findall("td")
            if not columns:
                continue

            # Entry ID is on the <a> tag's data-food-entry-id attribute in the first column
            a_tag = columns[0].find(".//a[@data-food-entry-id]")
            if a_tag is None:
                continue

            entry_id = a_tag.get("data-food-entry-id")
            name = a_tag.text.strip() if a_tag.text else columns[0].text_content().strip()

            entries.append({
                "entry_id": entry_id,
                "name": name,
                "meal": current_meal or "Unknown",
            })

    return entries


def raw_delete_food_entry(
    session: req_lib.Session,
    entry_id: str,
    date_str: str,
    username: str,
) -> None:
    """
    Delete a food diary entry using the MFP web form endpoint.
    Uses Rails-style DELETE-via-POST: POST /food/remove/{entry_id}
    with _method=delete and authenticity_token.

    Raises RuntimeError if the delete could not be confirmed.
    """
    # Fetch diary page for CSRF token
    diary_url = f"https://www.myfitnesspal.com/food/diary/{username}" if username else "https://www.myfitnesspal.com/food/diary"
    r = session.get(diary_url, params={"date": date_str})
    r.raise_for_status()
    doc = lxml.html.document_fromstring(r.text)

    # Rails UJS-style deletes use the page-level CSRF token from the meta tag.
    # Hidden form tokens on the page are not interchangeable here and can
    # trigger a login redirect instead of a real delete.
    tokens = doc.xpath("//meta[@name='csrf-token']/@content")
    if not tokens:
        tokens = doc.xpath("//input[@name='authenticity_token']/@value")
    if not tokens:
        raise RuntimeError("Could not find CSRF token on diary page")
    csrf = tokens[0]
    referer = r.url

    r = session.post(
        f"https://www.myfitnesspal.com/food/remove/{entry_id}",
        data={
            "_method": "delete",
            "authenticity_token": csrf,
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://www.myfitnesspal.com",
            "Referer": referer,
        },
        allow_redirects=False,
    )

    # Log response details for debugging
    redirect_chain = [(resp.status_code, resp.headers.get("Location", "")) for resp in r.history]
    logger.info(
        f"Delete entry_id={entry_id}: final_status={r.status_code}, "
        f"redirects={redirect_chain}, final_url={r.url}"
    )

    if r.status_code != 302:
        logger.warning(
            f"Delete entry_id={entry_id}: expected 302 redirect but got "
            f"status={r.status_code}, redirects={redirect_chain}, "
            f"response_snippet={r.text[:500]}"
        )
        r.raise_for_status()

    location = r.headers.get("Location", "")
    if "/account/login" in location:
        raise RuntimeError(
            "Delete request redirected to login, which usually means the "
            "session or CSRF token was rejected. Refresh cookies and try again."
        )

    # Verify the entry was actually removed
    post_entries = raw_get_diary_entries(session, date_str, username)
    still_exists = any(e["entry_id"] == entry_id for e in post_entries)
    if still_exists:
        raise RuntimeError(
            f"Delete appeared to succeed (status={r.status_code}, "
            f"redirects={redirect_chain}) but entry {entry_id} is still "
            f"present in diary. Response snippet: {r.text[:300]}"
        )


# ============================================================================
# Data Formatting Helper Functions
# ============================================================================


def parse_date(date_str: Optional[str] = None) -> date:
    """
    Parse a date string or return today's date.

    Args:
        date_str: Date in YYYY-MM-DD format, or None for today

    Returns:
        date: Parsed date object
    """
    if date_str is None:
        return date.today()
    return datetime.strptime(date_str, "%Y-%m-%d").date()


def format_nutrition_dict(nutrition: Dict[str, Any]) -> Dict[str, Any]:
    """
    Format nutrition dictionary for consistent output.

    Args:
        nutrition: Raw nutrition dictionary

    Returns:
        dict: Formatted nutrition data
    """
    formatted = {}
    for key, value in nutrition.items():
        if hasattr(value, "magnitude"):
            # Handle pint quantities
            formatted[key] = float(value.magnitude)
        else:
            formatted[key] = value
    return formatted


def format_meal_entry(entry) -> Dict[str, Any]:
    """
    Format a meal entry for output.

    Args:
        entry: MFP Entry object

    Returns:
        dict: Formatted entry data
    """
    return {
        "name": entry.name,
        "short_name": getattr(entry, "short_name", None),
        "quantity": getattr(entry, "quantity", None),
        "unit": getattr(entry, "unit", None),
        "nutrition": format_nutrition_dict(entry.totals),
    }


def format_exercise(exercise) -> Dict[str, Any]:
    """
    Format an exercise object for output.

    Args:
        exercise: MFP Exercise object

    Returns:
        dict: Formatted exercise data
    """
    entries = exercise.get_as_list()
    return {"name": exercise.name, "entries": entries}


def ordered_dict_to_dict(od: OrderedDict) -> Dict[str, Any]:
    """
    Convert OrderedDict with date keys to regular dict with string keys.

    Args:
        od: OrderedDict with date keys

    Returns:
        dict: Regular dict with string keys
    """
    return {str(k): v for k, v in od.items()}


def calculate_day_totals(day) -> Dict[str, float]:
    """
    Aggregate nutrition totals for a day from its entries.

    Args:
        day: MFP Day object

    Returns:
        dict: Daily nutrition totals
    """
    totals: Dict[str, float] = {}
    for entry in day.entries:
        for key, value in entry.totals.items():
            val = float(value.magnitude) if hasattr(value, "magnitude") else float(value)
            totals[key] = totals.get(key, 0.0) + val
    return totals


def calculate_day_exercise_burn(day) -> float:
    """
    Sum calories burned from the day's logged exercises.

    Args:
        day: MFP Day object

    Returns:
        float: Calories burned
    """
    total_burned = 0.0
    for exercise in day.exercises:
        for entry in exercise.get_as_list():
            if "nutrition_information" in entry:
                total_burned += float(
                    entry["nutrition_information"].get("calories burned", 0) or 0
                )
    return total_burned


def normalize_report_name(report_name: str) -> str:
    """Normalize report names for diary-derived fallback handling."""
    return " ".join(report_name.strip().lower().split())


def build_report_from_diary(client, report_name: str, start: date, end: date) -> Optional[OrderedDict]:
    """
    Build supported nutrition reports from diary data when MFP's report endpoint fails.

    Args:
        client: Authenticated MFP client
        report_name: Requested report name
        start: Inclusive start date
        end: Inclusive end date

    Returns:
        OrderedDict | None: Report values keyed by date, or None if unsupported
    """
    normalized = normalize_report_name(report_name)
    nutrient_map = {
        "protein": "protein",
        "fat": "fat",
        "carbs": "carbohydrates",
        "carbohydrates": "carbohydrates",
        "total calories": "calories",
    }

    # Key mapping for "all" report output (internal key -> display name)
    all_display_names = {
        "calories": "calories",
        "protein": "protein",
        "carbohydrates": "carbs",
        "fat": "fat",
    }

    values: OrderedDict = OrderedDict()
    current = start

    if normalized == "all":
        while current <= end:
            day = client.get_date(current)
            totals = calculate_day_totals(day)
            exercise_burn = calculate_day_exercise_burn(day)
            day_values = {
                all_display_names[k]: round(totals.get(k, 0.0), 1)
                for k in all_display_names
            }
            day_values["net_calories"] = round(day_values["calories"] - exercise_burn, 1)
            values[current] = day_values
            current += timedelta(days=1)
        return values

    if normalized == "net calories":
        while current <= end:
            day = client.get_date(current)
            totals = calculate_day_totals(day)
            values[current] = totals.get("calories", 0.0) - calculate_day_exercise_burn(day)
            current += timedelta(days=1)
        return values

    nutrient_key = nutrient_map.get(normalized)
    if not nutrient_key:
        return None

    while current <= end:
        day = client.get_date(current)
        totals = calculate_day_totals(day)
        values[current] = totals.get(nutrient_key, 0.0)
        current += timedelta(days=1)

    return values


class ResponseFormat(str, Enum):
    """Output format for tool responses."""

    MARKDOWN = "markdown"
    JSON = "json"


def format_response(data: Any, format_type: ResponseFormat, title: str = "") -> str:
    """
    Format response data based on requested format.

    Args:
        data: Data to format
        format_type: Output format (markdown or json)
        title: Optional title for markdown format

    Returns:
        str: Formatted response string
    """
    if format_type == ResponseFormat.JSON:
        return json.dumps(data, indent=2, default=str)

    # Markdown format
    lines = []
    if title:
        lines.append(f"## {title}\n")

    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, dict):
                lines.append(f"### {key}")
                for k, v in value.items():
                    lines.append(f"- **{k}**: {v}")
            elif isinstance(value, list):
                lines.append(f"### {key}")
                for item in value:
                    if isinstance(item, dict):
                        lines.append(f"- {item.get('name', str(item))}")
                        for k, v in item.items():
                            if k != "name":
                                lines.append(f"  - {k}: {v}")
                    else:
                        lines.append(f"- {item}")
            else:
                lines.append(f"- **{key}**: {value}")
    else:
        lines.append(str(data))

    return "\n".join(lines)


# ============================================================================
# Pydantic Input Models
# ============================================================================


class GetDiaryInput(BaseModel):
    """Input model for getting food diary."""

    model_config = ConfigDict(str_strip_whitespace=True)

    date: Optional[str] = Field(
        default=None,
        description="Date in YYYY-MM-DD format. Defaults to today if not specified.",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' for human-readable or 'json' for structured data",
    )


class SearchFoodInput(BaseModel):
    """Input model for searching foods."""

    model_config = ConfigDict(str_strip_whitespace=True)

    query: str = Field(
        ...,
        description="Search query for food items (e.g., 'chicken breast', 'apple')",
        min_length=1,
        max_length=200,
    )
    limit: int = Field(
        default=10,
        description="Maximum number of results to return",
        ge=1,
        le=50,
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' for human-readable or 'json' for structured data",
    )


class GetFoodDetailsInput(BaseModel):
    """Input model for getting food item details."""

    model_config = ConfigDict(str_strip_whitespace=True)

    mfp_id: str = Field(
        ...,
        description="MyFitnessPal food item ID (obtained from search results)",
        min_length=1,
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' for human-readable or 'json' for structured data",
    )


class GetMeasurementsInput(BaseModel):
    """Input model for getting measurements."""

    model_config = ConfigDict(str_strip_whitespace=True)

    measurement: str = Field(
        default="Weight",
        description="Type of measurement to retrieve (e.g., 'Weight', 'Body Fat', 'Waist')",
    )
    start_date: Optional[str] = Field(
        default=None,
        description="Start date in YYYY-MM-DD format. Defaults to 30 days ago.",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    )
    end_date: Optional[str] = Field(
        default=None,
        description="End date in YYYY-MM-DD format. Defaults to today.",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' for human-readable or 'json' for structured data",
    )


class SetMeasurementInput(BaseModel):
    """Input model for setting a measurement."""

    model_config = ConfigDict(str_strip_whitespace=True)

    measurement: str = Field(
        default="Weight",
        description="Type of measurement to set (e.g., 'Weight', 'Body Fat', 'Waist')",
    )
    value: float = Field(
        ...,
        description="Measurement value (e.g., 185.5 for weight in lbs)",
        gt=0,
    )


class GetExercisesInput(BaseModel):
    """Input model for getting exercises."""

    model_config = ConfigDict(str_strip_whitespace=True)

    date: Optional[str] = Field(
        default=None,
        description="Date in YYYY-MM-DD format. Defaults to today if not specified.",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' for human-readable or 'json' for structured data",
    )


class GetGoalsInput(BaseModel):
    """Input model for getting nutrition goals."""

    model_config = ConfigDict(str_strip_whitespace=True)

    date: Optional[str] = Field(
        default=None,
        description="Date in YYYY-MM-DD format. Defaults to today if not specified.",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' for human-readable or 'json' for structured data",
    )


class SetGoalsInput(BaseModel):
    """Input model for setting nutrition goals."""

    model_config = ConfigDict(str_strip_whitespace=True)

    calories: Optional[int] = Field(
        default=None,
        description="Daily calorie goal (e.g., 2000)",
        ge=500,
        le=10000,
    )
    protein: Optional[int] = Field(
        default=None,
        description="Daily protein goal in grams (e.g., 150)",
        ge=0,
        le=1000,
    )
    carbohydrates: Optional[int] = Field(
        default=None,
        description="Daily carbohydrate goal in grams (e.g., 200)",
        ge=0,
        le=2000,
    )
    fat: Optional[int] = Field(
        default=None,
        description="Daily fat goal in grams (e.g., 65)",
        ge=0,
        le=500,
    )


# class GetWaterInput(BaseModel):
#     """Input model for getting water intake."""
#
#     model_config = ConfigDict(str_strip_whitespace=True)
#
#     date: Optional[str] = Field(
#         default=None,
#         description="Date in YYYY-MM-DD format. Defaults to today if not specified.",
#         pattern=r"^\d{4}-\d{2}-\d{2}$",
#     )


class GetReportInput(BaseModel):
    """Input model for getting nutrition reports."""

    model_config = ConfigDict(str_strip_whitespace=True)

    report_name: str = Field(
        default="All",
        description="Report name: 'All' (default, all macros), 'Net Calories', 'Total Calories', 'Protein', 'Fat', or 'Carbs'",
    )
    start_date: Optional[str] = Field(
        default=None,
        description="Start date in YYYY-MM-DD format. Defaults to 7 days ago.",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    )
    end_date: Optional[str] = Field(
        default=None,
        description="End date in YYYY-MM-DD format. Defaults to today.",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' for human-readable or 'json' for structured data",
    )


class AddFoodToDiaryInput(BaseModel):
    """Input model for adding food to diary."""

    model_config = ConfigDict(str_strip_whitespace=True)

    food_id: str = Field(
        ...,
        description="MyFitnessPal food_id (obtained from mfp_search_food results)",
        min_length=1,
    )
    serving_id: str = Field(
        ...,
        description="Serving size ID (obtained from mfp_search_food results, e.g. the serving_id field)",
        min_length=1,
    )
    meal: str = Field(
        default="Breakfast",
        description="Meal name (e.g., 'Breakfast', 'Lunch', 'Dinner', 'Snacks')",
    )
    date: Optional[str] = Field(
        default=None,
        description="Date in YYYY-MM-DD format. Defaults to today if not specified.",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    )
    quantity: float = Field(
        default=1.0,
        description="Quantity/servings (e.g., 1.5 for 1.5 servings)",
        gt=0,
        le=100,
    )


class DeleteFoodFromDiaryInput(BaseModel):
    """Input model for deleting a food entry from the diary."""

    model_config = ConfigDict(str_strip_whitespace=True)

    entry_id: str = Field(
        ...,
        description="The diary entry ID to delete (obtain from mfp_get_diary which includes entry_id per food item)",
        min_length=1,
    )
    date: Optional[str] = Field(
        default=None,
        description="Date in YYYY-MM-DD format. Must match the date the entry belongs to. Defaults to today.",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    )


class RefreshCookiesInput(BaseModel):
    """Input model for refreshing auth cookies."""

    model_config = ConfigDict(str_strip_whitespace=True)

    browser: str = Field(
        default="auto",
        description="Browser to import from: auto, brave, chrome, chromium, or firefox",
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' for human-readable or 'json' for structured data",
    )


# class SetWaterInput(BaseModel):
#     """Input model for setting water intake."""
#
#     model_config = ConfigDict(str_strip_whitespace=True)
#
#     cups: float = Field(
#         ...,
#         description="Number of cups of water (e.g., 2.5 for 2.5 cups). Note: MyFitnessPal uses cups as the unit.",
#         ge=0,
#         le=50,
#     )
#     date: Optional[str] = Field(
#         default=None,
#         description="Date in YYYY-MM-DD format. Defaults to today if not specified.",
#         pattern=r"^\d{4}-\d{2}-\d{2}$",
#     )


# ============================================================================
# MCP Tools
# ============================================================================


@mcp.tool(
    name="mfp_refresh_cookies",
    annotations={
        "title": "Refresh Auth Cookies",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def mfp_refresh_cookies(params: RefreshCookiesInput) -> str:
    """
    Refresh stored MyFitnessPal cookies from Brave/Chrome/Firefox or the Netscape export file.
    """
    try:
        browser = params.browser.lower()
        preferred_browser = None if browser == "auto" else browser

        cookie_records = None
        source = None

        imported, imported_browser = import_browser_cookies(preferred_browser)
        if imported and imported_browser:
            cookie_records = imported
            source = f"browser:{imported_browser}"
        else:
            imported = import_netscape_cookies()
            if imported:
                cookie_records = imported
                source = f"netscape:{NETSCAPE_COOKIES_FILE}"

        if not cookie_records or not source:
            raise RuntimeError(
                "Could not refresh cookies from a live browser session or the Netscape export."
            )

        save_cookies(cookie_records, source=source)
        data = summarize_cookie_records(cookie_records)
        data["source"] = source

        return format_response(data, params.response_format, "MyFitnessPal Cookie Refresh")

    except Exception as e:
        return f"Error refreshing cookies: {str(e)}"


@mcp.tool(
    name="mfp_get_diary",
    annotations={
        "title": "Get Food Diary",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def mfp_get_diary(params: GetDiaryInput) -> str:
    """
    Get the food diary for a specific date including all meals and their nutritional information.

    Returns meals (Breakfast, Lunch, Dinner, Snacks) with each food entry's name,
    quantity, and complete nutrition breakdown (calories, protein, carbs, fat, etc.).
    Also includes daily totals and goals.

    Args:
        params: GetDiaryInput containing:
            - date (str, optional): Date in YYYY-MM-DD format, defaults to today
            - response_format (str): 'markdown' or 'json'

    Returns:
        str: Formatted diary data with meals, entries, nutrition, and goals
    """
    try:
        client = get_mfp_client()
        target_date = parse_date(params.date)
        day = client.get_date(target_date)

        # Fetch entry IDs via raw scraping (library discards these)
        id_by_position = {}
        try:
            session = get_raw_session()
            date_str = target_date.strftime("%Y-%m-%d")
            username = client.effective_username
            raw_entries = raw_get_diary_entries(session, date_str, username)
            from collections import defaultdict
            meal_counters = defaultdict(int)
            for e in raw_entries:
                meal_key = e["meal"].lower()
                idx = meal_counters[meal_key]
                id_by_position.setdefault(meal_key, {})[idx] = e["entry_id"]
                meal_counters[meal_key] = idx + 1
        except Exception:
            logger.debug("Failed to fetch entry IDs via raw scraping, entries will lack entry_id")

        # Build response data
        data = {
            "date": str(target_date),
            "meals": {},
            "daily_totals": {},
            "daily_goals": {},
            "water": day.water,
            "notes": day.notes or "",
        }

        # Process meals
        for meal in day.meals:
            entries_formatted = []
            for i, entry in enumerate(meal.entries):
                formatted = format_meal_entry(entry)
                eid = id_by_position.get(meal.name.lower(), {}).get(i)
                if eid:
                    formatted["entry_id"] = eid
                entries_formatted.append(formatted)
            meal_data = {
                "entries": entries_formatted,
                "totals": format_nutrition_dict(meal.totals),
            }
            data["meals"][meal.name] = meal_data

        # Get daily totals and goals
        data["daily_totals"] = calculate_day_totals(day)
        data["daily_goals"] = day.goals

        return format_response(
            data, params.response_format, f"Food Diary for {target_date}"
        )

    except Exception as e:
        return f"Error retrieving diary: {str(e)}"


@mcp.tool(
    name="mfp_search_food",
    annotations={
        "title": "Search Food Database",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def mfp_search_food(params: SearchFoodInput) -> str:
    """
    Search the MyFitnessPal food database for food items.

    Returns a list of matching foods with their name, brand, serving size,
    calories, and MFP ID (which can be used with mfp_get_food_details).

    Args:
        params: SearchFoodInput containing:
            - query (str): Search query (e.g., 'chicken breast')
            - limit (int): Maximum results to return (default 10)
            - response_format (str): 'markdown' or 'json'

    Returns:
        str: List of matching food items with basic nutrition info
    """
    try:
        session = get_raw_session()
        date_str = date.today().strftime("%Y-%m-%d")
        results = raw_search_foods(session, params.query, date_str, params.limit)

        data = {"query": params.query, "count": len(results), "results": results}

        return format_response(
            data, params.response_format, f"Food Search Results for '{params.query}'"
        )

    except Exception as e:
        return f"Error searching foods: {str(e)}"


@mcp.tool(
    name="mfp_get_food_details",
    annotations={
        "title": "Get Food Item Details",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def mfp_get_food_details(params: GetFoodDetailsInput) -> str:
    """
    Get detailed nutritional information for a specific food item by its MFP ID.

    Returns complete nutrition breakdown including calories, macros (protein, carbs, fat),
    fiber, sugar, sodium, cholesterol, vitamins, minerals, and available serving sizes.

    Args:
        params: GetFoodDetailsInput containing:
            - mfp_id (str): MyFitnessPal food item ID from search results
            - response_format (str): 'markdown' or 'json'

    Returns:
        str: Complete nutritional information for the food item
    """
    try:
        client = get_mfp_client()
        item = client.get_food_item_details(params.mfp_id)

        data = {
            "mfp_id": params.mfp_id,
            "description": getattr(item, "description", "N/A"),
            "brand_name": getattr(item, "brand_name", None),
            "verified": getattr(item, "verified", False),
            "calories": getattr(item, "calories", None),
            "nutrition": {
                "protein": getattr(item, "protein", None),
                "carbohydrates": getattr(item, "carbohydrates", None),
                "fat": getattr(item, "fat", None),
                "fiber": getattr(item, "fiber", None),
                "sugar": getattr(item, "sugar", None),
                "sodium": getattr(item, "sodium", None),
                "cholesterol": getattr(item, "cholesterol", None),
                "saturated_fat": getattr(item, "saturated_fat", None),
                "polyunsaturated_fat": getattr(item, "polyunsaturated_fat", None),
                "monounsaturated_fat": getattr(item, "monounsaturated_fat", None),
                "trans_fat": getattr(item, "trans_fat", None),
                "potassium": getattr(item, "potassium", None),
                "vitamin_a": getattr(item, "vitamin_a", None),
                "vitamin_c": getattr(item, "vitamin_c", None),
                "calcium": getattr(item, "calcium", None),
                "iron": getattr(item, "iron", None),
            },
            "servings": [],
        }

        # Get serving sizes if available
        if hasattr(item, "servings"):
            for serving in item.servings:
                data["servings"].append(str(serving))

        return format_response(data, params.response_format, "Food Item Details")

    except Exception as e:
        return f"Error getting food details: {str(e)}"


@mcp.tool(
    name="mfp_get_measurements",
    annotations={
        "title": "Get Body Measurements",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def mfp_get_measurements(params: GetMeasurementsInput) -> str:
    """
    Get body measurements (weight, body fat, etc.) over a date range.

    Returns historical measurement data with dates and values. Useful for
    tracking weight loss progress and body composition changes.

    Args:
        params: GetMeasurementsInput containing:
            - measurement (str): Type of measurement (default 'Weight')
            - start_date (str, optional): Start date, defaults to 30 days ago
            - end_date (str, optional): End date, defaults to today
            - response_format (str): 'markdown' or 'json'

    Returns:
        str: Measurement history with dates and values
    """
    try:
        client = get_mfp_client()

        end = parse_date(params.end_date)
        if params.start_date:
            start = parse_date(params.start_date)
        else:
            start = end - timedelta(days=30)

        measurements = client.get_measurements(params.measurement, start, end)

        data = {
            "measurement_type": params.measurement,
            "start_date": str(start),
            "end_date": str(end),
            "count": len(measurements),
            "values": ordered_dict_to_dict(measurements),
        }

        # Calculate summary stats if we have data
        if measurements:
            values = list(measurements.values())
            data["summary"] = {
                "latest": values[-1] if values else None,
                "earliest": values[0] if values else None,
                "change": round(values[-1] - values[0], 2) if len(values) >= 2 else 0,
                "min": min(values),
                "max": max(values),
                "average": round(sum(values) / len(values), 2),
            }

        return format_response(
            data, params.response_format, f"{params.measurement} History"
        )

    except Exception as e:
        return f"Error getting measurements: {str(e)}"


@mcp.tool(
    name="mfp_set_measurement",
    annotations={
        "title": "Log Body Measurement",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def mfp_set_measurement(params: SetMeasurementInput) -> str:
    """
    Log a new body measurement (weight, body fat, etc.) for today.

    Records the measurement value in MyFitnessPal for tracking progress.

    Args:
        params: SetMeasurementInput containing:
            - measurement (str): Type of measurement (default 'Weight')
            - value (float): Measurement value (e.g., 185.5)

    Returns:
        str: Confirmation message with the logged value
    """
    try:
        client = get_mfp_client()
        client.set_measurements(params.measurement, params.value)

        return json.dumps(
            {
                "success": True,
                "message": f"Successfully logged {params.measurement}: {params.value}",
                "measurement": params.measurement,
                "value": params.value,
                "date": str(date.today()),
            },
            indent=2,
        )

    except Exception as e:
        return f"Error setting measurement: {str(e)}"


@mcp.tool(
    name="mfp_get_exercises",
    annotations={
        "title": "Get Exercise Log",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def mfp_get_exercises(params: GetExercisesInput) -> str:
    """
    Get logged exercises for a specific date.

    Returns both cardiovascular and strength training exercises with their
    details (duration, calories burned, sets, reps, weight, etc.).

    Args:
        params: GetExercisesInput containing:
            - date (str, optional): Date in YYYY-MM-DD format, defaults to today
            - response_format (str): 'markdown' or 'json'

    Returns:
        str: List of exercises with details and calories burned
    """
    try:
        client = get_mfp_client()
        target_date = parse_date(params.date)
        day = client.get_date(target_date)

        data = {"date": str(target_date), "exercises": []}

        for exercise in day.exercises:
            data["exercises"].append(format_exercise(exercise))

        # Calculate total calories burned
        data["total_calories_burned"] = calculate_day_exercise_burn(day)

        return format_response(
            data, params.response_format, f"Exercise Log for {target_date}"
        )

    except Exception as e:
        return f"Error getting exercises: {str(e)}"


@mcp.tool(
    name="mfp_get_goals",
    annotations={
        "title": "Get Nutrition Goals",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def mfp_get_goals(params: GetGoalsInput) -> str:
    """
    Get the user's daily nutrition goals (calories, protein, carbs, fat, etc.).

    Returns the configured daily targets for all tracked nutrients.

    Args:
        params: GetGoalsInput containing:
            - date (str, optional): Date in YYYY-MM-DD format, defaults to today
            - response_format (str): 'markdown' or 'json'

    Returns:
        str: Daily nutrition goals and targets
    """
    try:
        client = get_mfp_client()
        target_date = parse_date(params.date)
        day = client.get_date(target_date)

        data = {"date": str(target_date), "goals": day.goals}

        return format_response(data, params.response_format, "Daily Nutrition Goals")

    except Exception as e:
        return f"Error getting goals: {str(e)}"


@mcp.tool(
    name="mfp_set_goals",
    annotations={
        "title": "Update Nutrition Goals",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def mfp_set_goals(params: SetGoalsInput) -> str:
    """
    Update daily nutrition goals (calories, protein, carbs, fat).

    Sets new daily targets for the specified nutrients. Only updates the
    values that are provided; others remain unchanged.

    Args:
        params: SetGoalsInput containing:
            - calories (int, optional): Daily calorie goal
            - protein (int, optional): Daily protein goal in grams
            - carbohydrates (int, optional): Daily carb goal in grams
            - fat (int, optional): Daily fat goal in grams

    Returns:
        str: Confirmation message with updated goals
    """
    try:
        # Check that at least one goal is provided
        if not any(
            [params.calories, params.protein, params.carbohydrates, params.fat]
        ):
            return "Error: Please provide at least one goal to update (calories, protein, carbohydrates, or fat)"

        client = get_mfp_client()

        # Build kwargs for set_new_goal
        kwargs = {}
        if params.calories:
            kwargs["energy"] = params.calories
        if params.protein:
            kwargs["protein"] = params.protein
        if params.carbohydrates:
            kwargs["carbohydrates"] = params.carbohydrates
        if params.fat:
            kwargs["fat"] = params.fat

        client.set_new_goal(**kwargs)

        return json.dumps(
            {
                "success": True,
                "message": "Successfully updated nutrition goals",
                "updated_goals": {
                    "calories": params.calories,
                    "protein": params.protein,
                    "carbohydrates": params.carbohydrates,
                    "fat": params.fat,
                },
            },
            indent=2,
        )

    except Exception as e:
        return f"Error setting goals: {str(e)}"


# @mcp.tool(
#     name="mfp_get_water",
#     annotations={
#         "title": "Get Water Intake",
#         "readOnlyHint": True,
#         "destructiveHint": False,
#         "idempotentHint": True,
#         "openWorldHint": True,
#     },
# )
# async def mfp_get_water(params: GetWaterInput) -> str:
#     """
#     Get water intake for a specific date.
#
#     Returns the number of cups/glasses of water logged for the day.
#     """
#     try:
#         client = get_mfp_client()
#         target_date = parse_date(params.date)
#         day = client.get_date(target_date)
#
#         data = {
#             "date": str(target_date),
#             "water_cups": day.water,
#             "water_ml": day.water * 236.588,
#         }
#
#         return json.dumps(data, indent=2)
#
#     except Exception as e:
#         return f"Error getting water intake: {str(e)}"


@mcp.tool(
    name="mfp_add_food_to_diary",
    annotations={
        "title": "Add Food to Diary",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def mfp_add_food_to_diary(params: AddFoodToDiaryInput) -> str:
    """
    Add a food item to your MyFitnessPal food diary for a specific date and meal.

    This tool adds a food entry to your diary. You can search for foods using
    mfp_search_food to find the food_id and serving_id needed for this tool.

    Args:
        params: AddFoodToDiaryInput containing:
            - food_id (str): MyFitnessPal food_id (from mfp_search_food)
            - serving_id (str): Serving size ID (from mfp_search_food)
            - meal (str): Meal name - 'Breakfast', 'Lunch', 'Dinner', or 'Snacks' (default: 'Breakfast')
            - date (str, optional): Date in YYYY-MM-DD format, defaults to today
            - quantity (float): Number of servings (default: 1.0)

    Returns:
        str: Confirmation message with details of the added food entry
    """
    try:
        session = get_raw_session()
        target_date = parse_date(params.date)
        date_str = target_date.strftime("%Y-%m-%d")

        # Normalize meal name
        meal = params.meal.strip().capitalize()
        if meal.lower() == "snack":
            meal = "Snacks"

        meal_map = {
            "breakfast": "0", "lunch": "1",
            "dinner": "2", "snacks": "3",
        }
        meal_index = meal_map.get(meal.lower(), "0")

        raw_add_food(
            session=session,
            food_id=params.food_id,
            serving_id=params.serving_id,
            date_str=date_str,
            meal_index=meal_index,
            quantity=params.quantity,
        )

        return json.dumps(
            {
                "success": True,
                "message": f"Successfully added food to {meal}",
                "date": date_str,
                "meal": meal,
                "food_id": params.food_id,
                "serving_id": params.serving_id,
                "quantity": params.quantity,
            },
            indent=2,
        )

    except Exception as e:
        return f"Error adding food to diary: {str(e)}"


@mcp.tool(
    name="mfp_delete_food_from_diary",
    annotations={
        "title": "Delete Food from Diary",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def mfp_delete_food_from_diary(params: DeleteFoodFromDiaryInput) -> str:
    """
    Delete a food entry from your MyFitnessPal food diary.

    Use mfp_get_diary to see entries with their entry_id, then pass the
    entry_id here to delete that specific entry.

    Args:
        params: DeleteFoodFromDiaryInput containing:
            - entry_id (str): The diary entry ID (from mfp_get_diary output)
            - date (str, optional): Date in YYYY-MM-DD format, defaults to today

    Returns:
        str: Confirmation message with details of the deleted food entry
    """
    try:
        session = get_raw_session()
        target_date = parse_date(params.date)
        date_str = target_date.strftime("%Y-%m-%d")

        # Use empty username so raw functions hit /food/diary (cookie-authed)
        username = ""

        # Verify entry exists and get its name for confirmation
        deleted_name = None
        deleted_meal = None
        try:
            diary_entries = raw_get_diary_entries(session, date_str, username)
            for entry in diary_entries:
                if entry["entry_id"] == params.entry_id:
                    deleted_name = entry["name"]
                    deleted_meal = entry["meal"]
                    break
            if deleted_name is None:
                return json.dumps({
                    "success": False,
                    "message": f"Entry ID '{params.entry_id}' not found in diary for {date_str}",
                }, indent=2)
        except Exception:
            pass  # proceed with delete anyway

        raw_delete_food_entry(
            session=session,
            entry_id=params.entry_id,
            date_str=date_str,
            username=username,
        )

        msg = "Successfully deleted"
        if deleted_name:
            msg += f" '{deleted_name}'"
        if deleted_meal:
            msg += f" from {deleted_meal}"

        return json.dumps(
            {
                "success": True,
                "message": msg,
                "date": date_str,
                "entry_id": params.entry_id,
                "deleted_food": deleted_name,
                "meal": deleted_meal,
            },
            indent=2,
        )

    except Exception as e:
        return f"Error deleting food from diary: {str(e)}"


# @mcp.tool(
#     name="mfp_set_water",
#     annotations={
#         "title": "Log Water Intake",
#         "readOnlyHint": False,
#         "destructiveHint": False,
#         "idempotentHint": False,
#         "openWorldHint": True,
#     },
# )
# async def mfp_set_water(params: SetWaterInput) -> str:
#     """Log water intake for a specific date using raw HTML scraping."""
#     try:
#         session = get_raw_session()
#         target_date = parse_date(params.date)
#         date_str = target_date.strftime("%Y-%m-%d")
#
#         # Get diary page for CSRF token
#         diary_url = "https://www.myfitnesspal.com/food/diary"
#         r = session.get(diary_url, params={"date": date_str})
#         r.raise_for_status()
#         doc = lxml.html.document_fromstring(r.text)
#
#         tokens = doc.xpath("//input[@name='authenticity_token']/@value")
#         if not tokens:
#             raise RuntimeError("Could not find CSRF token on diary page")
#
#         # Extract username from diary page URL or meta
#         username_el = doc.xpath("//h1[@class='main-title-2']/@data-user-name")
#         username = username_el[0] if username_el else ""
#
#         water_url = f"https://www.myfitnesspal.com/food/diary/{username}/water"
#         r = session.post(
#             water_url,
#             data={
#                 "authenticity_token": tokens[0],
#                 "date": date_str,
#                 "water": str(params.cups),
#             },
#             headers={
#                 "Referer": diary_url,
#                 "Content-Type": "application/x-www-form-urlencoded",
#                 "X-Requested-With": "XMLHttpRequest",
#             },
#         )
#         r.raise_for_status()
#
#         return json.dumps(
#             {
#                 "success": True,
#                 "message": f"Successfully logged {params.cups} cups of water",
#                 "date": date_str,
#                 "cups": params.cups,
#                 "milliliters": round(params.cups * 236.588, 2),
#             },
#             indent=2,
#         )
#
#     except Exception as e:
#         return f"Error setting water intake: {str(e)}"


@mcp.tool(
    name="mfp_get_report",
    annotations={
        "title": "Get Nutrition Report",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def mfp_get_report(params: GetReportInput) -> str:
    """
    Get a nutrition report over a date range.

    Returns daily values for the specified nutrient/metric over the date range.
    Useful for analyzing trends and patterns in nutrition intake.

    Args:
        params: GetReportInput containing:
            - report_name (str): 'All' (default, all macros), 'Net Calories', 'Protein', etc.
            - start_date (str, optional): Start date, defaults to 7 days ago
            - end_date (str, optional): End date, defaults to today
            - response_format (str): 'markdown' or 'json'

    Returns:
        str: Daily values and summary statistics for the report period
    """
    try:
        client = get_mfp_client()

        end = parse_date(params.end_date)
        if params.start_date:
            start = parse_date(params.start_date)
        else:
            start = end - timedelta(days=7)

        source = "report_api"
        try:
            report = client.get_report(
                report_name=params.report_name,
                report_category="Nutrition",
                lower_bound=start,
                upper_bound=end,
            )
        except Exception as report_error:
            report = build_report_from_diary(client, params.report_name, start, end)
            if report is None:
                raise report_error
            source = "diary_fallback"
            logger.warning(
                "Falling back to diary-derived report data for %s (%s)",
                params.report_name,
                report_error,
            )

        data = {
            "report_name": params.report_name,
            "start_date": str(start),
            "end_date": str(end),
            "source": source,
            "values": (
                ordered_dict_to_dict(report) if isinstance(report, OrderedDict) else report
            ),
        }

        # Calculate summary stats
        if report:
            values = list(report.values())
            if values and isinstance(values[0], dict):
                # "All" report: per-metric summaries
                metrics = values[0].keys()
                data["summary"] = {}
                for metric in metrics:
                    metric_vals = [v[metric] for v in values if isinstance(v.get(metric), (int, float))]
                    if metric_vals:
                        data["summary"][metric] = {
                            "total": round(sum(metric_vals), 1),
                            "average": round(sum(metric_vals) / len(metric_vals), 1),
                            "min": min(metric_vals),
                            "max": max(metric_vals),
                        }
            else:
                numeric_values = [v for v in values if isinstance(v, (int, float))]
                if numeric_values:
                    data["summary"] = {
                        "total": sum(numeric_values),
                        "average": round(sum(numeric_values) / len(numeric_values), 2),
                        "min": min(numeric_values),
                        "max": max(numeric_values),
                    }

        return format_response(
            data, params.response_format, f"{params.report_name} Report"
        )

    except Exception as e:
        return f"Error getting report: {str(e)}"


# ============================================================================
# Main Entry Point
# ============================================================================


def main():
    """Run the MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
