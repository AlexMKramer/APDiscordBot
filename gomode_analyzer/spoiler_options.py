"""Recover a slot's *resolved* options from the spoiler.

Many worlds put nothing (or only a subset) in slot_data, so for those the player's
actual settings only survive in the spoiler's per-player block. Archipelago writes each
option as `"{display_name}:" {current_option_name}` (BaseClasses.Spoiler.write_option),
so we can invert it: build the same `display_name -> attribute` map from the world's
options dataclass and convert each value string back with the option's own
`from_any`/`from_text`.

We deliberately only resolve *scalar* option types (Toggle / Choice / Range / FreeText
and their subclasses such as NamedRange / TextChoice). Those are the ones that (a) gate
logic and (b) round-trip cleanly from the spoiler text. List/dict/plando options are
left at their defaults -- they mostly affect fill/placement, not the no-fill logic graph,
and don't reverse reliably from text.

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


def resolve_options(world_type, settings: dict) -> dict:
    """Convert a parsed settings block into {option_attr: value} for the given world.
    Only scalar option types are resolved; anything else is left to its default."""
    from Options import Toggle, Choice, Range, FreeText  # lazy: needs AP on sys.path
    scalar_types = (Toggle, Choice, Range, FreeText)

    resolved: dict[str, Any] = {}
    type_hints = getattr(world_type.options_dataclass, "type_hints", {})
    for attr, opt_cls in type_hints.items():
        if not (isinstance(opt_cls, type) and issubclass(opt_cls, scalar_types)):
            continue
        # Mirror exactly how the spoiler chose the key: display_name, else the attr name.
        spoiler_key = getattr(opt_cls, "display_name", attr)
        if spoiler_key not in settings:
            continue
        raw = settings[spoiler_key]
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
