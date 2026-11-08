"""Time zone helpers that also work on minimal systems without IANA tzdata."""
from __future__ import annotations

import datetime as _dt
import re

_FIXED = re.compile(r"^(?:UTC|GMT)?\s*([+-])(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)


def resolve_timezone(name: str) -> _dt.tzinfo:
    """Return a tzinfo for an IANA name (``Asia/Shanghai``) or a fixed offset (``UTC+08:00``).

    Minimal distributions (OpenWrt, some containers) ship Python without tzdata.
    In that case an IANA name cannot be loaded and the caller gets a clear error
    suggesting a fixed offset instead of silently using UTC.
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("timezone must be a non-empty string")
    text = name.strip()
    if text.upper() in {"UTC", "GMT", "Z"}:
        return _dt.timezone.utc
    match = _FIXED.match(text)
    if match:
        sign, hours, minutes = match.group(1), int(match.group(2)), int(match.group(3) or 0)
        if hours > 23 or minutes > 59:
            raise ValueError(f"invalid fixed UTC offset: {name!r}")
        delta = _dt.timedelta(hours=hours, minutes=minutes)
        label = f"UTC{sign}{hours:02d}:{minutes:02d}"
        return _dt.timezone(-delta if sign == "-" else delta, label)
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    except ImportError as exc:  # pragma: no cover - Python < 3.9
        raise ValueError("zoneinfo is unavailable; use a fixed offset such as UTC+08:00") from exc
    try:
        return ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ValueError(
            f"time zone {name!r} is not available on this system (no tzdata?); "
            "install tzdata or use a fixed offset such as UTC+08:00") from exc


def zone_label(tz: _dt.tzinfo) -> str:
    key = getattr(tz, "key", None)
    if isinstance(key, str):
        return key
    return str(tz) if str(tz) else "UTC"
