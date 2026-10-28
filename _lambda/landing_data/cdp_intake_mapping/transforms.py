"""Value transforms shared by upload landing and the connector worker (stdlib only)."""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

# Python 3.10 (landing Lambda) has no datetime.UTC alias, so timezone.utc stays.
UTC = timezone.utc  # noqa: UP017
NULL_TOKENS = frozenset({"", "-", "--", "n/a", "na", "null", "none", "#n/a", "#value!", "#ref!",
                         "#div/0!"})


class TransformError(ValueError):
    pass


def clean(value: Any) -> Any:
    """Source cell -> python value; spreadsheet placeholders become None."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return None if text.lower() in NULL_TOKENS else text
    return value


def _text(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def digits(value: Any) -> str | None:
    out = re.sub(r"[^0-9]", "", _text(value))
    return out or None


def th_mobile(value: Any) -> str | None:
    d = re.sub(r"[^0-9]", "", _text(value))
    if re.fullmatch(r"0[689][0-9]{8}", d):
        return d
    if re.fullmatch(r"66[689][0-9]{8}", d):
        return "0" + d[2:]
    if re.fullmatch(r"[689][0-9]{8}", d):
        return "0" + d
    return None


def _phone_parts(value: Any) -> tuple[str, str] | None:
    """-> (kind, number). kind: th_mobile | th_fixed | intl ('+' E.164) | other (country unknown).

    Thai numbers stay in national form (0XXXXXXXXX), so keys match `th_mobile` output exactly.
    """
    text = re.sub(r"(?i)\s*(?:ext\.?|x|ต่อ|#)\s*\d{1,6}$", "", _text(value).strip())
    d = re.sub(r"[^0-9]", "", text)
    intl = text.startswith("+") or (d.startswith("00") and not d.startswith("000"))
    if intl and d.startswith("00"):
        d = d[2:]
    if not d or len(set(d.lstrip("0"))) <= 1:
        return None
    if intl and not d.startswith("66"):
        return ("intl", "+" + d) if 8 <= len(d) <= 15 else None
    if d.startswith("66") and (intl or len(d) in (10, 11)):
        d = "0" + d[2:]
    if re.fullmatch(r"0[689][0-9]{8}", d):
        return "th_mobile", d
    if re.fullmatch(r"[689][0-9]{8}", d):
        return "th_mobile", "0" + d
    if re.fullmatch(r"0[2-7][0-9]{7}", d):
        return "th_fixed", d
    return ("other", d) if 7 <= len(d) <= 15 else None


def phone(value: Any) -> str | None:
    """Any phone for the profile: Thai national form, '+' E.164 with a country code, else digits."""
    parts = _phone_parts(value)
    return parts[1] if parts else None


def phone_key(value: Any) -> str | None:
    """Phone usable as a person key: like `phone` but Thai landlines (shared lines) are not keys."""
    parts = _phone_parts(value)
    return parts[1] if parts and parts[0] != "th_fixed" else None


def to_number(value: Any) -> str:
    if isinstance(value, (int, Decimal)) or (isinstance(value, float) and value == value):
        return format(Decimal(repr(value)) if isinstance(value, float) else Decimal(value), "f")
    text = re.sub(r"[,\s฿]", "", _text(value))
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise TransformError("not a number") from None
    if not number.is_finite():
        raise TransformError("not a number")
    return format(number, "f")


_TH_MONTHS = (("มกราคม", "ม.ค."), ("กุมภาพันธ์", "ก.พ."), ("มีนาคม", "มี.ค."), ("เมษายน", "เม.ย."),
              ("พฤษภาคม", "พ.ค."), ("มิถุนายน", "มิ.ย."), ("กรกฎาคม", "ก.ค."), ("สิงหาคม", "ส.ค."),
              ("กันยายน", "ก.ย."), ("ตุลาคม", "ต.ค."), ("พฤศจิกายน", "พ.ย."), ("ธันวาคม", "ธ.ค."))
_EN_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


def _th_month_pattern() -> re.Pattern[str]:
    words = []
    for n, (full, abbr) in enumerate(_TH_MONTHS, 1):
        words.append((full, n))
        words.append((r"\.?".join(re.escape(c) for c in abbr.replace(".", "")) + r"\.?", n))
    words.sort(key=lambda w: -len(w[0]))
    return re.compile("|".join(f"(?P<m{i}_{n}>{w})" for i, (w, n) in enumerate(words)))


_TH_MONTH_RE = _th_month_pattern()
_EN_MONTH_RE = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?(?![a-z])",
                          re.I)
_WEEKDAY_RE = re.compile(r"\b(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?,?"
                         r"|วัน(?:จันทร์|อังคาร|พุธ|พฤหัสบดี|พฤหัส|ศุกร์|เสาร์|อาทิตย์)"
                         r"|วันที่|ที่|เวลา|(?:พ\.?ศ|ค\.?ศ)\.?", re.I)
_TZ = r"(?P<tz>z|(?:gmt|utc)?\s*[+-]\d{1,2}(?::?\d{2})?|gmt|utc)?"
_TIME_RE = re.compile(r"(?<!\d)(?P<h>\d{1,2}):(?P<mi>\d{2})"
                      r"(?::(?P<s>\d{2})(?:[.,](?P<f>\d{1,9}))?)?\s*"
                      r"(?P<ap>[ap]\.?m\.?|น\.)?\s*" + _TZ + r"(?![\d:])", re.I)
_THAI_DOT_TIME_RE = re.compile(r"(?<!\d)(?P<h>\d{1,2})\.(?P<mi>\d{2})\s*(?P<ap>น\.)")
_EXCEL_EPOCH = datetime(1899, 12, 30)


def _year(y: int, raw: str, buddhist: bool = False) -> int:
    if len(raw) <= 2:
        # Thai text "5 พ.ย. 67" means BE 2567; otherwise the strptime %y pivot
        y += 2500 if buddhist else 2000 if y < 69 else 1900
    return y - 543 if y > 2400 else y  # Buddhist Era


def _from_number(n: float, tz_offset_hours: float) -> datetime:
    """Spreadsheet serial day or Unix epoch (s/ms); epoch becomes local wall time."""
    if 20000 <= n < 80000:
        return _EXCEL_EPOCH + timedelta(days=n)
    if 1e9 <= n < 1e11 or 1e12 <= n < 1e14:
        seconds = n / 1000 if n >= 1e12 else n
        utc = datetime.fromtimestamp(seconds, UTC).replace(tzinfo=None)
        return utc + timedelta(hours=tz_offset_hours)
    raise TransformError("unrecognised date")


def _tz(raw: str | None) -> timezone | None:
    if not raw:
        return None
    raw = re.sub(r"(?i)gmt|utc|\s", "", raw)
    if raw in ("", "z", "Z"):
        return UTC
    m = re.fullmatch(r"([+-])(\d{1,2}):?(\d{2})?", raw)
    if not m:
        raise TransformError("unrecognised time zone")
    minutes = int(m.group(2)) * 60 + int(m.group(3) or 0)
    if minutes > 14 * 60:
        raise TransformError("unrecognised time zone")
    return timezone(timedelta(minutes=minutes if m.group(1) == "+" else -minutes))


def parse_datetime(value: Any, dayfirst: bool = True, tz_offset_hours: float = 7.0) -> datetime:
    """Best-effort parse of any common date/time text -> datetime (naive = local wall time).

    Handles ISO 8601 (T, Z, offsets, fractions), JS Date.toString(), RFC 2822, English and Thai
    month names, weekday words, Buddhist Era years, 2-digit years, AM/PM and 'น.', compact
    YYYYMMDD[HHMM[SS]], spreadsheet serial days and Unix epoch (s/ms). Numeric d/m vs m/d is
    decided by a part > 12; when both parts are <= 12 `dayfirst` decides.
    """
    if isinstance(value, datetime):
        return value.replace(year=value.year - 543) if value.year > 2400 else value
    if isinstance(value, date):
        return datetime.combine(_fix_buddhist(value), datetime.min.time())
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        if 19000101 <= value <= 22001231 and float(value).is_integer():
            return parse_datetime(str(int(value)), dayfirst, tz_offset_hours)
        return _from_number(float(value), tz_offset_hours)
    text = _text(value).strip()
    if re.fullmatch(r"\d{8}|\d{12}|\d{14}", text):
        hms = (int(text[8:10] or 0), int(text[10:12] or 0), int(text[12:14] or 0))
        for y_raw, mo, d in ((text[0:4], text[4:6], text[6:8]), (text[4:8], text[2:4], text[0:2])):
            yr = _year(int(y_raw), y_raw)
            if 1800 <= yr <= 2200:
                try:
                    return datetime(yr, int(mo), int(d), *hms)
                except ValueError:
                    pass
            if len(text) > 8:
                break  # YYYYMMDDHHMM[SS] only
        if len(text) == 8:
            raise TransformError("unrecognised date")
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return _from_number(float(text), tz_offset_hours)

    s = re.sub(r"\([^)]*\)", " ", text)  # "(Eastern Standard Time)"
    s = re.sub(r"(?<=\d)T(?=\d)", " ", s)
    s = _WEEKDAY_RE.sub(" ", s)
    s = re.sub(r"(?<=\d)(?:st|nd|rd|th)\b|\b(?:at|of|on)\b", " ", s, flags=re.I)
    month, thai = None, False

    def take_month(m: re.Match[str]) -> str:
        nonlocal month, thai
        if month is not None:
            raise TransformError("unrecognised date")
        if m.re is _EN_MONTH_RE:
            month = _EN_MONTHS.index(m.group(1).lower()) + 1
        else:
            group = next(k for k, v in m.groupdict().items() if v)
            month, thai = int(group.rsplit("_", 1)[1]), True
        return " M "

    s = _TH_MONTH_RE.sub(take_month, s)
    s = _EN_MONTH_RE.sub(take_month, s)

    h = mi = sec = micro = 0
    tz, us_clock = None, False
    tm = _TIME_RE.search(s) or _THAI_DOT_TIME_RE.search(s)
    if tm:
        g = tm.groupdict()
        h, mi, sec = int(g["h"]), int(g["mi"]), int(g.get("s") or 0)
        micro = int((g.get("f") or "0")[:6].ljust(6, "0"))
        ap = (g.get("ap") or "").lower().replace(".", "")
        if ap in ("am", "pm"):
            if not 1 <= h <= 12:
                raise TransformError("unrecognised time")
            us_clock = True  # 12-hour clock: an ambiguous 11/5/2024 reads month first, as before
            h = h % 12 + (12 if ap == "pm" else 0)
        tz = _tz(g.get("tz"))
        s = s[:tm.start()] + " " + s[tm.end():]

    tokens = re.findall(r"M|\d+", re.sub(r"[,/.\-\s]+", " ", s))
    if re.sub(r"[\sM\d,/.\-]", "", s):
        raise TransformError("unrecognised date")
    nums = [t for t in tokens if t != "M"]
    try:
        if month is not None and len(nums) == 2:
            # "5 Nov 2024", "Nov 5, 2024", "2024 Nov 5"; two short numbers read day, then year
            a, b = nums
            y_raw, d_raw = (a, b) if len(a) >= 3 or (len(b) < 3 and int(a) > 31) else (b, a)
            day, mo, yr = int(d_raw), month, _year(int(y_raw), y_raw, thai)
        elif month is None and len(nums) == 3:
            a, b, c = nums
            if len(a) >= 3:
                yr, mo, day = _year(int(a), a), int(b), int(c)
            else:
                x, y = int(a), int(b)
                if x > 12 >= y:
                    day, mo = x, y
                elif y > 12 >= x:
                    mo, day = x, y
                else:
                    day, mo = (x, y) if dayfirst and not us_clock else (y, x)
                yr = _year(int(c), c)
        else:
            raise TransformError("unrecognised date")
        if not 1800 <= yr <= 2200:
            raise TransformError("unrecognised date")
        return datetime(yr, mo, day, h, mi, sec, micro, tzinfo=tz)
    except ValueError:
        raise TransformError("unrecognised date") from None


def _fix_buddhist(d: date) -> date:
    return d.replace(year=d.year - 543) if d.year > 2400 else d


def parse_date(value: Any, dayfirst: bool = True, tz_offset_hours: float = 7.0) -> str:
    """Calendar date as written in the source (a time zone in the text does not move the date)."""
    if isinstance(value, datetime):
        return _fix_buddhist(value.date()).isoformat()
    if isinstance(value, date):
        return _fix_buddhist(value).isoformat()
    return parse_datetime(value, dayfirst, tz_offset_hours).date().isoformat()


def parse_timestamp(value: Any, tz_offset_hours: float = 7.0, dayfirst: bool = True) -> str:
    """Naive source timestamps are local time (default Asia/Bangkok, UTC+7); output is UTC ISO."""
    dt = parse_datetime(value, dayfirst, tz_offset_hours)
    if dt.tzinfo is None:
        dt = (dt - timedelta(hours=tz_offset_hours)).replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Transforms:
    """Named transforms; `hmac` needs a per-tenant secret so hashed IDs cannot be brute-forced."""

    def __init__(self, hmac_key: bytes | None = None, tz_offset_hours: float = 7.0):
        self.hmac_key = hmac_key
        self.tz_offset_hours = tz_offset_hours
        self._table: dict[str, Callable[[Any], Any]] = {
            "trim": lambda v: _text(v).strip(),
            "lowercase": lambda v: _text(v).lower(),
            "uppercase": lambda v: _text(v).upper(),
            "stringify": _text,
            "digits": digits,
            "th_mobile": th_mobile,
            "phone": phone,
            "phone_key": phone_key,
            "to_number": to_number,
            "parse_date": lambda v: parse_date(v, True, self.tz_offset_hours),
            "parse_timestamp": lambda v: parse_timestamp(v, self.tz_offset_hours),
            "parse_date_mdy": lambda v: parse_date(v, False, self.tz_offset_hours),
            "parse_timestamp_mdy": lambda v: parse_timestamp(v, self.tz_offset_hours, False),
            "nospace": lambda v: re.sub(r"\s+", "", _text(v)),
            "alnum": lambda v: re.sub(r"[^0-9A-Za-z]", "", _text(v)).upper() or None,
            "hmac": self._hmac,
        }

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self._table)

    def _hmac(self, value: Any) -> str:
        if not self.hmac_key:
            raise TransformError("hmac transform needs a tenant key")
        return hmac.new(self.hmac_key, _text(value).encode(), hashlib.sha256).hexdigest()

    def apply(self, value: Any, names: list[str]) -> Any:
        for name in names:
            if value is None:
                return None
            if name not in self._table:
                raise TransformError(f"unknown transform {name}")
            value = self._table[name](value)
        return value
