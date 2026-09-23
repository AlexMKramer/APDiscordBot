"""Load a generated Archipelago seed (the AP_<seed>.zip the host produces at generation).

Extracts, per slot: the game, the resolved options that the world author chose to put in
slot_data, the starting (pre-collected) inventory, and the seed's AP version stamp. Also
exposes the spoiler text, which the (future) spoiler-settings parser will use to recover
resolved options for worlds that put nothing in slot_data.

Runs inside an AP environment (uses Utils.restricted_loads); AP imports are lazy so the
caller controls sys.path first.
"""
from __future__ import annotations

import re
import zipfile
import zlib
from collections import Counter
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
    slot_data: dict = field(default_factory=dict)         # the slot's full slot_data (UT regen passthrough)
    datapackage: dict = field(default_factory=dict)       # the game's datapackage the seed was generated with
    # What the real generation produced for this slot, to check a rebuild against:
    location_ids: set = field(default_factory=set)        # the slot's real location IDs
    prog_item_ids: Counter = field(default_factory=Counter)  # its progression items placed anywhere


@dataclass
class SeedData:
    seed_name: str
    version: tuple                  # e.g. (0, 6, 7)
    race_mode: int
    slots: dict                     # {slot_number: SlotData}
    spoiler_text: Optional[str] = None
    gen_seed: Optional[int] = None  # the generation seed, from the spoiler header
    players: int = 0                # player slots (worlds are created for 1..players, in order)

    @property
    def version_str(self) -> str:
        return ".".join(str(p) for p in self.version)

    def engine_kwargs(self, sd: SlotData) -> dict:
        """Everything engine.analyze_slot / prepare_slot needs to rebuild this slot faithfully."""
        return {
            "slot": sd.slot, "name": sd.name, "spoiler_settings": sd.spoiler_settings,
            "precollected": sd.precollected, "slot_data": sd.slot_data,
            "datapackage": sd.datapackage,
            "gen_seed": None if self.race_mode else self.gen_seed, "players": self.players,
            "expected_locations": sd.location_ids, "expected_prog": sd.prog_item_ids,
        }

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
    datapackage = decoded.get("datapackage", {}) or {}
    blocks = spoiler_options.parse_player_blocks(spoiler)

    # locations: {slot: {location_id: (item_id, receiving_slot, flags)}}; flag 0b1 = progression.
    location_ids: dict = {}
    prog_item_ids: dict = {}
    for sid, locs in (decoded.get("locations", {}) or {}).items():
        location_ids[sid] = set(locs)
        for item_id, receiver, flags in locs.values():
            if flags & 0b1:
                prog_item_ids.setdefault(receiver, Counter())[item_id] += 1

    slots = {}
    for sid, info in slot_info.items():
        sd = slot_data.get(sid, {})
        options = sd.get("options", {}) if isinstance(sd, dict) else {}
        game = getattr(info, "game", "Unknown")
        slots[sid] = SlotData(
            slot=sid,
            name=getattr(info, "name", str(info)),
            game=game,
            options=dict(options) if isinstance(options, dict) else {},
            precollected=list(precollected.get(sid, []) or []),
            spoiler_settings=blocks.get(sid, {}).get("settings", {}),
            slot_data=dict(sd) if isinstance(sd, dict) else {},
            datapackage=datapackage.get(game) or {},
            location_ids=location_ids.get(sid, set()),
            prog_item_ids=prog_item_ids.get(sid, Counter()),
        )

    # The spoiler header carries the generation seed ("Archipelago Version X  -  Seed: N"). With
    # it, each slot's world RNG can be replayed exactly (see engine._build_multiworld).
    gen_seed = None
    match = re.search(r"Seed:\s*(\d+)", (spoiler or "")[:300])
    if match:
        gen_seed = int(match.group(1))
    # Every player and spectator gets a world (and an RNG draw); item-link groups come after
    # them and don't.
    players = sum(1 for info in slot_info.values()
                  if not int(getattr(info, "type", 1)) & 0b10)

    return SeedData(
        seed_name=str(decoded.get("seed_name", "")),
        version=tuple(decoded.get("version", ()) or ()),
        race_mode=int(decoded.get("race_mode", 0) or 0),
        slots=slots,
        spoiler_text=spoiler,
        gen_seed=gen_seed,
        players=players,
    )
