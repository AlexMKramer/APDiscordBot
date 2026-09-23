"""Go-mode logic engine.

Given a game, its resolved options, and a player's current inventory, this rebuilds
the world's logic (NO item placement) and answers two questions:

  * are they in "go mode" (can they still logically reach their goal from here)?
  * if not, which progression items do they still need?

It only ever reports ITEM NAMES -- never locations or which world holds an item -- so
it cannot leak placement/routing spoilers. Reachability is computed purely over the
abstract logic graph + item pool, which never references placements.

This module must run inside an Archipelago environment (the AP source tree on sys.path
plus its dependencies). It is import-light at module load; AP imports happen lazily so
the caller controls sys.path first.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

# The generation stages that build a world's logic graph + item pool, stopping
# before fill (distribute_items_restrictive) so regular locations stay empty. Event
# / Victory locations are placed deterministically by create_items/generate_basic.
# Same list Universal Tracker runs.
BUILD_STEPS = (
    "generate_early",
    "create_regions",
    "create_items",
    "set_rules",
    "connect_entrances",
    "generate_basic",
)

# Cap on the per-request minimization work so a pathologically large pool can't hang.
MAX_MINIMIZATION_ITEMS = 600
# Above this many items in the minimal set, skip the full requirement decomposition.
# Decomposition is run once per slot (precomputed), so we allow large collect-everything
# pools; this is just a backstop against a pathological case.
MAX_CLASSIFY_ITEMS = 600


@dataclass
class SlotResult:
    slot: Optional[int]
    name: str
    game: str
    status: str                       # "ok" | "unsupported" | "error"
    in_go_mode: Optional[bool] = None
    items_needed: list[dict] = field(default_factory=list)  # [{"name": str, "count": int}]
    reason: str = ""                  # why unsupported / error
    progression_pool: int = 0
    unknown_inventory: list[str] = field(default_factory=list)
    options_source: str = ""          # where the resolved options came from
    regen: str = ""                   # how the world was rebuilt: seed / seed+ut(...) / ut(...)
                                      # "seed" = the world's real RNG was replayed
    mismatch: Optional[int] = None    # differences from the seed's record of the slot (0 = exact)
    # Structured view of items_needed: which are strictly required vs "N of a group".
    requirements: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "slot": self.slot,
            "name": self.name,
            "game": self.game,
            "status": self.status,
            "in_go_mode": self.in_go_mode,
            "items_needed": self.items_needed,
            "requirements": self.requirements,
            "reason": self.reason,
            "progression_pool": self.progression_pool,
            "unknown_inventory": self.unknown_inventory,
            "options_source": self.options_source,
            "regen": self.regen,
            "mismatch": self.mismatch,
        }


def _describe_source(slot_data_opts: dict, spoiler_opts: dict) -> str:
    """Human-readable note about where the resolved options came from."""
    parts = []
    if slot_data_opts:
        parts.append(f"slot_data({len(slot_data_opts)})")
    spoiler_only = [k for k in spoiler_opts if k not in slot_data_opts]
    if spoiler_only:
        parts.append(f"spoiler({len(spoiler_only)})")
    if not parts:
        return "defaults"
    return "+".join(parts)


def _build_multiworld(world_type, options: dict, player_name: str, passthrough, rng=None):
    """One no-fill generation of a single slot, the way Universal Tracker's TMain does it:
    flagged as fake generation, and with the seed's slot data handed back to the world when
    it supports UT regeneration (`re_gen_passthrough`).

    `rng` is (generation seed, slot, players). With it the world gets the same RNG it had in
    the real generation, so its random choices (which weapons are progression, shuffled
    entrances, rolled counts) come out the same even for worlds without UT support."""
    from argparse import Namespace
    from random import Random
    from BaseClasses import CollectionState, MultiWorld
    from worlds.AutoWorld import World, call_all
    from worlds.generic.Rules import exclusion_rules

    multiworld = MultiWorld(1)
    multiworld.generation_is_fake = True
    if passthrough is not None:
        multiworld.re_gen_passthrough = {world_type.game: passthrough}
    # UT's "off" mode: every entrance gets connected. We have no found-entrance data, and
    # go mode means beatable with full knowledge, so nothing stays deferred.
    multiworld.enforce_deferred_connections = "off"
    multiworld.set_seed(rng[0] if rng else 0)
    multiworld.game = {1: world_type.game}
    multiworld.player_name = {1: player_name or "Player1"}
    args = Namespace()
    for key, option in world_type.options_dataclass.type_hints.items():
        setattr(args, key, {1: option.from_any(options.get(key, option.default))})
    multiworld.set_options(args)
    if rng:
        # Real generation seeds the multiworld RNG with the seed, then each world, in player
        # order, takes Random(multiworld.random.getrandbits(64)). Replay the slot's own draw,
        # and leave the multiworld RNG where it was once every world had drawn.
        gen_seed, slot, players = rng
        draws = Random(gen_seed)
        for _ in range(slot - 1):
            draws.getrandbits(64)
        multiworld.worlds[1].random = Random(draws.getrandbits(64))
        for _ in range(max(players, slot) - 1):
            multiworld.random.getrandbits(64)
    multiworld.state = CollectionState(multiworld)
    for step in BUILD_STEPS:
        if not hasattr(World, step):
            continue
        call_all(multiworld, step)
        if step == "set_rules":
            exclude = getattr(multiworld.worlds[1].options, "exclude_locations", None)
            if exclude is not None:
                exclusion_rules(multiworld, 1, exclude.value)
    return multiworld


def _regen_like_ut(world_type, options: dict, player_name: str, slot_data: dict, rng=None):
    """Rebuild a slot the way Universal Tracker does, so worlds that randomize their logic
    structure during generation (regions, chapter order, entrances, goal counts) reproduce
    this seed's structure instead of whatever seed 0 rolls.

    Mirrors UT's regen: yaml-less worlds get the raw slot_data as the passthrough, then any
    world with interpret_slot_data is rebuilt with what it returns. Returns (multiworld, how)."""
    import inspect

    static_isd = isinstance(inspect.getattr_static(world_type, "interpret_slot_data", None),
                            (staticmethod, classmethod))
    base = slot_data if (slot_data and getattr(world_type, "ut_can_gen_without_yaml", False)) else None
    base_how = "slot_data" if base is not None else "none"

    if static_isd:
        interpreted = world_type.interpret_slot_data(slot_data) if slot_data else None
        if interpreted:
            multiworld = _build_multiworld(world_type, options, player_name, interpreted, rng)
            return multiworld, "interpret_slot_data"
        return _build_multiworld(world_type, options, player_name, base, rng), base_how

    multiworld = _build_multiworld(world_type, options, player_name, base, rng)
    isd = getattr(multiworld.worlds[1], "interpret_slot_data", None)
    if slot_data and callable(isd):
        interpreted = isd(slot_data)
        if interpreted:
            multiworld = _build_multiworld(world_type, options, player_name, interpreted, rng)
            return multiworld, "interpret_slot_data"
    return multiworld, base_how


@dataclass
class PreparedSlot:
    """A slot's rebuilt world, ready for any number of go-mode checks."""
    multiworld: object
    world_type: type
    universe: list          # every progression Item the player can ever hold (pool + start items)
    prog_names: set         # names that are progression somewhere in the universe
    templates: dict         # item name -> an Item the rebuild made, for worlds without create_item


def _make_item(world, name: str, templates: dict):
    """A new Item by name. Some worlds (Gamer Connections) never implement create_item and build
    their items directly, so fall back to copying one the rebuild already made."""
    import copy
    try:
        return world.create_item(name)
    except NotImplementedError:
        if name not in templates:
            raise
        item = copy.copy(templates[name])
        item.location = None
        return item


def _finish(multiworld, world_type, precollected):
    """Turn a rebuilt multiworld into a PreparedSlot, or return (None, reason) if it fails a
    guardrail. Strips start items (the tracker inventory supplies them) and collects the
    universe of progression items the player can ever hold."""
    from BaseClasses import CollectionState

    player = 1  # solo multiworld
    world = multiworld.worlds[player]
    templates = {}
    for item in [*multiworld.itempool, *multiworld.precollected_items[player],
                 *(loc.item for loc in multiworld.get_locations(player) if loc.item)]:
        templates.setdefault(item.name, item)

    # Start inventory: the tracker's received list already includes it (the web tracker adds
    # the multidata's precollected items to every inventory), so like UT we strip every real
    # start item from the rebuilt world and let the inventory supply it. Only events stay.
    world_start = [it for it in multiworld.precollected_items[player] if it.code is not None]
    multiworld.precollected_items[player] = [it for it in multiworld.precollected_items[player]
                                             if it.code is None]

    # Guardrail 1: the world must define a real goal. The default completion_condition
    # is `lambda state: True`; if an empty state already "beats" the game, the goal was
    # never set (or this world needs setup we skipped), so we must not claim go-mode.
    if multiworld.can_beat_game(CollectionState(multiworld)):
        return None, "World has no gating goal in logic (cannot determine go-mode reliably)."

    # Everything the player can ever hold: the item pool plus their start inventory (the
    # world's own start items, plus yaml/randomized start inventory from the multidata).
    start_items = list(world_start)
    already = Counter(it.code for it in world_start)
    id_to_name = getattr(world_type, "item_id_to_name", {}) or {}
    for code in precollected or []:
        code_i = int(code) if str(code).lstrip("-").isdigit() else code
        if already.get(code_i, 0) > 0:
            already[code_i] -= 1
            continue
        item_name = id_to_name.get(code_i)
        if not item_name:
            continue
        try:
            start_items.append(_make_item(world, item_name, templates))
        except Exception:  # noqa: BLE001 -- a start item we can't reconstruct is simply skipped
            pass
    universe = [item for item in multiworld.itempool if item.advancement]
    universe += [item for item in start_items if item.advancement]

    def beats_with(items):
        state = CollectionState(multiworld)
        for item in items:
            state.collect(item, prevent_sweep=True)
        return multiworld.can_beat_game(state)

    # Guardrail 2: holding everything must beat the game. Some worlds place key items themselves
    # in pre_fill (Ship of Harkinian's songs, dungeon rewards and keys), which we stop before,
    # so if the pool alone falls short, add the world's pre-fill items the way AP's fill counts
    # them. (Only then: other worlds' pre-fill lists repeat items already in the pool.)
    if not beats_with(universe):
        try:
            universe += [item for item in world.get_pre_fill_items() if item.advancement]
        except Exception:  # noqa: BLE001 -- a world whose pre-fill list needs pre_fill state
            pass
        if not beats_with(universe):
            # Still short: our reconstruction is missing something. Don't guess.
            return None, ("Goal is unreachable even with every progression item -- the logic "
                          "reconstruction is incomplete.")

    return PreparedSlot(multiworld=multiworld, world_type=world_type, universe=universe,
                        prog_names={item.name for item in universe}, templates=templates), ""


def _mismatch(multiworld, expected_locations: set, expected_prog: Counter) -> int:
    """How far a rebuilt slot is from what the real generation recorded: its location IDs, and
    the progression items it owns (placed anywhere). 0 means the same world."""
    world = multiworld.worlds[1]
    locations = [loc for loc in multiworld.get_locations(1) if loc.address is not None]
    ours = Counter(item.code for item in multiworld.itempool
                   if item.advancement and item.code is not None)
    ours += Counter(loc.item.code for loc in locations
                    if loc.item and loc.item.advancement and loc.item.code is not None)
    # Items a world places in its own pre_fill (which we don't run) are either separate from
    # the pool or drawn from it, depending on the world; take whichever reading matches.
    try:
        pre_fill = Counter(item.code for item in world.get_pre_fill_items()
                           if item.advancement and item.code is not None)
    except Exception:  # noqa: BLE001
        pre_fill = Counter()
    # Real generation moves start_inventory_from_pool items out of the pool, which we don't do.
    from_pool = getattr(world.options, "start_inventory_from_pool", None)
    if from_pool:
        ours -= Counter({world.item_name_to_id[name]: count for name, count in from_pool.value.items()
                         if name in world.item_name_to_id})
    location_diff = len({loc.address for loc in locations} ^ set(expected_locations))
    item_diff = min(sum(((held - expected_prog) + (expected_prog - held)).values())
                    for held in (ours, ours + pre_fill))
    return location_diff + item_diff


def prepare_slot(game: str, options: dict, *, slot: Optional[int] = None, name: str = "",
                 spoiler_settings: Optional[dict] = None, precollected: Optional[list] = None,
                 slot_data: Optional[dict] = None, datapackage_checksum: Optional[str] = None,
                 gen_seed: Optional[int] = None, players: int = 0,
                 expected_locations: Optional[set] = None, expected_prog: Optional[Counter] = None,
                 result: Optional[SlotResult] = None):
    """Rebuild one slot's logic. Returns (PreparedSlot or None, SlotResult); on None the result
    carries the unsupported/error status and reason.

    With the seed's record of the slot (`expected_locations`, `expected_prog`), each way of
    rebuilding is checked against it and the first exact match is used."""
    # Lazy AP imports -- the caller is responsible for putting the (version-pinned) AP
    # source on sys.path before calling this.
    from worlds.AutoWorld import AutoWorldRegister
    import spoiler_options

    result = result or SlotResult(slot=None, name=name or "", game=game, status="error")

    world_type = AutoWorldRegister.world_types.get(game)
    if world_type is None:
        result.status = "unsupported"
        result.reason = (f"World '{game}' is not loaded. Its apworld may be missing or "
                         f"built for a different Archipelago version.")
        return None, result
    if getattr(world_type, "disable_ut", False):
        result.status = "unsupported"
        result.reason = "The world's author has disabled tracker regeneration for this game."
        return None, result

    # The installed apworld must be the one that generated the seed. A different version can
    # have different logic, and would silently answer with the wrong rules.
    if datapackage_checksum:
        installed = world_type.get_data_package_data().get("checksum")
        if installed != datapackage_checksum:
            result.status = "unsupported"
            result.reason = ("The installed apworld doesn't match the one that generated this seed "
                             "(datapackage checksum differs). Install the same apworld version.")
            return None, result

    # Resolve options: spoiler-recovered as a base, slot_data overriding it (slot_data is
    # exact/typed; the spoiler is parsed from text). Anything still missing -> world default.
    slot_data_opts = dict(options or {})
    spoiler_opts = spoiler_options.resolve_options(world_type, spoiler_settings or {})
    merged = {**spoiler_opts, **slot_data_opts}

    # A spoiler-recovered value can occasionally break the build (a mis-converted option).
    # Try the richest option set first, then fall back to slot_data-only, then defaults,
    # so the world at least builds.
    attempts = [(merged, _describe_source(slot_data_opts, spoiler_opts))]
    if slot_data_opts and slot_data_opts != merged:
        attempts.append((slot_data_opts, "slot_data only (spoiler dropped: build failed)"))
    attempts.append(({}, "defaults (recovered options dropped: build failed)"))

    # Ways to rebuild, most faithful first. With the generation seed the world replays its real
    # RNG and runs exactly the code the real generation ran. UT's slot data passthrough covers
    # what that can't (settings the spoiler doesn't carry) but switches some worlds onto their
    # UT code path, and without the seed it's all UT itself has.
    rng = (gen_seed, slot, players) if gen_seed is not None and slot else None
    candidates = []
    if rng:
        candidates += [("seed", rng, {}), ("seed+ut", rng, slot_data or {})]
    candidates.append(("ut", None, slot_data or {}))
    checkable = bool(expected_locations or expected_prog)

    best = None      # (mismatch, PreparedSlot, label, source)
    failures = []
    for label, cand_rng, cand_slot_data in candidates:
        for opts, source in attempts:
            try:
                multiworld, how = _regen_like_ut(world_type, opts, name, cand_slot_data, cand_rng)
                prepared, why = _finish(multiworld, world_type, precollected)
            except Exception as exc:  # noqa: BLE001 -- try the next, less-faithful option set
                failures.append(f"{label}: {type(exc).__name__}: {exc}")
                continue
            if prepared is None:
                failures.append(f"{label}: {why}")
                break
            mismatch = (_mismatch(prepared.multiworld, expected_locations or set(),
                                  expected_prog or Counter()) if checkable else None)
            tag = label if how == "none" or label == "seed" else f"{label}({how})"
            if best is None or (mismatch is not None and mismatch < best[0]):
                best = (mismatch, prepared, tag, source)
            break
        if best is not None and not best[0]:
            break  # an exact match (or nothing to check against): take it

    if best is None:
        # Nothing built past the guardrails. Report the most faithful attempt's reason.
        reason = failures[0] if failures else "no rebuild succeeded"
        result.status = "error" if "Error" in reason or "Exception" in reason else "unsupported"
        result.reason = reason.split(": ", 1)[1] if ": " in reason else reason
        return None, result

    mismatch, prepared, tag, source = best
    if mismatch and source.startswith("defaults"):
        # The real settings didn't build, and a default-settings world is a different game.
        result.status = "unsupported"
        result.reason = ("The slot's settings didn't rebuild, and a default-settings rebuild doesn't "
                         f"match the seed ({failures[0] if failures else 'no detail'}).")
        return None, result
    # Some worlds settle which copies are progression after generate_basic. The seed says which
    # names were progression for this slot; count those too, as UT does with the server's flags.
    id_to_name = getattr(world_type, "item_id_to_name", {}) or {}
    prepared.prog_names |= {id_to_name[code] for code in (expected_prog or {}) if code in id_to_name}
    result.status = "ok"
    result.regen = tag
    result.options_source = source
    result.mismatch = mismatch
    result.progression_pool = len(prepared.universe)
    return prepared, result


def inventory_state(prepared: PreparedSlot, inventory: dict):
    """A CollectionState holding the player's inventory ({item_name: count}), built the way UT
    builds it. Returns (state, unknown_item_names)."""
    from BaseClasses import CollectionState, ItemClassification

    world = prepared.multiworld.worlds[1]
    state = CollectionState(prepared.multiworld)
    unknown = []
    for item_name, count in inventory.items():
        try:
            count = int(count)
        except (TypeError, ValueError):
            count = 0
        if count <= 0:
            continue
        if item_name not in prepared.world_type.item_name_to_id:
            unknown.append(item_name)
            continue
        for _ in range(count):
            try:
                item = _make_item(world, item_name, prepared.templates)
            except Exception:  # noqa: BLE001 -- can't make it at all: report it, don't guess
                unknown.append(item_name)
                break
            # UT ORs in the server's item flags. The tracker page doesn't carry them per copy,
            # so any copy of a name that's progression for this slot counts: in the game, every
            # copy you hold works (e.g. any 16 strawberries finish Celeste 64, even though the
            # generator only flagged 16 specific copies as progression).
            if not item.advancement and item_name in prepared.prog_names:
                item.classification |= ItemClassification.progression
            state.collect(item, prevent_sweep=True)
    return state, unknown


def analyze_slot(game: str, options: dict, inventory: dict, *, slot: Optional[int] = None,
                 name: str = "", spoiler_settings: Optional[dict] = None,
                 precollected: Optional[list] = None, slot_data: Optional[dict] = None,
                 datapackage_checksum: Optional[str] = None, gen_seed: Optional[int] = None,
                 players: int = 0, expected_locations: Optional[set] = None,
                 expected_prog: Optional[Counter] = None, fast: bool = False) -> SlotResult:
    """Analyze one slot.

    `options` is the slot's resolved slot_data options (may be empty). `spoiler_settings`
    is the raw {display name: value} block parsed from the spoiler, used to recover
    options the world didn't put in slot_data. slot_data values win where both exist.
    `slot_data` is the slot's full slot data, handed to the world for UT-style regeneration.
    `gen_seed`/`players` (from the spoiler) let the slot's world replay its real RNG, and
    `expected_locations`/`expected_prog` (from the multidata) check the rebuild against it.
    `inventory` is {item_name: count}, as the tracker shows it (start inventory included).

    `fast=True` returns as soon as the go-mode boolean is known, skipping the expensive
    minimization + requirement decomposition. Used by the go-mode notification loop, which
    only needs `in_go_mode` (the same build + guardrails still run, so the answer is exact).
    """
    result = SlotResult(slot=slot, name=name or "", game=game, status="error")
    prepared, result = prepare_slot(game, options, slot=slot, name=name,
                                    spoiler_settings=spoiler_settings, precollected=precollected,
                                    slot_data=slot_data, datapackage_checksum=datapackage_checksum,
                                    gen_seed=gen_seed, players=players,
                                    expected_locations=expected_locations,
                                    expected_prog=expected_prog, result=result)
    if prepared is None:
        return result

    multiworld = prepared.multiworld
    current, result.unknown_inventory = inventory_state(prepared, inventory)

    # Are they already in go-mode? UT's test: what they hold, plus a sweep of their own event
    # (and locked) locations, against the goal.
    if multiworld.can_beat_game(current):
        result.in_go_mode = True
        result.items_needed = []
        return result

    # Not yet -- find a minimal set of still-needed progression items, then classify each
    # as strictly required vs interchangeable ("N of a group") so the bot doesn't present a
    # fungible pick (e.g. one of many worlds) as if it were mandatory.
    result.in_go_mode = False
    if fast:
        # Notification fast-path: the caller only needs the go-mode boolean.
        return result
    remaining = _remaining_pool(prepared.universe, inventory)

    if len(remaining) > MAX_MINIMIZATION_ITEMS:
        # Safety valve: don't attempt an unbounded minimization. Report the whole
        # remaining progression set rather than hang.
        counts = Counter(item.name for item in remaining)
        result.items_needed = [{"name": n, "count": c, "approximate": True} for n, c in sorted(counts.items())]
        result.requirements = {"required": result.items_needed, "choices": [], "approximate": True}
        return result

    minimal_items = _minimize(multiworld, current, remaining)
    result.items_needed = _aggregate(minimal_items)
    if len(minimal_items) > MAX_CLASSIFY_ITEMS:
        # Almost certainly a "collect (nearly) everything" goal; don't decompose.
        result.requirements = {"verified": False, "required": [], "example_path": result.items_needed,
                               "has_alternatives": None,
                               "note": "too many items to decompose; one valid set shown"}
        return result

    # Discover a full requirement tree (routes + N-of-group), then trust it only if it
    # provably reproduces the can_beat_game oracle on random item-sets.
    import requirements
    world = multiworld.worlds[1]
    tree, verified = requirements.discover(multiworld.can_beat_game, current, remaining,
                                           getattr(world, "item_name_groups", {}) or {})
    if verified and tree is not None:
        result.requirements = {"verified": True, "tree": tree}
    else:
        # Unverified -> fall back to the conservative split (never overclaims).
        req = _classify_requirements(multiworld, current, remaining, minimal_items)
        req["verified"] = False
        if tree is not None:
            req["unverified_tree"] = tree
        result.requirements = req
    return result


def _remaining_pool(prog_pool, inventory) -> list:
    """Progression items not already covered by the inventory (matched by name)."""
    held = Counter({k: int(v) for k, v in inventory.items() if str(v).lstrip("-").isdigit()})
    remaining = []
    for item in prog_pool:
        if held.get(item.name, 0) > 0:
            held[item.name] -= 1
        else:
            remaining.append(item)
    return remaining


def _minimize(multiworld, base_state, remaining) -> list:
    """Greedy item-removal (AP's create_playthrough pattern): drop any item whose removal
    still leaves the goal reachable, returning a minimal sufficient set of Items."""
    def beats_with(items):
        state = base_state.copy()
        for it in items:
            state.collect(it, prevent_sweep=True)
        return multiworld.can_beat_game(state)

    required = list(remaining)
    for candidate in list(required):
        trial = [it for it in required if it is not candidate]
        if beats_with(trial):
            required.remove(candidate)
    return required


def _aggregate(items) -> list[dict]:
    counts = Counter(it.name for it in items)
    return [{"name": n, "count": c} for n, c in sorted(counts.items())]


def _classify_requirements(multiworld, base_state, remaining, minimal_items) -> dict:
    """Separate the minimal set into items that are STRICTLY required (needed in EVERY way
    to win) versus items specific to THIS particular completion path.

    A flat "required items" list can't faithfully represent goals with alternate routes or
    collective ("any N of a group") requirements -- e.g. Kingdom Hearts can be finished via
    the normal End-of-the-World + puppies route OR the Destiny Islands homecoming route. So
    rather than overclaim a group structure, we report: the always-required items, one
    concrete example path for the rest, and a flag that alternatives exist."""
    minimal_counts = Counter(it.name for it in minimal_items)

    def beats(items):
        state = base_state.copy()
        for it in items:
            state.collect(it, prevent_sweep=True)
        return multiworld.can_beat_game(state)

    # Strict := removing every copy of this item from the FULL remaining pool still can't
    # win, i.e. there is no alternative anywhere -> it is needed on every path.
    strict = {}
    for name in minimal_counts:
        without = [it for it in remaining if it.name != name]
        strict[name] = not beats(without)

    required = [{"name": n, "count": minimal_counts[n]} for n in sorted(minimal_counts) if strict[n]]
    example_path = [{"name": n, "count": minimal_counts[n]} for n in sorted(minimal_counts) if not strict[n]]

    return {
        # Items needed no matter how you finish.
        "required": required,
        # One concrete set of additional items that completes the goal; NOT the only way --
        # any of these may be substitutable (other worlds, an alternate win route, etc.).
        "example_path": example_path,
        "has_alternatives": bool(example_path),
    }
