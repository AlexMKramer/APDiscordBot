"""Load a generated Archipelago seed (the AP_<seed>.zip the host produces at generation).

Extracts, per slot: the game, the resolved options that the world author chose to put in
slot_data, the starting (pre-collected) inventory, and the seed's AP version stamp. Also
exposes the spoiler text, which the (future) spoiler-settings parser will use to recover
resolved options for worlds that put nothing in slot_data.

Runs inside an AP environment (uses Utils.restricted_loads); AP imports are lazy so the
caller controls sys.path first.
"""
from __future__ import annotations

import zipfile
import zlib
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SlotData:
    slot: int
    name: str
    game: str
    options: dict = field(default_factory=dict)           # resolved options from slot_data (may be empty)
    precollected: list = field(default_factory=list)      # starting-inventory item IDs (codes)
    spoiler_settings: dict = field(default_factory=dict)  # raw {display name: value} from the spoiler block


@dataclass
class SeedData:
    seed_name: str
    version: tuple                  # e.g. (0, 6, 7)
    race_mode: int
    slots: dict                     # {slot_number: SlotData}
    spoiler_text: Optional[str] = None

    @property
    def version_str(self) -> str:
        return ".".join(str(p) for p in self.version)

    def find_slot(self, name: str) -> Optional[SlotData]:
        """Case-insensitive lookup of a slot by its name."""
        lowered = name.strip().lower()
        for sd in self.slots.values():
            if sd.name.lower() == lowered:
                return sd
        return None


def _read_multidata(zip_path: str):
    from Utils import restricted_loads  # lazy: needs AP on sys.path
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        md_name = next(n for n in names if n.endswith(".archipelago"))
        decoded = restricted_loads(zlib.decompress(zf.read(md_name)[1:]))
        spoiler = None
        sp = next((n for n in names if n.endswith("_Spoiler.txt")), None)
        if sp:
            spoiler = zf.read(sp).decode("utf-8-sig", "replace")
    return decoded, spoiler


def load_seed(zip_path: str) -> SeedData:
    import spoiler_options  # pure text parsing, no AP needed

    decoded, spoiler = _read_multidata(zip_path)

    slot_info = decoded.get("slot_info", {})
    slot_data = decoded.get("slot_data", {})
    precollected = decoded.get("precollected_items", {})
    blocks = spoiler_options.parse_player_blocks(spoiler)

    slots = {}
    for sid, info in slot_info.items():
        sd = slot_data.get(sid, {})
        options = sd.get("options", {}) if isinstance(sd, dict) else {}
        slots[sid] = SlotData(
            slot=sid,
            name=getattr(info, "name", str(info)),
            game=getattr(info, "game", "Unknown"),
            options=dict(options) if isinstance(options, dict) else {},
            precollected=list(precollected.get(sid, []) or []),
            spoiler_settings=blocks.get(sid, {}).get("settings", {}),
        )

    return SeedData(
        seed_name=str(decoded.get("seed_name", "")),
        version=tuple(decoded.get("version", ()) or ()),
        race_mode=int(decoded.get("race_mode", 0) or 0),
        slots=slots,
        spoiler_text=spoiler,
    )
