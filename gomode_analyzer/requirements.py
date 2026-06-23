"""Discover a FULL, faithful representation of what a slot still needs to reach go mode.

The goal's logic is a black box: all we can do is ask `can_beat_game(itemset)` (a monotone
yes/no oracle). From queries alone -- no apworld-specific knowledge -- we build a structured
requirement *tree* and then VERIFY it against the oracle. If the tree doesn't reproduce the
oracle, we don't trust it (the caller falls back to a simpler answer).

Decomposition (threshold-first):
  1. strict items   -- needed on every path (with min counts).
  2. choice clauses -- per minimal-set item, discovered empirically:
       * a THRESHOLD ("any K of {point-sources}"), where a point-source may be a single
         item OR a bundle (e.g. {Atlantica, Crystal Trident}); route-critical worlds that
         also count toward the total are included so they aren't lost.
       * or a one-off CHOICE ("one of {...}", incl count-N like 2x Aero).
  3. route clause   -- alternate win routes (OR of bundles), discovered on a residual oracle.

Node shapes (JSON-serialisable):
  {"type": "item",    "name": str, "count": int}
  {"type": "atleast", "n": int, "options": [ {name: count, ...}, ... ]}   # each option is a bundle
  {"type": "all",     "children": [...]}     # AND
  {"type": "any",     "children": [...]}     # OR
"""
from __future__ import annotations

import itertools
import random

MAX_ROUTES = 12
VERIFY_SAMPLES = 160
MAX_GROUP_OPTIONS = 60


# --------------------------------------------------------------------------- evaluation
def satisfies(node: dict, held: dict) -> bool:
    t = node["type"]
    if t == "item":
        return held.get(node["name"], 0) >= node["count"]
    if t == "atleast":
        return sum(1 for opt in node["options"]
                   if all(held.get(k, 0) >= v for k, v in opt.items())) >= node["n"]
    if t == "all":
        return all(satisfies(c, held) for c in node["children"])
    if t == "any":
        return any(satisfies(c, held) for c in node["children"])
    raise ValueError(f"unknown node type {t!r}")


def _fmt_bundle(opt: dict) -> str:
    parts = [k if v == 1 else f"{k} x{v}" for k, v in sorted(opt.items())]
    return parts[0] if len(parts) == 1 else "(" + " + ".join(parts) + ")"


def render(node: dict, indent: int = 0) -> list[str]:
    pad = "  " * indent
    t = node["type"]
    if t == "item":
        c = node["count"]
        return [f"{pad}- {node['name']}" + (f" x{c}" if c != 1 else "")]
    if t == "atleast":
        opts = node["options"]
        shown = ", ".join(_fmt_bundle(o) for o in opts[:12])
        if len(opts) > 12:
            shown += f", +{len(opts) - 12} more"
        return [f"{pad}- any {node['n']} of: {shown}"]
    if t == "all":
        lines = []
        for c in node["children"]:
            lines += render(c, indent)
        return lines
    if t == "any":
        if all(c["type"] == "item" for c in node["children"]):
            opts = ", ".join((f"{c['name']} x{c['count']}" if c["count"] != 1 else c["name"])
                             for c in node["children"])
            return [f"{pad}- one of: {opts}"]
        lines = [f"{pad}- ONE of these routes:"]
        for i, c in enumerate(node["children"], 1):
            lines.append(f"{pad}  Route {i}:")
            lines += render(c, indent + 2)
        return lines
    return []


def render_requirements(req: dict) -> list[str]:
    """Player-facing summary of what's still needed to reach go mode."""
    if req.get("verified") and req.get("tree"):
        return ["To reach go mode, you still need:"] + render(req["tree"])

    def fmt(x):
        return x["name"] + (f" x{x['count']}" if x["count"] != 1 else "")

    required = req.get("required", [])
    example = req.get("example_path", [])
    if not required and not example:
        return ["You're in go mode -- nothing left to collect!"]

    lines = []
    if required:
        lines.append("You'll definitely need:")
        lines += [f"- {fmt(x)}" for x in required]
    if example:
        lines.append("Plus enough of these to finish (this is one working set -- "
                     "other combinations also work):")
        lines += [f"- {fmt(x)}" for x in example]
    return lines


def _node_key(node: dict):
    t = node["type"]
    if t == "item":
        return ("item", node["name"], node["count"])
    if t == "atleast":
        return ("atleast", node["n"], tuple(sorted(tuple(sorted(o.items())) for o in node["options"])))
    if t in ("all", "any"):
        return (t, tuple(sorted(_node_key(c) for c in node["children"])))
    return None


def _bundle_node(bundle: dict) -> dict:
    items = [{"type": "item", "name": n, "count": c} for n, c in sorted(bundle.items())]
    return items[0] if len(items) == 1 else {"type": "all", "children": items}


# --------------------------------------------------------------------------- discovery
def discover(can_beat, base_state, remaining, item_groups=None, log=None) -> tuple[dict | None, bool]:
    rng = random.Random(0)
    by_name: dict[str, list] = {}
    for it in remaining:
        by_name.setdefault(it.name, []).append(it)
    avail = {n: len(v) for n, v in by_name.items()}

    def wins(sel: dict) -> bool:
        state = base_state.copy()
        for name, count in sel.items():
            for it in by_name[name][:count]:
                state.collect(it, prevent_sweep=True)
        return can_beat(state)

    if wins({}):
        return {"type": "all", "children": []}, True
    if not wins(avail):
        return None, False

    def minimal(exclude=frozenset(), pin=frozenset()) -> dict | None:
        sel = {n: c for n, c in avail.items() if n not in exclude}
        if not wins(sel):
            return None
        for name in list(sel):
            if name in pin:
                continue
            without = {k: v for k, v in sel.items() if k != name}
            if wins(without):
                sel = without
                continue
            lo, hi = 0, sel[name]
            while lo + 1 < hi:
                mid = (lo + hi) // 2
                trial = dict(sel); trial[name] = mid
                if wins(trial):
                    hi = mid
                else:
                    lo = mid
            sel[name] = hi
        return sel

    # 1. Strict items + min counts.
    strict = {}
    for x in list(avail):
        if not wins({n: c for n, c in avail.items() if n != x}):
            strict[x] = _min_count(x, avail, wins)

    # 2. One minimal winning set (strict pinned) -> non-strict extras.
    seed = minimal(pin=frozenset(strict))
    if seed is None:
        return None, False
    extra = {x: seed[x] for x in seed if x not in strict}

    # 3. Per-extra alternatives; split choice vs route-defining items.
    alts = {x: _alternatives(seed, x, avail, wins) for x in extra}
    choice_items = [x for x in extra if all(len(b) == 1 for b in alts[x])]
    route_items = [x for x in extra if x not in choice_items]
    route_critical = set(route_items)
    for x in route_items:
        for b in alts[x]:
            route_critical.update(b)
    choice_items = [x for x in choice_items if x not in route_critical]

    # 4. Choice clauses (+ canonical fill). Strict items are excluded from threshold options
    #    (they're already required separately, so they must not inflate a "any k of" count).
    choice_clauses, choice_fill = _build_choices(choice_items, alts, seed, avail, route_critical,
                                                 set(strict), wins, minimal)

    # 5. Route clause on the residual.
    fill = {**strict, **choice_fill}
    strict_children = [{"type": "item", "name": n, "count": c} for n, c in sorted(strict.items())]

    def build(route_clause):
        ch = strict_children + list(choice_clauses)
        if route_clause is not None:
            ch.append(route_clause)
        return {"type": "all", "children": ch}

    # First try expressing the residual as a bundle-threshold ("any N of {bundles}", e.g.
    # Dead Rising's "any 20 survivors, each = mission + key"); only keep it if it verifies.
    bundle = _try_bundle_threshold(fill, avail, wins, rng)
    if bundle is not None:
        tree = build(bundle)
        if _verify(tree, avail, wins, minimal, rng):
            if log:
                log("bundle-threshold verified")
            return tree, True

    # Otherwise enumerate distinct routes.
    tree = build(_build_routes(fill, avail, wins, rng))
    verified = _verify(tree, avail, wins, minimal, rng)
    if log:
        log(f"strict={len(strict)} choices={len(choice_clauses)} verified={verified}")
    return tree, verified


def _min_count(x: str, avail: dict, wins) -> int:
    lo, hi = 0, avail[x]
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        trial = dict(avail); trial[x] = mid
        if wins(trial):
            hi = mid
        else:
            lo = mid
    return hi


def _minimize_addition(fixed: dict, addable: dict, wins) -> dict:
    add = dict(addable)
    for name in list(add):
        without = {k: v for k, v in add.items() if k != name}
        if wins({**fixed, **without}):
            add = without
            continue
        lo, hi = 0, add[name]
        while lo + 1 < hi:
            mid = (lo + hi) // 2
            trial = dict(add); trial[name] = mid
            if wins({**fixed, **trial}):
                hi = mid
            else:
                lo = mid
        add[name] = hi
    return add


def _alternatives(sel: dict, x: str, avail: dict, wins, cap: int = 16) -> list:
    fixed = {k: v for k, v in sel.items() if k != x}
    alts = [{x: sel[x]}]
    excluded = {x}
    while len(alts) < cap:
        addable = {n: avail[n] for n in avail if n not in fixed and n not in excluded}
        if not addable or not wins({**fixed, **addable}):
            break
        add = _minimize_addition(fixed, addable, wins)
        if not add:
            break
        alts.append(add)
        excluded.update(add)
    return alts


def _generalize_threshold(members, alts, seed, avail, route_critical, strict_names, wins, minimal):
    """Turn a single-item threshold cluster into a bundle threshold: enumerate ALL
    point-sources (singles, conditional bundles like {Atlantica, Crystal Trident}, and
    route-critical countable singles like EOTW/DI), and compute the count n. Strict items
    are NOT point-sources here -- they're required separately and would inflate the count."""
    cluster = list(members)
    sources = [{m: 1} for m in cluster]
    seen = set(cluster) | set(strict_names)

    # additional point-sources that can replace one cluster member's contribution. The
    # base route stays intact (just one point short), so every minimal addition is a single
    # world-point or a conditional bundle (e.g. {Atlantica, Crystal Trident}) -- never a
    # route swap (that would cost more than one item, so it's never minimal here).
    base = {k: v for k, v in seed.items() if k != cluster[0]}
    while len(sources) < MAX_GROUP_OPTIONS:
        addable = {n: avail[n] for n in avail if n not in base and n not in seen}
        if not addable or not wins({**base, **addable}):
            break
        add = _minimize_addition(base, addable, wins)
        if not add:
            break
        sources.append(add)
        seen.update(add)

    # route-critical worlds that ALSO count as points (tested via an alternate route)
    for r in route_critical:
        if r in seen:
            continue
        alt = minimal(frozenset([r]))
        if not alt:
            continue
        cm = next((c for c in cluster if c in alt), None)
        if cm is None:
            continue
        deficient = {k: v for k, v in alt.items() if k != cm}
        if wins({**deficient, r: 1}):
            sources.append({r: 1})
            seen.add(r)

    n = sum(1 for ps in sources if all(seed.get(k, 0) >= v for k, v in ps.items()))
    n = max(n, sum(1 for m in cluster if seed.get(m, 0) >= 1))

    node = {"type": "atleast", "n": n, "options": sources[:MAX_GROUP_OPTIONS]}
    # fill: n non-route-critical single point-sources
    singles = [next(iter(s)) for s in sources if len(s) == 1 and next(iter(s)) not in route_critical]
    fill = {name: 1 for name in singles[:n]}
    return node, fill


def _build_choices(choice_items, alts, seed, avail, route_critical, strict_names, wins, minimal):
    opt_names = {}
    for x in choice_items:
        names = set()
        for b in alts[x]:
            names.update(b)
        names -= route_critical
        names -= strict_names
        names.add(x)
        opt_names[x] = names

    parent = {x: x for x in choice_items}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for i, x in enumerate(choice_items):
        for y in choice_items[i + 1:]:
            if opt_names[x] & opt_names[y]:
                parent[find(x)] = find(y)

    clusters = {}
    for x in choice_items:
        clusters.setdefault(find(x), []).append(x)

    clauses, fill = [], {}

    def per_item_choices(members):
        # each member becomes its own "one of {its alternatives}" clause
        for x in members:
            bundles = {}
            for b in alts[x]:
                if not (set(b) <= route_critical):
                    bundles[tuple(sorted(b.items()))] = b
            nodes = [_bundle_node(b) for b in bundles.values()]
            clauses.append(nodes[0] if len(nodes) == 1 else {"type": "any", "children": nodes})
            fill.update(next(iter(bundles.values())))

    for members in clusters.values():
        k = len(members)
        all_count1 = all(c == 1 for x in members for b in alts[x] for c in b.values())
        if k >= 2 and all_count1:
            node, f = _generalize_threshold(members, alts, seed, avail, route_critical,
                                            strict_names, wins, minimal)
            if _is_uniform(node["options"], node["n"], seed, wins):
                clauses.append(node)
                fill.update(f)
            else:
                # not a real "any k of": a member double-covers (e.g. shapez Balancer does
                # both merge+split), so split into independent per-item choices.
                per_item_choices(members)
        else:
            per_item_choices(members)
    return clauses, fill


def _is_uniform(options: list, n: int, seed: dict, wins) -> bool:
    """A genuine 'any n of' threshold needs exactly n points: any (n-1) point-sources must
    LOSE. If some (n-1) subset wins, a member is worth more than one point -> not uniform."""
    if n < 2:
        return True
    option_names = set()
    for b in options:
        option_names.update(b)
    ctx = {k: v for k, v in seed.items() if k not in option_names}
    tested = 0
    for subset in itertools.combinations(options, n - 1):
        sel = dict(ctx)
        for b in subset:
            for nm, c in b.items():
                sel[nm] = max(sel.get(nm, 0), c)
        if wins(sel):
            return False
        tested += 1
        if tested >= 15:
            break
    return True


def _try_bundle_threshold(fill: dict, avail: dict, wins, rng):
    """Try to express the residual as 'any N of {bundles}' -- e.g. Dead Rising's 'rescue
    any 20 survivors', where each survivor is a bundle {mission, zone key} and keys are
    shared. Returns an ATLEAST node or None if the residual isn't this shape."""
    pool = {n: c for n, c in avail.items() if n not in fill}

    def res_wins(sel):
        return wins({**fill, **sel})

    if res_wins({}) or not res_wins(pool):
        return None

    def res_minimal(exclude=frozenset()):
        sel = {n: c for n, c in pool.items() if n not in exclude}
        if not res_wins(sel):
            return None
        for nm in list(sel):
            without = {k: v for k, v in sel.items() if k != nm}
            if res_wins(without):
                sel = without
                continue
            lo, hi = 0, sel[nm]
            while lo + 1 < hi:
                mid = (lo + hi) // 2
                trial = dict(sel); trial[nm] = mid
                if res_wins(trial):
                    hi = mid
                else:
                    lo = mid
            sel[nm] = hi
        return sel

    M = res_minimal()
    if not M:
        return None

    # Classify M's items: a UNIT contributes one count and is replaceable by a single other
    # item; an ENABLER (shared key) is not (removing it drops several units).
    units, enablers = [], []
    for x in M:
        base = {k: v for k, v in M.items() if k != x}
        if res_wins(base):
            continue
        if any(y not in M and res_wins({**base, y: 1}) for y in pool):
            units.append(x)
        else:
            enablers.append(x)
    n = len(units)
    if n < 3:
        return None  # too few to be a real threshold; let route enumeration handle it

    # Enumerate the unit-bundles from a one-unit-short context, then make each bundle whole
    # by adding the shared enablers it actually depends on.
    deficient = {k: v for k, v in M.items() if k != units[0]}
    raw = {}

    def add(b):
        raw[tuple(sorted(b.items()))] = b

    add({units[0]: M[units[0]]})
    seen = set(deficient)
    while len(raw) < MAX_GROUP_OPTIONS:
        addable = {nm: pool[nm] for nm in pool if nm not in deficient and nm not in seen}
        if not addable or not res_wins({**deficient, **addable}):
            break
        a = _minimize_addition(deficient, addable, res_wins)
        if not a:
            break
        add(a)
        seen.update(a)

    enabler_set = set(enablers)
    candidate_units = set(units)
    for b in raw.values():
        candidate_units.update(k for k in b if k not in enabler_set)

    bundles = {}
    for u in candidate_units:
        uc = M.get(u, 1)
        needed = {}
        for e in enabler_set:
            # An N-survivor set that avoids both e's "plaza" and u; drop one to get an
            # independent (N-1) base that does NOT depend on e. Then u needs e iff u counts
            # with e present but not without it.
            alt = res_minimal(frozenset([e, u]))
            if alt is None:
                needed[e] = 1
                continue
            au = [k for k in alt if k not in enabler_set]
            if not au:
                continue
            defi = {k: v for k, v in alt.items() if k != au[0]}
            if res_wins({**defi, u: uc, e: 1}) and not res_wins({**defi, u: uc}):
                needed[e] = 1
        bundle = {u: uc, **needed}
        bundles[tuple(sorted(bundle.items()))] = bundle

    if not bundles:
        return None
    return {"type": "atleast", "n": n, "options": list(bundles.values())[:MAX_GROUP_OPTIONS]}


def _build_routes(fill: dict, avail: dict, wins, rng):
    pool = {n: c for n, c in avail.items() if n not in fill}

    def res_wins(sel):
        return wins({**fill, **sel})

    if res_wins({}):
        return None

    def res_minimal(exclude):
        sel = {n: c for n, c in pool.items() if n not in exclude}
        if not res_wins(sel):
            return None
        for name in list(sel):
            without = {k: v for k, v in sel.items() if k != name}
            if res_wins(without):
                sel = without
                continue
            lo, hi = 0, sel[name]
            while lo + 1 < hi:
                mid = (lo + hi) // 2
                trial = dict(sel); trial[name] = mid
                if res_wins(trial):
                    hi = mid
                else:
                    lo = mid
            sel[name] = hi
        return sel

    base = res_minimal(frozenset())
    if not base:
        return None
    bundles = {tuple(sorted(base.items())): base}
    frontier = [base]
    while frontier and len(bundles) < MAX_ROUTES:
        cur = frontier.pop()
        for name in list(cur):
            alt = res_minimal(frozenset([name]))
            if alt is None:
                continue
            key = tuple(sorted(alt.items()))
            if key not in bundles:
                bundles[key] = alt
                frontier.append(alt)

    # CEGIS coverage: any residual-winning set no bundle covers becomes a new route.
    def covered(sel):
        return any(all(sel.get(n, 0) >= v for n, v in b.items()) for b in bundles.values())

    names = list(pool)
    for _ in range(VERIFY_SAMPLES):
        if len(bundles) >= MAX_ROUTES:
            break
        sample = {n: pool[n] for n in names if rng.random() < 0.5}
        if not res_wins(sample) or covered(sample):
            continue
        new = res_minimal(frozenset(n for n in pool if n not in sample))
        if new:
            bundles.setdefault(tuple(sorted(new.items())), new)

    nodes = [_bundle_node(b) for b in bundles.values()]
    return nodes[0] if len(nodes) == 1 else {"type": "any", "children": nodes}


# --------------------------------------------------------------------------- verification
def _collect_option_sets(node: dict, acc: list) -> None:
    if node["type"] == "atleast":
        names = set()
        for o in node["options"]:
            names.update(o)
        acc.append(list(names))
    elif node["type"] in ("all", "any"):
        for c in node["children"]:
            _collect_option_sets(c, acc)


def _merge(bundles: list) -> dict:
    m = {}
    for b in bundles:
        for k, v in b.items():
            m[k] = max(m.get(k, 0), v)
    return m


def _tree_min_sels(node: dict) -> list:
    t = node["type"]
    if t == "item":
        return [{node["name"]: node["count"]}]
    if t == "atleast":
        opts, n = node["options"], node["n"]
        return [_merge(opts[:n]), _merge(opts[-n:])]
    if t == "any":
        sels = []
        for c in node["children"]:
            sels += _tree_min_sels(c)
        return sels
    if t == "all":
        child_sels = [_tree_min_sels(c) for c in node["children"]]
        combos = []
        for pick in (0, -1):
            combos.append(_merge([cs[pick if abs(pick) < len(cs) else 0] for cs in child_sels]))
        return combos
    return [{}]


def _verify(tree: dict, avail: dict, wins, minimal, rng) -> bool:
    for sel in _tree_min_sels(tree):
        if not wins(sel):
            return False

    option_sets = []
    _collect_option_sets(tree, option_sets)
    exclusions = [frozenset()] + [frozenset(o) for o in option_sets]
    names = list(avail)
    for _ in range(VERIFY_SAMPLES):
        exclusions.append(frozenset(n for n in names if rng.random() < 0.3))
    for ex in exclusions:
        m = minimal(ex)
        if m is not None and not satisfies(tree, m):
            return False

    for sel in ({}, dict(avail)):
        if satisfies(tree, sel) != wins(sel):
            return False
    for _ in range(VERIFY_SAMPLES):
        sel = {}
        for n in names:
            r = rng.random()
            if r < 0.5:
                sel[n] = avail[n] if r < 0.4 else 1
        if satisfies(tree, sel) != wins(sel):
            return False
    return True
