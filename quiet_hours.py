"""Quiet hours: a daily window in which the bot holds its automatic DMs and sends them when it ends.

The window is a start and an end time (24-hour HH:MM) in an IANA time zone, and it may cross midnight
(22:00 to 07:00). A DM that arrives inside the window is saved in the run's own held_dms.json, so it
is still there after a restart, and it is sent by the first check after the window ends.
"""
import datetime
import json
import os
import re
import uuid
from zoneinfo import ZoneInfo, available_timezones

import runs

FILE_NAME = "held_dms.json"
DEFAULT_ZONE = "UTC"
_TIME = re.compile(r"(\d{1,2}):(\d{2})")


def parse_time(text) -> str:
    """'7:30' or '07:30' as '07:30'. Raises ValueError for anything that isn't a 24-hour time."""
    m = _TIME.fullmatch(str(text).strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError(f"'{text}' isn't a time. Use 24-hour HH:MM, such as 22:00.")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def parse_zone(text) -> str:
    """The IANA name for what was typed (any letter case). Raises ValueError when it isn't one, or
    when this host has no time zone data."""
    name = str(text).strip()
    try:
        ZoneInfo(name)
        return name
    except Exception:  # noqa: BLE001 - not found, malformed, or no tz database at all
        pass
    try:
        for known in available_timezones():
            if known.lower() == name.lower():
                return known
    except Exception:  # noqa: BLE001
        pass
    raise ValueError(f"'{text}' isn't a time zone this bot knows. Use an IANA name such as America/New_York "
                     f"or Europe/London.")


def _minutes(hhmm: str) -> int:
    return int(hhmm[:2]) * 60 + int(hhmm[3:])


def window(start, end, zone=None):
    """(start minute, end minute, zone name) for a complete setting, else None (quiet hours off)."""
    if not start or not end:
        return None
    return _minutes(start), _minutes(end), zone or DEFAULT_ZONE


def utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def in_window(win, now=None) -> bool:
    """Whether now (a datetime; the current time when omitted) is inside the window."""
    if win is None:
        return False
    start, end, zone = win
    try:
        local = (now or utcnow()).astimezone(ZoneInfo(zone))
    except Exception:  # noqa: BLE001 - a zone that can't be read means no quiet hours, never a lost DM
        return False
    t = local.hour * 60 + local.minute
    if start == end:
        return False
    return start <= t < end if start < end else (t >= start or t < end)


# --- held DMs ---------------------------------------------------------------------------------

def _path(run=None) -> str:
    return runs.run_path(FILE_NAME, run)


def load(run=None) -> list:
    """The held DMs, oldest first: [{"id", "user_id", "content", "held"}]."""
    try:
        with open(_path(run), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    return [d for d in data if isinstance(d, dict) and "user_id" in d and "content" in d] \
        if isinstance(data, list) else []


def _save(entries: list, run=None) -> None:
    path = _path(run)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(entries, fh, indent=2)
    os.replace(tmp, path)


def hold(user_id, content: str, run=None) -> None:
    entries = load(run)
    entries.append({"id": uuid.uuid4().hex, "user_id": str(user_id), "content": content,
                    "held": utcnow().strftime("%Y-%m-%d %H:%M:%S")})
    _save(entries, run)


def remove(ids, run=None) -> None:
    """Forget the held DMs with these ids (the rest are kept, including any held meanwhile)."""
    ids = set(ids)
    if ids:
        _save([d for d in load(run) if d.get("id") not in ids], run)
