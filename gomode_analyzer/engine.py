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


def analyze_slot(game: str, options: dict, inventory: dict, *, slot: Optional[int] = None,
                 name: str = "", spoiler_settings: Optional[dict] = None,
                 precollected: Optional[list] = None, fast: bool = False) -> SlotResult:
    """Analyze one slot.

    `options` is the slot's resolved slot_data options (may be empty). `spoiler_settings`
    is the raw {display name: value} block parsed from the spoiler, used to recover
    options the world didn't put in slot_data. slot_data values win where both exist.
    `inventory` is {item_name: count}. Missing options fall back to world defaults.

    `fast=True` returns as soon as the go-mode boolean is known, skipping the expensive
    minimization + requirement decomposition. Used by the go-mode notification loop, which
    only needs `in_go_mode` (the same build + guardrails still run, so the answer is exact).
    """
    # Lazy AP imports -- the caller is responsible for putting the (version-pinned) AP
    # source on sys.path before calling this.
    from BaseClasses import CollectionState
    from worlds.AutoWorld import AutoWorldRegister
    from test.general import setup_multiworld
    import spoiler_options

    result = SlotResult(slot=slot, name=name or "", game=game, status="error")

    world_type = AutoWorldRegister.world_types.get(game)
    if world_type is None:
        result.status = "unsupported"
        result.reason = (f"World '{game}' is not loaded. Its apworld may be missing or "
                         f"built for a different Archipelago version.")
        return result

    # Resolve options: spoiler-recovered as a base, slot_data overriding it (slot_data is
    # exact/typed; the spoiler is parsed from text). Anything still missing -> world default.
    slot_data_opts = dict(options or {})
    spoiler_opts = spoiler_options.resolve_options(world_type, spoiler_settings or {})
    merged = {**spoiler_opts, **slot_data_opts}

    # A spoiler-recovered value can occasionally break the build (a mis-converted option).
    # Try the richest option set first, then fall back to slot_data-only, then defaults,
    # so Phase 2 never regresses below "the world at least builds".
    attempts = [(merged, _describe_source(slot_data_opts, spoiler_opts))]
    if slot_data_opts and slot_data_opts != merged:
        attempts.append((slot_data_opts, "slot_data only (spoiler dropped: build failed)"))
    attempts.append(({}, "defaults (recovered options dropped: build failed)"))

    multiworld = None
    last_exc = None
    for opts, source in attempts:
        try:
            multiworld = setup_multiworld(world_type, steps=BUILD_STEPS, seed=0, options=opts)
            result.options_source = source
            break
        except Exception as exc:  # noqa: BLE001 -- try the next, less-faithful option set
            last_exc = exc
    if multiworld is None:
        result.status = "error"
        result.reason = f"Failed to build logic: {type(last_exc).__name__}: {last_exc}"
        return result

    player = 1  # solo multiworld

    # Guardrail 1: the world must define a real goal. The default completion_condition
    # is `lambda state: True`; if an empty state already "beats" the game, the goal was
    # never set (or this world needs setup we skipped), so we must not claim go-mode.
    # NOTE: run this BEFORE crediting start inventory, so a slot with a generous start
    # inventory can't be misread as having no gating goal.
    try:
        if multiworld.can_beat_game(CollectionState(multiworld)):
            result.status = "unsupported"
            result.reason = "World has no gating goal in logic (cannot determine go-mode reliably)."
            return result
    except Exception as exc:  # noqa: BLE001
        result.status = "error"
        result.reason = f"Goal check failed: {type(exc).__name__}: {exc}"
        return result

    prog_pool = [item for item in multiworld.itempool if item.advancement]
    result.progression_pool = len(prog_pool)

    # Guardrail 2: collecting the entire progression pool must beat the game. If it
    # doesn't, our reconstruction is missing something (classically: entrance
    # randomization whose real connections we don't have). Don't guess.
    full_state = CollectionState(multiworld)
    for item in prog_pool:
        full_state.collect(item, prevent_sweep=True)
    if not multiworld.can_beat_game(full_state):
        result.status = "unsupported"
        result.reason = ("Goal is unreachable even with every progression item -- the "
                         "logic reconstruction is incomplete (often entrance randomization). "
                         "Faithful support needs the seed's entrance data.")
        return result

    # Credit the slot's ACTUAL start inventory (after the guardrails). setup_multiworld runs
    # the world stages but NOT core generation's start-inventory step, so a world's own
    # push_precollected is included, yet yaml start_inventory / start_inventory_from_pool (and
    # randomized start inventory) are not. Inject the multidata's ground-truth precollected for
    # anything the rebuild didn't already credit, so go-mode + requirements account for items
    # the player holds from the start.
    if precollected:
        world = multiworld.worlds[player]
        id_to_name = getattr(world_type, "item_id_to_name", {}) or {}
        already = Counter(getattr(it, "code", None) for it in multiworld.precollected_items[player])
        for code in precollected:
            code_i = int(code) if str(code).lstrip("-").isdigit() else code
            if already.get(code_i, 0) > 0:
                already[code_i] -= 1   # the world already granted this start item
                continue
            item_name = id_to_name.get(code_i)
            if not item_name:
                continue
            try:
                multiworld.push_precollected(world.create_item(item_name))
            except Exception:  # noqa: BLE001 -- a start item we can't reconstruct is simply skipped
                pass

    # Seed the player's current inventory directly into a fresh state (now also carrying the
    # injected start inventory via CollectionState's precollected auto-collect).
    current = CollectionState(multiworld)
    unknown = []
    for item_name, count in inventory.items():
        try:
            count = int(count)
        except (TypeError, ValueError):
            count = 0
        if count <= 0:
            continue
        if item_name not in world_type.item_name_to_id:
            unknown.append(item_name)
            continue
        world = multiworld.worlds[player]
        for _ in range(count):
            current.collect(world.create_item(item_name), prevent_sweep=True)
    result.unknown_inventory = unknown

    # Are they already in go-mode?
    if multiworld.can_beat_game(current):
        result.status = "ok"
        result.in_go_mode = True
        result.items_needed = []
        return result

    # Not yet -- find a minimal set of still-needed progression items, then classify each
    # as strictly required vs interchangeable ("N of a group") so the bot doesn't present a
    # fungible pick (e.g. one of many worlds) as if it were mandatory.
    result.status = "ok"
    result.in_go_mode = False
    if fast:
        # Notification fast-path: the caller only needs the go-mode boolean.
        return result
    remaining = _remaining_pool(prog_pool, inventory)

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
    world = multiworld.worlds[player]
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
