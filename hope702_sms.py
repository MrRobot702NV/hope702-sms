#!/usr/bin/env python3
"""
HOPE 702 SMS Webhook
Twilio-powered SMS resource bot for Las Vegas homeless services.
Runs on port 5702.
"""

import csv
import json
import re
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
ZIP_LOG_FILE     = Path(__file__).parent / "hope702_zip_log.csv"
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
                creds = Credentials.from_service_account_info(json.loads(creds_json), scopes=SCOPES)
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


RESOURCES: list[Resource] = load_resources_from_sheet()

# ── Category metadata ─────────────────────────────────────────────────────────

CATEGORY_META: dict[str, dict] = {
    "SHELTER": {
        "emoji": "🏠",
        "label": "Emergency Shelter",
    },
    "FOOD": {
        "emoji": "🍽️",
        "label": "Food & Meals",
    },
    "WATER": {
        "emoji": "💧",
        "label": "Water & Hydration",
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
    "WATER":   "WATER",
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
    if category == "SHELTER" and shelter_type and shelter_type != "ALL":
        pool = [r for r in pool if shelter_type in r.shelter_type or r.shelter_type == ""]
    if zip_code:
        pool = [r for r in pool if not r.zip_code or r.zip_code == zip_code]
    return pool


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

    r_word = "resource" if total == 1 else "resources"

    if not page:
        if offset == 0:
            if zip_code:
                return (
                    f"{emoji} No {label} {r_word} found near {zip_code}.\n\n"
                    f"Reply MORE for all results or try a different ZIP."
                )
            return f"{emoji} No {label} {r_word} found right now."
        return f"That's all {total} {r_word}."

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

    return header + "\n\n" + body + footer


HOPE_MENU = (
    "HOPE 702 💛\n\n"
    "1 - Shelter\n"
    "2 - Cooling Center\n"
    "3 - Food\n"
    "4 - Water\n"
    "5 - Pet Help\n\n"
    "Reply a number to get started.\n"
    "hope702.org"
)

NUMBER_MAP: dict[str, str] = {
    "1": "SHELTER",
    "2": "COOL",
    "3": "FOOD",
    "4": "WATER",
    "5": "PET",
}


# ── ZIP logging ───────────────────────────────────────────────────────────────

def _log_zip(from_number: str, zip_code: str) -> None:
    area_code = from_number.lstrip("+")[:1 + (1 if from_number.startswith("+1") else 0)]
    area_code = from_number.lstrip("+")[1:4] if from_number.startswith("+1") else from_number[:3]
    log.info("ZIP log → area_code=%r zip=%r", area_code, zip_code)
    write_header = not ZIP_LOG_FILE.exists() or ZIP_LOG_FILE.stat().st_size == 0
    with ZIP_LOG_FILE.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(["timestamp", "from_number", "area_code", "zip_code"])
        timestamp = datetime.now(timezone.utc).isoformat()
        writer.writerow([timestamp, from_number, area_code, zip_code])


# ── Flask app ─────────────────────────────────────────────────────────────────

app = Flask(__name__)


@app.route("/sms", methods=["POST"])
def sms():
    from_number = request.form.get("From", "")
    raw_body    = request.form.get("Body", "")
    body        = raw_body.strip().upper()

    log.info("SMS from %s: %r", from_number, raw_body)

    resp = MessagingResponse()

    # Extract ZIP if user sent one (5-digit number)
    zip_code = ""
    if body.isdigit() and len(body) == 5:
        zip_code = body

    _log_zip(from_number, zip_code)

    if zip_code:
        session = _get_session(from_number)
        if session:
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
            return Response(str(resp), mimetype="text/xml")

    if len(user_sessions) > 100:
        _cleanup_sessions()

    if body == "MORE":
        session = _get_session(from_number)
        if session:
            log.info("→ MORE  category=%s offset=%d shelter_type=%s zip=%s", session.category, session.offset, session.shelter_type, session.zip_code)
            resp.message(build_category_message(session.category, session.offset, session.shelter_type, session.zip_code))
            _advance_session(session)
        else:
            resp.message(
                "No active search to continue.\n\n"
                "Text HOPE for Las Vegas resources.\n"
                "Keywords: COOL · SHELTER · FOOD · WATER · PET"
            )
        return Response(str(resp), mimetype="text/xml")

    if body in NUMBER_MAP:
        cat = NUMBER_MAP[body]
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
        log.info("→ shelter type %s", shelter_type)
        resp.message(build_category_message("SHELTER", shelter_type=shelter_type))
        _set_session(from_number, "SHELTER", shelter_type=shelter_type)

    elif body in KEYWORD_MAP:
        cat = KEYWORD_MAP[body]
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

    return Response(str(resp), mimetype="text/xml")


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
