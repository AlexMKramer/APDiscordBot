# Go-mode analyzer

Standalone, **version-pinned** Archipelago logic engine that answers, for one slot and a
current inventory:

- **Are they in "go mode"?** — can they still logically reach their goal from here.
- **If not, which progression items do they still need?**

It is intentionally **decoupled from the Discord bot**: the bot's environment can't run
Archipelago (wrong dependencies, and importing apworlds is code execution), and the
analyzer must match the *exact AP version that generated the seed*. The bot invokes this
as a subprocess (one fork per request) and reads JSON back.

## Spoiler safety

The analyzer only ever reports **item names** (and counts). It never reads or emits
placements, locations, or which world holds an item — reachability is computed purely
over the abstract logic graph + item pool, which never references placements (no fill is
run). So it cannot leak routing spoilers like "complete location Y to get player B's item."

## Pieces

| File | Runs in | Purpose |
|------|---------|---------|
| `provision.py` | plain Python (the bot can call it) | Detect the seed's AP version, materialize a matching AP source tree, install the host's apworlds, write a manifest. |
| `seed_data.py` | AP env | Decode a generated `AP_<seed>.zip` → per-slot `{game, slot_data, resolved options, precollected, spoiler settings, datapackage checksum, real location IDs, real progression items}` + version + the generation seed (from the spoiler header). |
| `spoiler_options.py` | mixed | Parse the spoiler's per-player blocks (pure text) and reverse the options back into `{attr: value}` (AP env). Recovers settings for worlds that put nothing in slot_data. |
| `engine.py` | AP env | Rebuild a slot's world (no fill) the way Universal Tracker does, plus a replay of the slot's real RNG; compute go-mode + the minimal still-needed item set, with guardrails. |
| `cli.py` | AP env | JSON entrypoint the bot calls for an on-demand single-slot analysis. Emits clean JSON only. |
| `precompute.py` | AP env | Analyze **every** slot once (empty inventory) and write `runtime/seed_cache.json` — the per-slot requirement trees the bot reads. Run once per registered seed. |
| `../gomode_bot.py` | bot env | Orchestrates `provision.py` + `precompute.py` as subprocesses for `/register_seed`, and exposes the cached registry (`load_registry`/`load_cache`) to the bot. Imports neither Discord nor AP. |

`runtime/` (git-ignored) holds provisioned AP trees (`ap-<version>/`) and `manifest.json`.

## Usage

**1. Provision** a pinned AP environment for a registered seed (once per seed/version).
From a local AP git checkout (fast, no network):

```
python gomode_analyzer/provision.py \
  --seed-zip /path/AP_<seed>.zip \
  --runtime-dir gomode_analyzer/runtime \
  --apworlds /path/to/custom_worlds \
  --ap-repo /path/to/Archipelago        # omit to download the release zip from GitHub
```

This writes `runtime/ap-<version>/` and `runtime/manifest.json`. As in the launcher, an
apworld replaces the built-in world of the same name: the tree's `worlds/<name>` folder is
moved to `shadowed_worlds/`, since in a source tree it would otherwise load first and win. An
apworld with only compiled bytecode (a copy of a launcher's built-in world) can't load under
another Python version, so it's skipped and listed under `apworlds_skipped_no_source`. An
apworld zipped with Windows backslashes in its entry names is rewritten with forward
slashes, since Linux can't import it otherwise. The AP env still needs an
interpreter with AP's runtime deps (PyYAML, schema, jellyfish, …); point the analyzer at
one. A dedicated `--build-venv` step is a future addition; for now reuse an AP venv.

**2. Analyze** a slot for a player's current inventory:

```
<ap-venv-python> gomode_analyzer/cli.py \
  --ap-path gomode_analyzer/runtime/ap-<version> \
  --seed-zip /path/AP_<seed>.zip \
  --slot "Alex_Crab" \
  --inventory '{"Katana": 1, "Hammer": 1}'
```

Output:

```json
{
  "slot": 1, "name": "Alex_Crab", "game": "Crab Champions",
  "status": "ok", "in_go_mode": false,
  "items_needed": [{"name": "Blade Launcher", "count": 1}, ...],
  "progression_pool": 95, "unknown_inventory": []
}
```

`--survey` analyzes every slot (empty inventory) — useful for coverage checks.

## Bot integration (`/register_seed`)

The owner-only `/register_seed` command (in `main.py`) drives the whole thing. It takes the
small generation zip as a Discord attachment (or a `server_path` to one already on the bot
server) and runs provisioning + precompute via `gomode_bot.py`, writing
`data/registered_seed.json` + `runtime/seed_cache.json`. **Apworlds are never uploaded
through Discord** — they're large and static, so the host places `custom_worlds/` on the bot
server (e.g. via FTP) and points `GOMODE_APWORLDS_DIR` at it. The AP *source* is fetched by
version automatically.

Config (environment):

| Var | Purpose |
|-----|---------|
| `GOMODE_AP_PYTHON` | Python interpreter with Archipelago's deps (runs the precompute). |
| `GOMODE_APWORLDS_DIR` | The host's `custom_worlds/` on the bot server (FTP'd there). |
| `GOMODE_AP_REPO` | *(optional)* local AP git checkout for offline provisioning; else GitHub. |
| `GOMODE_RUNTIME_DIR` | Where provisioned trees + the seed cache live (default `gomode_analyzer/runtime`). |
| `GOMODE_OWNER_ID` / `OWNER_ID` | Discord user allowed to register; else the guild owner. |

### Player-facing surfaces (after registration)

- **`/items_to_go_mode`** — no argument gives an at-a-glance overview of all the caller's
  slots (one batched go-mode check); a slot argument (or the caller's only slot) runs a full
  on-demand analysis (`cli.py --slot --inventory`) and renders `requirements_text`.
- **Go-mode notification** — a 120s loop DMs the assigned player the moment a slot reaches go
  mode. Dedup is per `(author, slot)` and persisted per seed (`data/go_mode_notified.json`),
  marked only after the DM actually sends. Slots are throttled on an inventory signature so an
  unchanged world isn't rebuilt every cycle.

Both go-mode surfaces check against the rebuilt world's real logic, as Universal Tracker does.
The precomputed requirement trees only feed `/items_to_go_mode`'s breakdown and the
registration summary.

`cli.py --go-mode-batch @file` is that boolean check: input `{slot: inventory}`, output
`{go_mode: {slot: {status, in_go_mode}}}`, via `analyze_slot(fast=True)` (same build +
guardrails, skips the requirement decomposition).

**Operational requirement:** register the *same* seed the tracker is tracking. The bot joins
live inventory to the registered slot purely by slot name; a cheap game-name cross-check
(`items_received` "Game Name" vs the cached game) guards the obvious mismatch, but a stale/foreign
tracker with matching names+games could otherwise be evaluated as this seed's inventory.

## How a slot is rebuilt

Many worlds randomize logic-relevant structure while generating: which weapons are
progression, chapter or region order, entrances, goal counts. Rebuilding with an arbitrary
seed gives a different world from the one being played, which was the main source of wrong
go-mode answers. `engine.prepare_slot` tries these, most faithful first:

1. **`seed`** — replay the slot's real RNG. Real generation seeds the multiworld RNG with the
   generation seed (in the spoiler header), then each world, in player order, takes
   `Random(multiworld.random.getrandbits(64))`. Giving the rebuilt world the same draw makes it
   run exactly the code the real generation ran, including worlds with no UT support.
2. **`seed+ut`** — the same, plus Universal Tracker's regeneration: `generation_is_fake`, and
   the slot's `slot_data` handed back through `re_gen_passthrough` (via `interpret_slot_data`
   where the world has one). This covers settings the spoiler doesn't carry.
3. **`ut`** — UT's regeneration alone, for seeds without a spoiler (race seeds).

Each rebuild is checked against what the multidata recorded for the slot: its real location
IDs and the progression items it owns. The first exact match is used; if none match, the
closest is. `regen` and `mismatch` in the result (and cache) say which was used and how far
it was from the seed (0 = the same world).

**Go mode** is checked the way UT checks it (collect what the player holds, sweep their event
locations, check the goal), with two deliberate differences, both toward what the player can
actually do in game:

- Every copy of an item you hold counts. Some worlds flag only the copies the goal needs as
  progression (Celeste 64 flags 16 of its strawberries), so UT, which uses the server's
  per-copy flags, can lag behind a player who already holds enough.
- Items the world locks onto its own locations (Refunct's `Goal Location` on the goal
  platform) count once reachable, so "go mode" means "you have everything, go finish" rather
  than "you already touched the goal".

**Start inventory:** the tracker's inventory already includes it (the web tracker adds the
multidata's precollected items), so the rebuild strips every real start item and lets the
inventory supply it, as UT does. The multidata's start items still count toward the universe
of items the player can hold, as do items a world places itself in `pre_fill`.

**apworld version:** if the installed apworld's datapackage checksum differs from the one the
seed was generated with, the slot is `unsupported` and the registration summary names it.

## `requirements`: the full decomposition (and how it's trustworthy)

A goal's real requirement is a logical expression, not a flat list — games have
**interchangeable picks** ("any 6 worlds") and **alternate win routes** (Kingdom Hearts:
End-of-the-World + puppies *or* the Destiny Islands homecoming route). `requirements.py`
discovers this as a **tree** using only black-box `can_beat_game` queries (so it works on
apworlds we've never seen — it never reads apworld code):

```
item(name, count)            need >= count of an item
atleast(n, [options])        need >= n distinct items from a fungible group
all([...])                   AND
any([...])                   one of several routes (OR)
```

Crucially, the tree is **self-verified against the oracle**: every route's minimal set must
actually win, and oracle-minimal winning sets (including ones that avoid each choice group)
must satisfy the tree, plus random two-directional samples. If it doesn't match, the tree is
**not trusted**.

- **Verified** (`requirements.verified == true`): `requirements.tree` is a faithful full
  representation. `requirements.render_requirements(req)` / the CLI's `requirements_text`
  renders it. Example (Refunct): a list of required grass items + "ONE of these routes" with
  three ending routes.
- **Not verified**: the structure was too rich to prove (e.g. a count-2 cross-group
  alternative, or interchangeable items the apworld doesn't put in a shared group). The
  engine **falls back** to a conservative, never-overclaiming split: `required` (items
  needed on every path), `example_path` (one concrete completion), `has_alternatives`. It
  also keeps the best-effort `unverified_tree` for inspection.

`items_needed` remains available as one concrete minimal set (a single path).

Known cases that currently fall back (faithful tree not yet provable): count-N cross-group
alternatives (KH1's "MP Rage or Second Chance or 2× Progressive Aero"), thresholds whose
members aren't in a shared item group (Crab Champions weapons), and goals with many routes
(capped at `MAX_ROUTES`).

## Where options come from

To build correct logic, each slot's *resolved* options are recovered in layers (most to
least authoritative), and the build falls back if a richer set fails:

1. **slot_data options** — exact/typed, but only some worlds populate them.
2. **spoiler settings** — the spoiler lists every player's resolved options as text;
   `spoiler_options.py` reverses scalar options (Toggle/Choice/Range/FreeText) with their own
   converters, and dict/list/set options by parsing the joined text, kept only if it renders
   back to exactly what the spoiler wrote. Choices are matched against each value's display
   form ("Completely Random"), which `from_text` can't read back. This is how empty-`slot_data` games (Kingdom
   Hearts, etc.) become faithful instead of guessing defaults. (Verified: KH with its real
   settings correctly requires "Puppy" because the player set "Final Rest Door Key:
   Puppies"; defaults got this wrong.) Dict options matter for the RNG replay too: Refunct
   draws its minigames from a weighted dict, so the default weights pick different ones.
3. **defaults** — for anything still unresolved.

If a spoiler-recovered value breaks the world build, the engine retries with slot_data-only,
then defaults, so it never regresses below "the world at least builds". `options_source` in
the result records what was actually used (e.g. `slot_data(33)+spoiler(3)`).

## Result `status`

- **`ok`** — analysis is trustworthy. Read `in_go_mode` and `items_needed`.
- **`unsupported`** — the engine deliberately declined rather than risk a wrong answer:
  - world not loaded (apworld missing / incompatible),
  - no gating goal in logic,
  - goal unreachable even with every progression item (usually **entrance randomization**
    whose real connections aren't reconstructed — needs the seed's ER data).
- **`error`** — the world failed to build (assertion/exception); see `reason`.

The bot should surface `ok` results and, for the rest, tell the user the game isn't
supported yet — never present an `unsupported`/`error` slot as a real answer.

## Validation

Checked against ground truth: a 20-slot multiworld generated locally from template yamls
(14 games that support UT regeneration, plus Crab Champions, Kingdom Hearts, Kingdom Hearts 2,
Star Fox 64, Celeste 64 and APFlowFree), regenerated in-process so the real filled world is
available. For each slot and each sphere, the tracker's view of the inventory (start items +
everything received through that sphere) went through the engine, and the answer was
compared with the goal check on the real world.

- Previous engine (seed-0 rebuild): 6 of 20 slots wrong or unsupported. Jigsaw announced go
  mode 13 spheres early; Rimworld, APFlowFree, Crab Champions and Celeste 64 never flipped at
  all; Ship of Harkinian was unsupported.
- Current engine: 20 of 20 exact at every sphere (no early or missed go mode), 18 of them
  rebuilt with 0 mismatch against the seed's record.

## Known limitations / next

- **Entrance randomization with no recoverable connections**: when the real entrance graph
  isn't reconstructable, guardrail 2 reports `unsupported` rather than guessing.
- **Plando options** aren't reversed from the spoiler.
- **Worlds that read other players' settings or use the shared multiworld RNG** during
  generation can't be replayed exactly from one slot; the mismatch check shows it.
- **Provisioning a deps venv** is currently a manual step; auto-build is a later addition.
- **Performance**: the still-needed minimization is O(progression-pool) reachability
  sweeps; fine on demand, and the go-mode *notification* check only needs a single
  `can_beat_game` call.
- **apworld compatibility**: a world whose apworld won't import in the source env (e.g.
  Super Smash Bros. 64) is reported `unsupported` — a per-apworld issue, separate from the
  engine.
