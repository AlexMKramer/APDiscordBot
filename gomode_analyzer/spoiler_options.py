"""Recover a slot's *resolved* options from the spoiler.

Many worlds put nothing (or only a subset) in slot_data, so for those the player's
actual settings only survive in the spoiler's per-player block. Archipelago writes each
option as `"{display_name}:" {current_option_name}` (BaseClasses.Spoiler.write_option),
so we can invert it: build the same `display_name -> attribute` map from the world's
options dataclass and convert each value string back with the option's own
`from_any`/`from_text`.

Scalar options (Toggle / Choice / Range / FreeText and subclasses) convert back with the
option's own converters. Dict, list and set options are parsed from their joined text and
kept only if they render back to exactly what the spoiler wrote, so an ambiguous parse is
dropped rather than guessed. They matter even when they don't gate logic directly: a world
that draws randomly from one (e.g. weighted minigames) has to see the real value for the
replayed RNG to make the same picks. Plando options are left at their defaults.

`parse_player_blocks` is pure text (no AP). `resolve_options` needs the AP environment.
"""
from __future__ import annotations

import re
from typing import Any

_PLAYER_RE = re.compile(r"^Player\s+(\d+):\s+(.+)$")
# Section headers that mark the end of the per-player settings region.
_SECTION_HEADERS = {
    "Entrances", "Starting Items", "Locations", "Playthrough", "Paths",
    "Unreachable Progression Items", "Shops",
}


def parse_player_blocks(spoiler_text: str | None) -> dict:
    """Parse `Player N: name` blocks into {slot:int -> {"name": str, "settings": {k: v}}}.
    `settings` keys are the display names exactly as the spoiler wrote them."""
    blocks: dict = {}
    if not spoiler_text:
        return blocks
    current = None
    for line in spoiler_text.splitlines():
        stripped = line.strip()
        m = _PLAYER_RE.match(stripped)
        if m:
            current = {"name": m.group(2).strip(), "settings": {}}
            blocks[int(m.group(1))] = current
            continue
        if current is None:
            continue
        if not stripped:
            continue
        # A section header (e.g. "Entrances:") ends the settings region.
        if stripped.endswith(":") and stripped[:-1] in _SECTION_HEADERS:
            current = None
            continue
        if ":" in line:
            key, value = line.split(":", 1)
            current["settings"][key.strip()] = value.strip()
    return blocks


def _scalar(text: str):
    return int(text) if re.fullmatch(r"-?\d+", text) else text


def _parse_collection(opt_cls, raw: str):
    """Parse a dict/list/set option from its spoiler text, or None if it doesn't round-trip."""
    from Options import OptionDict, OptionList

    parts = raw.split(", ") if raw else []
    if issubclass(opt_cls, OptionDict):
        value = {}
        for part in parts:
            if ": " not in part:
                return None
            key, val = part.rsplit(": ", 1)
            value[key] = _scalar(val)
    elif issubclass(opt_cls, OptionList):
        value = list(parts)
    else:
        value = set(parts)
    valid = getattr(opt_cls, "valid_keys", None)
    if valid and any(key not in valid for key in value):
        return None
    try:
        if opt_cls.get_option_name(value) != raw:
            return None
        opt_cls.from_any(value)  # must construct cleanly
    except Exception:  # noqa: BLE001 -- unparseable -> leave the option at its default
        return None
    # The plain value, not the option's own (a Counter, for counters): the rebuild converts it
    # with from_any again, and OptionDict only accepts a real dict.
    return value


def resolve_options(world_type, settings: dict) -> dict:
    """Convert a parsed settings block into {option_attr: value} for the given world.
    Scalar, dict, list and set options are resolved; plando options keep their defaults."""
    from Options import Toggle, Choice, Range, FreeText, OptionDict, OptionList, OptionSet  # lazy: needs AP
    scalar_types = (Toggle, Choice, Range, FreeText)
    collection_types = (OptionDict, OptionList, OptionSet)

    resolved: dict[str, Any] = {}
    type_hints = getattr(world_type.options_dataclass, "type_hints", {})
    for attr, opt_cls in type_hints.items():
        if not isinstance(opt_cls, type):
            continue
        # Mirror exactly how the spoiler chose the key: display_name, else the attr name.
        spoiler_key = getattr(opt_cls, "display_name", attr)
        if spoiler_key not in settings:
            continue
        raw = settings[spoiler_key]
        if issubclass(opt_cls, collection_types) and "Plando" not in opt_cls.__name__:
            value = _parse_collection(opt_cls, raw)
            if value is not None:
                resolved[attr] = value
            continue
        if not issubclass(opt_cls, scalar_types):
            continue
        if issubclass(opt_cls, Choice):
            # The spoiler writes a choice's display form ("Completely Random", "Defeat Gol And
            # Maia"), which from_text can't read back; match it against each value's rendering.
            value = next((v for v in opt_cls.name_lookup if opt_cls.get_option_name(v) == raw), None)
            if value is not None:
                resolved[attr] = value
                continue
        opt = None
        for converter in ("from_any", "from_text"):
            fn = getattr(opt_cls, converter, None)
            if fn is None:
                continue
            try:
                opt = fn(raw)
                break
            except Exception:  # noqa: BLE001 -- try the next converter / give up to default
                opt = None
        if opt is not None:
            resolved[attr] = opt.value
    return resolved
