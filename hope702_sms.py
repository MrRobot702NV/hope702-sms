#!/usr/bin/env python3
"""
HOPE 702 SMS Webhook
Twilio-powered SMS resource bot for Las Vegas homeless services.
Runs on port 5702.
"""

import hashlib
import hmac
import json
import re
import logging
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import gspread
from flask import Flask, Response, request
from google.oauth2.service_account import Credentials
from twilio.twiml.messaging_response import MessagingResponse

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(Path(__file__).parent / "hope702_sms.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

SHEET_NAME       = os.getenv("HOPE702_SHEET_NAME", "HOPE 702 Resource Database")
CREDS_FILE       = os.getenv("HOPE702_CREDS_FILE", str(Path(__file__).parent / "creds.json"))

# Durable storage. On Railway this points at a mounted volume (set
# HOPE702_DATA_DIR=/data), so the demand log survives redeploys — the container
# filesystem does not. Falls back to the source directory for local runs.
DATA_DIR         = Path(os.getenv("HOPE702_DATA_DIR", str(Path(__file__).parent)))
DEMAND_DB_FILE   = DATA_DIR / "hope702_activity.db"
ALLOWED_ORIGIN   = os.getenv("HOPE702_ALLOWED_ORIGIN", "https://hope702.org")
SCOPES           = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive.readonly"]
PAGE_SIZE        = 3
SESSION_TIMEOUT  = 1800  # 30 minutes in seconds

# ── Resource dataclass ────────────────────────────────────────────────────────

@dataclass
class Resource:
    name: str
    category: str
    address: str = ""
    zip_code: str = ""
    phone: str = ""
    hours: str = ""
    pets: str = ""
    categories: str = ""
    shelter_type: str = ""
    notes: str = ""
    eligibility: str = ""


# ── User session tracking (for MORE pagination) ───────────────────────────────

@dataclass
class UserSession:
    category: str
    shelter_type: Optional[str] = None
    zip_code: Optional[str] = None
    offset: int = 0
    timestamp: float = field(default_factory=time.time)

user_sessions: dict[str, UserSession] = {}


def _get_session(phone: str) -> Optional[UserSession]:
    session = user_sessions.get(phone)
    if session is None:
        return None
    if time.time() - session.timestamp > SESSION_TIMEOUT:
        del user_sessions[phone]
        return None
    return session


def _set_session(phone: str, category: str, shelter_type: Optional[str] = None, zip_code: Optional[str] = None) -> None:
    user_sessions[phone] = UserSession(
        category=category,
        shelter_type=shelter_type,
        zip_code=zip_code,
        offset=PAGE_SIZE,
    )


def _advance_session(session: UserSession) -> None:
    session.offset += PAGE_SIZE
    session.timestamp = time.time()


def _cleanup_sessions() -> None:
    now = time.time()
    expired = [p for p, s in user_sessions.items() if now - s.timestamp > SESSION_TIMEOUT]
    for phone in expired:
        del user_sessions[phone]


# ── Google Sheets loader ──────────────────────────────────────────────────────

def load_resources_from_sheet() -> list[Resource]:
    try:
        creds_json = os.getenv("GOOGLE_CREDS_JSON")
        log.info("ENV CHECK: GOOGLE_CREDS_JSON present=%s length=%s", creds_json is not None, len(creds_json) if creds_json else 0)
        log.info("ENV KEYS with GOOGLE: %s", [k for k in os.environ if "GOOGLE" in k])
        if creds_json:
            try:
                creds_json_fixed = creds_json.replace('\\\\n', '\\n').replace('\\r', '').replace('\\r\\n', '\\n')
                creds = Credentials.from_service_account_info(json.loads(creds_json_fixed, strict=False), scopes=SCOPES)
                log.info("Loaded Google credentials from GOOGLE_CREDS_JSON env var")
            except Exception as creds_err:
                log.error("Failed to load credentials from GOOGLE_CREDS_JSON: %s", creds_err, exc_info=True)
                raise
        else:
            creds = Credentials.from_service_account_file(CREDS_FILE, scopes=SCOPES)
            log.info("Loaded Google credentials from file: %s", CREDS_FILE)
        client = gspread.authorize(creds)
        sheet  = client.open(SHEET_NAME).sheet1

        all_values = sheet.get_all_values()
        if not all_values:
            return []
        # Row 0 is spreadsheet column labels (A, B, C…); row 2 has real headers
        headers = [h.strip() for h in all_values[2]]
        rows    = [dict(zip(headers, row)) for row in all_values[3:]]

        resources = []
        for r in rows:
            name = r.get("Name", "").strip()
            if not name:
                continue
            resources.append(Resource(
                name         = name,
                category     = r.get("Category", "").strip().upper(),
                address      = r.get("Address", "").strip(),
                zip_code     = r.get("ZIP", "").strip(),
                phone        = r.get("Phone", "").strip(),
                hours        = r.get("Hours", "").strip(),
                pets         = r.get("Pets", "").strip(),
                categories   = r.get("Categories", "").strip(),
                shelter_type = r.get("Shelter/Station Type", "").strip().upper(),
                notes        = r.get("Notes", "").strip(),
                eligibility  = r.get("Eligibility Requirements", "").strip(),
            ))

        cats = len({r.category for r in resources})
        log.info("Loaded %d resources across %d categories", len(resources), cats)
        log.info("Twilio webhook URL → http://<your-host>:5702/sms")
        return resources

    except Exception as e:
        log.error("Failed to load from Google Sheets: %s", e, exc_info=True)
        return []


RESOURCES: list[Resource] = []

# ── Category metadata ─────────────────────────────────────────────────────────

CATEGORY_META: dict[str, dict] = {
    "SHELTER": {
        "emoji": "🏠",
        "label": "Emergency Shelter",
    },
    "FOOD": {
        "emoji": "🍽️",
        "label": "Food & Water",
    },
    "COOL": {
        "emoji": "❄️",
        "label": "Cooling Stations",
    },
    "PET": {
        "emoji": "🐾",
        "label": "Pet-Friendly Resources",
    },
}

KEYWORD_MAP: dict[str, str] = {
    "SHELTER": "SHELTER",
    "FOOD":    "FOOD",
    "WATER":   "FOOD",
    "COOL":    "COOL",
    "COOLING": "COOL",
    "PET":     "PET",
    "PETS":    "PET",
}

SHELTER_TYPE_MAP: dict[str, str] = {
    "MEN":    "MEN",
    "WOMEN":  "WOMEN",
    "WOMAN":  "WOMEN",
    "DV":     "WOMEN",
    "FAMILY": "FAMILY",
    "FAM":    "FAMILY",
    "YOUTH":  "YOUTH",
    "KID":    "YOUTH",
    "VET":    "VET",
    "VETS":   "VET",
    "VETERAN":"VET",
    "ALL":    "ALL",
}

# ── Pool builder ──────────────────────────────────────────────────────────────

def _get_pool(category: str, shelter_type: Optional[str] = None, zip_code: Optional[str] = None) -> list[Resource]:
    pool = [r for r in RESOURCES if r.category == category]
    if category == "PET":
        # No-ZIP PET rows are mobile outreach orgs — appended separately to
        # every PET response by _mobile_pet_block(), so keep them out of the
        # paged pool to avoid duplicates.
        pool = [r for r in pool if r.zip_code]
    if category == "SHELTER" and shelter_type and shelter_type != "ALL":
        pool = [r for r in pool if shelter_type in r.shelter_type or r.shelter_type == ""]
    if zip_code:
        pool = [r for r in pool if not r.zip_code or r.zip_code == zip_code]
    return pool


def _mobile_pet_block() -> str:
    mobile = [r for r in RESOURCES if r.category == "PET" and not r.zip_code]
    if not mobile:
        return ""
    body = "\n\n".join(_format_resource(r) for r in mobile)
    return "\n\n" + "─" * 20 + "\n\n🚐 Mobile outreach, comes to you:\n\n" + body


# ── Formatters ────────────────────────────────────────────────────────────────

def _format_resource(r: Resource) -> str:
    parts = [r.name.upper() + "\n"]
    if r.address:
        parts.append(r.address)
    if r.phone:
        parts.append(r.phone)
    if r.hours:
        hours = r.hours.strip()
        parts.append(hours[:60] + "…" if len(hours) > 60 else hours)
    if r.eligibility:
        elig = r.eligibility.strip()
        elig_low = elig.lower()
        # Skip ZIP-based eligibility — the pool filter handles it
        has_many_zips = len(re.findall(r'\b\d{5}\b', elig)) >= 3
        if (
            elig_low not in ("n/a", "none", "")
            and "zip" not in elig_low
            and not has_many_zips
        ):
            parts.append(f"Need: {elig[:40]}{'…' if len(elig) > 40 else ''}")
    return "\n".join(parts)


def build_category_message(
    category: str,
    offset: int = 0,
    shelter_type: Optional[str] = None,
    zip_code: Optional[str] = None,
) -> str:
    meta  = CATEGORY_META.get(category, {"emoji": "", "label": category.title()})
    emoji = meta["emoji"]
    label = meta["label"]

    pool  = _get_pool(category, shelter_type, zip_code)
    total = len(pool)
    page  = pool[offset:offset + PAGE_SIZE]

    mobile_block = _mobile_pet_block() if category == "PET" else ""

    r_word = "resource" if total == 1 else "resources"
    # "Pet-Friendly Resources" already ends in the noun — avoid "Resources resources"
    label_noun = label if label.lower().endswith("resources") else f"{label} {r_word}"

    if not page:
        if offset == 0:
            if zip_code:
                return (
                    f"{emoji} No {label_noun} found near {zip_code}.\n\n"
                    f"Reply MORE for all results or try a different ZIP."
                ) + mobile_block
            return f"{emoji} No {label_noun} found right now." + mobile_block
        return f"That's all {total} {r_word}." + mobile_block

    if offset > 0:
        range_end = min(offset + PAGE_SIZE, total)
        zip_part = f" near {zip_code}" if zip_code else ""
        header = f"{emoji} {label.upper()}{zip_part} ({offset + 1}-{range_end} of {total})"
    else:
        zip_part = f" (near {zip_code})" if zip_code else ""
        header = f"{emoji} {label.upper()}{zip_part}"

    body = ("\n\n" + "─" * 20 + "\n\n").join(_format_resource(r) for r in page)

    remaining = total - (offset + PAGE_SIZE)
    if remaining > 0:
        next_count = min(remaining, PAGE_SIZE)
        zip_hint = "\nReply your ZIP for nearest results." if offset == 0 and not zip_code else ""
        footer = f"\n\nReply MORE for next {next_count}.{zip_hint}"
    else:
        footer = f"\n\nThat's all {total} {r_word}."

    return header + "\n\n" + body + footer + mobile_block


HOPE_MENU = (
    "HOPE 702 💛\n\n"
    "1 - Shelter\n"
    "2 - Cooling Center\n"
    "3 - Food & Water\n"
    "4 - Pet Help\n\n"
    "Reply a number to get started.\n"
    "hope702.org"
)

NUMBER_MAP: dict[str, str] = {
    "1": "SHELTER",
    "2": "COOL",
    "3": "FOOD",
    "4": "PET",
}


# ── Anonymous demand logging ──────────────────────────────────────────────────
#
# What is stored per text: ZIP, category, UTC timestamp. That is the whole row.
# The phone number is NEVER written to disk in readable form — it is used only
# to derive a keyed digest so repeat texters can be counted without knowing who
# they are.
#
# Why HMAC with a secret key rather than a plain hash: there are only ~10^10
# possible US phone numbers, so an unsalted SHA-256 of a number is reversible by
# brute force in seconds and would not be anonymous at all. The secret is what
# makes the digest non-invertible in practice, so it must stay secret and stay
# stable (a rotating secret would silently break repeat-texter counting).

DEMAND_SCHEMA = """
CREATE TABLE IF NOT EXISTS demand (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,                 -- UTC ISO8601
    day          TEXT NOT NULL,                 -- UTC date, for cheap grouping
    zip_code     TEXT NOT NULL DEFAULT '',
    category     TEXT NOT NULL DEFAULT '',
    contact_hash TEXT NOT NULL                  -- HMAC-SHA256(secret, phone)
);
CREATE INDEX IF NOT EXISTS idx_demand_day ON demand(day);
CREATE INDEX IF NOT EXISTS idx_demand_zip ON demand(zip_code);
CREATE INDEX IF NOT EXISTS idx_demand_cat ON demand(category);
"""


def _load_contact_secret() -> bytes:
    """Key for the contact digest. Prefers HOPE702_HASH_SALT; otherwise keeps a
    generated key beside the database so dedup survives restarts."""
    env = os.getenv("HOPE702_HASH_SALT", "").strip()
    if env:
        return env.encode("utf-8")
    key_file = DATA_DIR / ".contact_secret"
    try:
        if key_file.exists():
            return key_file.read_bytes()
        key = secrets.token_bytes(32)
        key_file.write_bytes(key)
        try:
            os.chmod(key_file, 0o600)
        except OSError:
            pass
        log.warning("HOPE702_HASH_SALT not set — generated a keyfile at %s", key_file)
        return key
    except OSError as exc:
        # Read-only disk: fall back to a per-process key. Privacy is preserved;
        # only cross-restart dedup is lost, so say so rather than failing quietly.
        log.warning("could not persist contact secret (%s) — using a "
                    "per-process key; repeat-texter counts reset on restart", exc)
        return secrets.token_bytes(32)


_CONTACT_SECRET: Optional[bytes] = None


def _contact_digest(from_number: str) -> str:
    global _CONTACT_SECRET
    if _CONTACT_SECRET is None:
        _CONTACT_SECRET = _load_contact_secret()
    return hmac.new(_CONTACT_SECRET, (from_number or "").encode("utf-8"),
                    hashlib.sha256).hexdigest()[:16]


def _demand_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DEMAND_DB_FILE, timeout=5)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(DEMAND_SCHEMA)
    return con


def log_demand(from_number: str, zip_code: str, category: str) -> None:
    """Record one anonymous demand row. No-op when nothing was resolved."""
    zip_code = (zip_code or "").strip()
    category = (category or "").strip().upper()
    if not zip_code and not category:
        return                       # nothing resolved — nothing worth storing
    now = datetime.now(timezone.utc)
    try:
        with _demand_db() as con:
            con.execute(
                "INSERT INTO demand(ts, day, zip_code, category, contact_hash)"
                " VALUES(?,?,?,?,?)",
                (now.isoformat(), now.strftime("%Y-%m-%d"), zip_code, category,
                 _contact_digest(from_number)))
    except sqlite3.Error as exc:
        # Logging demand must never cost a texter their reply.
        log.warning("demand log write failed: %s", exc)
    log.info("demand → zip=%s category=%s", zip_code or "-", category or "-")


# ── Flask app ─────────────────────────────────────────────────────────────────

app = Flask(__name__)


@app.before_request
def _ensure_resources():
    global RESOURCES
    if not RESOURCES:
        RESOURCES = load_resources_from_sheet()


@app.route("/sms", methods=["POST"])
def sms():
    from_number = request.form.get("From", "")
    raw_body    = request.form.get("Body", "")
    body        = raw_body.strip().upper()

    # The number is masked here too: this log file is durable storage, so
    # "never store the phone number in plain text" has to cover it as well.
    log.info("SMS from %s: %r", _contact_digest(from_number), raw_body)

    resp = MessagingResponse()

    # Extract ZIP if user sent one (5-digit number)
    zip_code = ""
    if body.isdigit() and len(body) == 5:
        zip_code = body

    # Demand attributes for this text, filled in as the branches below resolve
    # them. _reply() writes the row; log_demand skips it when neither resolved.
    demand_zip, demand_cat = zip_code, ""

    def _reply() -> Response:
        log_demand(from_number, demand_zip, demand_cat)
        return Response(str(resp), mimetype="text/xml")

    if zip_code:
        session = _get_session(from_number)
        if session:
            demand_cat = session.category
            log.info("→ ZIP %s refining session category=%s", zip_code, session.category)
            pool_check = _get_pool(session.category, session.shelter_type, zip_code)
            if pool_check:
                session.zip_code = zip_code
                session.offset = PAGE_SIZE
            else:
                session.zip_code = None
                session.offset = PAGE_SIZE
            session.timestamp = time.time()
            resp.message(build_category_message(session.category, offset=0, shelter_type=session.shelter_type, zip_code=zip_code))
            return _reply()

    if len(user_sessions) > 100:
        _cleanup_sessions()

    if body == "MORE":
        session = _get_session(from_number)
        if session:
            # A MORE is continued interest in the same category, so it counts as
            # demand for it. It inherits the session's ZIP when one was given.
            demand_cat = session.category
            demand_zip = session.zip_code or demand_zip
            log.info("→ MORE  category=%s offset=%d shelter_type=%s zip=%s", session.category, session.offset, session.shelter_type, session.zip_code)
            resp.message(build_category_message(session.category, session.offset, session.shelter_type, session.zip_code))
            _advance_session(session)
        else:
            resp.message(
                "No active search to continue.\n\n"
                "Text HOPE for Las Vegas resources.\n"
                "Keywords: COOL · SHELTER · FOOD · WATER · PET"
            )
        return _reply()

    if body in NUMBER_MAP:
        cat = NUMBER_MAP[body]
        demand_cat = cat
        log.info("→ number shortcut %s → %s", body, cat)
        if cat == "SHELTER":
            resp.message(
                "What type of shelter do you need?\n\n"
                "Reply:\n"
                "MEN - Adults without children\n"
                "FAMILY - Families with children\n"
                "WOMEN - Fleeing domestic violence\n"
                "YOUTH - Youth under 18\n"
                "VET - Veterans\n"
                "ALL - Show all shelters"
            )
        else:
            resp.message(build_category_message(cat))
            _set_session(from_number, cat)

    elif "HOPE" in body:
        log.info("→ HOPE menu")
        resp.message(HOPE_MENU)

    elif body in SHELTER_TYPE_MAP:
        shelter_type = SHELTER_TYPE_MAP[body]
        demand_cat = "SHELTER"
        log.info("→ shelter type %s", shelter_type)
        resp.message(build_category_message("SHELTER", shelter_type=shelter_type))
        _set_session(from_number, "SHELTER", shelter_type=shelter_type)

    elif body in KEYWORD_MAP:
        cat = KEYWORD_MAP[body]
        demand_cat = cat
        if cat == "SHELTER":
            resp.message(
                "What type of shelter do you need?\n\n"
                "Reply:\n"
                "MEN - Adults without children\n"
                "FAMILY - Families with children\n"
                "WOMEN - Fleeing domestic violence\n"
                "YOUTH - Youth under 18\n"
                "VET - Veterans\n"
                "ALL - Show all shelters"
            )
        else:
            resp.message(build_category_message(cat))
            _set_session(from_number, cat)

    else:
        log.info("→ unknown keyword, sent help prompt")
        resp.message(
            "Text HOPE for Las Vegas resources.\n\n"
            "Keywords: COOL · SHELTER · FOOD · WATER · PET\n\n"
            "In crisis? Call 211 (24/7)\n"
            "hope702.org"
        )

    return _reply()


@app.route("/api/activity", methods=["GET"])
def api_activity():
    """Anonymous aggregates only: counts by ZIP, by category, by day.

    Deliberately has no way to return a row. Every query below is a GROUP BY
    with a COUNT, and contact_hash is never selected or exposed — the endpoint
    cannot leak an individual text even if something upstream asked it to.
    """
    try:
        con = _demand_db()
    except sqlite3.Error as exc:
        log.warning("activity read failed: %s", exc)
        return {"error": "activity store unavailable"}, 503
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        week_start = (datetime.now(timezone.utc) - timedelta(days=6)).strftime("%Y-%m-%d")

        by_zip = [{"zip": z, "texts": n} for z, n in con.execute(
            "SELECT zip_code, COUNT(*) FROM demand WHERE zip_code <> ''"
            " GROUP BY zip_code ORDER BY 2 DESC")]
        by_category = [{"category": c, "texts": n} for c, n in con.execute(
            "SELECT category, COUNT(*) FROM demand WHERE category <> ''"
            " GROUP BY category ORDER BY 2 DESC")]
        by_day = [{"day": d, "texts": n} for d, n in con.execute(
            "SELECT day, COUNT(*) FROM demand GROUP BY day ORDER BY day DESC"
            " LIMIT 60")]
        # ZIP x category, so the dashboard's category filter still means
        # something in demand mode ("which ZIP needs what"). Still a pure
        # count — the cross-tab carries no more identity than its margins.
        by_zip_category = [{"zip": z, "category": c, "texts": n}
                           for z, c, n in con.execute(
            "SELECT zip_code, category, COUNT(*) FROM demand"
            " WHERE zip_code <> '' AND category <> ''"
            " GROUP BY zip_code, category ORDER BY 3 DESC")]
        total = con.execute("SELECT COUNT(*) FROM demand").fetchone()[0]
        this_week = con.execute(
            "SELECT COUNT(*) FROM demand WHERE day >= ?", (week_start,)).fetchone()[0]
        today_n = con.execute(
            "SELECT COUNT(*) FROM demand WHERE day = ?", (today,)).fetchone()[0]
        # Distinct texters, from the keyed digest — a count, never the digests.
        people_week = con.execute(
            "SELECT COUNT(DISTINCT contact_hash) FROM demand WHERE day >= ?",
            (week_start,)).fetchone()[0]
    finally:
        con.close()

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "week_start": week_start,
        "totals": {"all_time": total, "this_week": this_week,
                   "today": today_n, "people_this_week": people_week},
        "by_zip": by_zip,
        "by_category": by_category,
        "by_zip_category": by_zip_category,
        "by_day": by_day,
    }
    resp = app.response_class(json.dumps(payload), mimetype="application/json")
    resp.headers["Access-Control-Allow-Origin"] = ALLOWED_ORIGIN
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok", "resources": len(RESOURCES), "active_sessions": len(user_sessions)}, 200


@app.route("/reload", methods=["POST"])
def reload_resources():
    global RESOURCES
    RESOURCES = load_resources_from_sheet()
    return {"status": "ok", "resources": len(RESOURCES)}, 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5702))
    host = os.getenv("HOST", "0.0.0.0")
    log.info("HOPE 702 SMS webhook starting on %s:%d", host, port)
    log.info("Loaded %d resources from Google Sheets", len(RESOURCES))
    app.run(host=host, port=port, debug=False)
