from __future__ import annotations

import hashlib
import hmac
import json
import re
import unicodedata
from datetime import date, datetime
from typing import Any, Iterable

import phonenumbers
import pycountry
from dateutil import parser as date_parser
from email_validator import EmailNotValidError, validate_email


EMPTY_MARKERS = {"", "null", "none", "n/a", "na", "-"}
NATIONAL_ID_KEYS = ("national_id", "citizen_id", "citizenid", "tax_id", "passport_number")
COUNTRY_ALIASES = {
    "usa": "US",
    "u.s.a.": "US",
    "united states of america": "US",
    "uk": "GB",
    "south korea": "KR",
    "north korea": "KP",
    "laos": "LA",
    "russia": "RU",
    "vietnam": "VN",
}


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = unicodedata.normalize("NFKC", str(value)).strip()
    if text.casefold() in EMPTY_MARKERS:
        return None
    return text


def normalize_text(value: Any) -> str | None:
    text = clean_text(value)
    if text is None:
        return None
    text = text.casefold()
    text = "".join(" " if unicodedata.category(char)[0] in {"P", "S"} else char for char in text)
    return re.sub(r"\s+", " ", text).strip() or None


def normalize_email(value: Any) -> tuple[str | None, str | None, bool]:
    text = clean_text(value)
    if text is None:
        return None, None, False
    text = re.sub(r"\s+", "", text).casefold()
    try:
        result = validate_email(text, check_deliverability=False)
        normalized = result.normalized.casefold()
        return normalized, normalized.rsplit("@", 1)[1], True
    except EmailNotValidError:
        domain = text.rsplit("@", 1)[1] if text.count("@") == 1 else None
        return text, domain, False


def normalize_phone(value: Any, default_region: str) -> tuple[str | None, str | None, bool]:
    text = clean_text(value)
    if text is None:
        return None, None, False
    try:
        parsed = phonenumbers.parse(text, default_region)
        country_code = f"+{parsed.country_code}" if parsed.country_code else None
        if phonenumbers.is_possible_number(parsed):
            return (
                phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164),
                country_code,
                phonenumbers.is_valid_number(parsed),
            )
    except phonenumbers.NumberParseException:
        pass
    digits = re.sub(r"\D", "", text)
    return (digits or None), None, False


def normalize_date(value: Any) -> date | None:
    text = clean_text(value)
    if text is None:
        return None
    try:
        parsed = date_parser.parse(text, yearfirst=True, dayfirst=False).date()
    except (ValueError, OverflowError):
        return None
    today = date.today()
    if parsed > today or parsed.year < today.year - 120:
        return None
    return parsed


def normalize_gender(value: Any) -> str | None:
    text = normalize_text(value)
    if text is None:
        return None
    aliases = {
        "m": "male",
        "ชาย": "male",
        "f": "female",
        "หญิง": "female",
        "o": "other",
        "non binary": "other",
        "nonbinary": "other",
    }
    return aliases.get(text, text)


def normalize_country(value: Any) -> str | None:
    text = clean_text(value)
    if text is None:
        return None
    alias = COUNTRY_ALIASES.get(text.casefold())
    if alias:
        return alias
    if len(text) == 2:
        country = pycountry.countries.get(alpha_2=text.upper())
        return country.alpha_2 if country else None
    try:
        return pycountry.countries.lookup(text).alpha_2
    except LookupError:
        return None


def normalized_devices(external_ids: Any) -> list[str]:
    if not isinstance(external_ids, dict):
        return []
    raw = external_ids.get("device_ids", [])
    if isinstance(raw, str):
        raw = re.split(r"[;,|]", raw)
    if not isinstance(raw, list):
        return []
    return sorted({item for value in raw if (item := normalize_text(value))})


def national_id_hash(external_ids: Any, key: str | None) -> str | None:
    if not key or not isinstance(external_ids, dict):
        return None
    lowered = {str(k).casefold(): v for k, v in external_ids.items()}
    for candidate in NATIONAL_ID_KEYS:
        value = clean_text(lowered.get(candidate))
        if value:
            canonical = re.sub(r"[^0-9a-zA-Z]", "", value).casefold()
            return hmac.new(key.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    return None


def stable_fingerprint(values: Iterable[Any]) -> str:
    encoded = json.dumps(list(values), ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def newest_timestamp(*values: Any) -> datetime | None:
    timestamps = [value for value in values if isinstance(value, datetime)]
    return max(timestamps) if timestamps else None

