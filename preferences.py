"""Per-run preferences: the settings an admin can change for one run, and where they are kept.

Each run keeps only the values that differ from the defaults, in its own preferences.json (next to its
other per-run files). A run with no file, or a setting not in the file, uses the default, so a run
registered before a setting existed behaves as it always did, and a new setting needs no migration.

To add a setting, add one Setting to SETTINGS: its key, kind, default and wording. Reading, saving,
validating and the summary the command shows all follow from that entry.
"""
import dataclasses
import json
import os
import re

import runs
from gomode_analyzer.requirements import DEFAULT_PALETTE, category_code, ANSI_RESET

FILE_NAME = "preferences.json"
_HEX = re.compile(r"#?([0-9a-fA-F]{6})")
_RESET_WORDS = {"default", "reset"}


@dataclasses.dataclass(frozen=True)
class Setting:
    key: str          # name in the file and in the command
    kind: str         # "colour" (six-digit hex) or "bool"
    default: object
    label: str        # how the summary names it
    help: str = ""


# Item colours, in the order the summary lists them.
SETTINGS = (
    Setting("color_progression", "colour", DEFAULT_PALETTE["progression"], "Progression item colour",
            "Items needed to progress."),
    Setting("color_useful", "colour", DEFAULT_PALETTE["useful"], "Useful item colour",
            "Helpful but not required items."),
    Setting("color_trap", "colour", DEFAULT_PALETTE["trap"], "Trap item colour", "Trap items."),
    Setting("color_filler", "colour", DEFAULT_PALETTE["filler"], "Filler item colour", "Filler items."),
)
BY_KEY = {s.key: s for s in SETTINGS}


def parse_value(setting: Setting, text):
    """The stored form of what the admin typed: hex colours as six upper-case digits without the #,
    booleans as True/False. Raises ValueError with a message for the admin when it isn't valid."""
    if setting.kind == "colour":
        m = _HEX.fullmatch(str(text).strip())
        if not m:
            raise ValueError(f"{setting.label}: '{text}' isn't a colour. Use six hex digits such as "
                             f"{setting.default}, with or without the #.")
        return m.group(1).upper()
    if setting.kind == "bool":
        if isinstance(text, bool):
            return text
        word = str(text).strip().lower()
        if word in ("on", "true", "yes", "1"):
            return True
        if word in ("off", "false", "no", "0"):
            return False
        raise ValueError(f"{setting.label}: use on or off.")
    raise ValueError(f"unknown setting kind {setting.kind!r}")


def is_reset(text) -> bool:
    return isinstance(text, str) and text.strip().lower() in _RESET_WORDS


def _path(run=None) -> str:
    return runs.run_path(FILE_NAME, run)


def _overrides(run=None) -> dict:
    """The saved values that are still valid settings; anything else in the file is ignored."""
    try:
        with open(_path(run), encoding="utf-8") as fh:
            saved = (json.load(fh) or {}).get("values") or {}
    except (OSError, ValueError, AttributeError):
        return {}
    out = {}
    for key, value in saved.items() if isinstance(saved, dict) else ():
        if key in BY_KEY:
            try:
                out[key] = parse_value(BY_KEY[key], value)
            except ValueError:
                pass
    return out


def values(run=None) -> dict:
    """Every setting's current value, defaults filled in."""
    return {**{s.key: s.default for s in SETTINGS}, **_overrides(run)}


def get(key: str, run=None):
    return values(run)[key]


def save(changes: dict, run=None) -> dict:
    """Apply {key: value-or-None} (None puts the setting back to its default) and write the file
    atomically. Returns the resulting values. Values must already be parsed."""
    current = _overrides(run)
    for key, value in changes.items():
        if key not in BY_KEY:
            raise KeyError(key)
        if value is None or value == BY_KEY[key].default:
            current.pop(key, None)
        else:
            current[key] = value
    path = _path(run)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"values": current}, fh, indent=2)
    os.replace(tmp, path)
    return values(run)


def palette(run=None) -> dict:
    """The run's item colours as {progression, useful, trap, filler: hex}, for the colouring code."""
    v = values(run)
    return {name: v[f"color_{name}"] for name in DEFAULT_PALETTE}


def summary_lines(run=None) -> list:
    """One line per setting for the command's reply, colour settings drawn in their own colour (the
    reply goes in an ansi block)."""
    v, saved = values(run), _overrides(run)
    lines = []
    for s in SETTINGS:
        value = v[s.key]
        if s.kind == "colour":
            shown = f"{category_code(s.key[len('color_'):], value)}#{value}{ANSI_RESET}"
        else:
            shown = "on" if value else "off"
        lines.append(f"{s.label}: {shown}" + ("" if s.key in saved else " (default)"))
    return lines
