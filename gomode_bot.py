"""Bot-side orchestration for the go-mode feature.

The heavy lifting (Archipelago logic) lives in `gomode_analyzer/` and runs as SUBPROCESSES
so the bot's own environment never has to import Archipelago (wrong deps, and importing an
apworld is code execution). This module just:

  * provisions a version-pinned AP env for a registered seed (`gomode_analyzer/provision.py`),
  * precomputes every slot's go-mode requirements once (`gomode_analyzer/precompute.py`),
  * keeps one analyzer running for the registered seed (`gomode_analyzer/worker.py`) and sends
    it the go-mode, checks and item-ranking queries,
  * manages the small JSON registry and state files the commands and loops read.

Large, static apworlds are NOT uploaded through Discord -- the host places them on the bot
server (e.g. via FTP) in GOMODE_APWORLDS_DIR. Only the small per-seed generation zip comes
in through `/register_seed`. The AP *source* is fetched by version, no upload needed.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import os
import sys

ANALYZER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gomode_analyzer")

# --- configuration (environment) --------------------------------------------
# A Python interpreter that already has Archipelago's runtime deps. Required for the
# precompute step (which imports AP). The bot's OWN interpreter deliberately lacks AP's
# deps, so leaving this unset is a misconfiguration -- is_configured() rejects it rather
# than letting precompute fail minutes later with an opaque ImportError.
_AP_PYTHON_SET = os.getenv("GOMODE_AP_PYTHON") is not None
AP_PYTHON = os.getenv("GOMODE_AP_PYTHON", sys.executable)
# The host's apworlds (placed on the bot server, e.g. via FTP) used to generate seeds.
APWORLDS_DIR = os.getenv("GOMODE_APWORLDS_DIR")
# Optional local AP git checkout for fast, offline provisioning (else download from GitHub).
AP_REPO = os.getenv("GOMODE_AP_REPO")
# Where provisioned AP trees + the precomputed seed cache live.
RUNTIME_DIR = os.getenv("GOMODE_RUNTIME_DIR", os.path.join(ANALYZER_DIR, "runtime"))

DATA_DIR = "data"
REGISTRY_PATH = os.path.join(DATA_DIR, "registered_seed.json")
CACHE_PATH = os.path.join(RUNTIME_DIR, "seed_cache.json")


def is_configured() -> tuple[bool, str]:
    """Whether the host has set up the go-mode environment. Returns (ok, reason)."""
    if not APWORLDS_DIR or not os.path.isdir(APWORLDS_DIR):
        return False, ("GOMODE_APWORLDS_DIR is not set to a valid apworlds directory "
                       "(place your custom_worlds on the bot server and point this at it).")
    if not _AP_PYTHON_SET:
        return False, ("GOMODE_AP_PYTHON is not set. Point it at a Python interpreter/venv "
                       "that has Archipelago's dependencies installed -- the bot's own "
                       "interpreter does not.")
    if not os.path.isfile(AP_PYTHON):
        return False, (f"GOMODE_AP_PYTHON ({AP_PYTHON}) is not a valid Python interpreter "
                       "with Archipelago's dependencies installed.")
    return True, ""


async def _run(cmd: list[str]) -> tuple[int, str, str]:
    """Run a subprocess to completion without blocking the event loop."""
    # No stdin: anything in the analyzer that asks a question fails at once instead of hanging.
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def _quiet_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


async def register_seed(seed_zip: str, *, progress=None) -> dict:
    """Provision a pinned AP env for `seed_zip` and precompute every slot's requirements.

    Writes the registry + per-slot cache and returns a summary dict. `progress` is an
    optional async callable(str) for status updates. Raises RuntimeError on failure.
    """
    async def say(msg: str) -> None:
        if progress:
            await progress(msg)

    ok, why = is_configured()
    if not ok:
        raise RuntimeError(why)
    if not os.path.isfile(seed_zip):
        raise RuntimeError(f"Seed file not found: {seed_zip}")

    os.makedirs(RUNTIME_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)

    # 1. Provision the matching AP source + the host's apworlds (plain Python: no AP import).
    await say("Provisioning the matching Archipelago version (this can take a minute)...")
    cmd = [sys.executable, os.path.join(ANALYZER_DIR, "provision.py"),
           "--seed-zip", seed_zip, "--runtime-dir", RUNTIME_DIR, "--apworlds", APWORLDS_DIR]
    if AP_REPO:
        cmd += ["--ap-repo", AP_REPO]
    rc, out, err = await _run(cmd)
    if rc != 0:
        raise RuntimeError(f"Provisioning failed:\n{(err or out)[-1500:]}")
    manifest = json.loads(out)
    ap_path, version = manifest["ap_path"], manifest["version"]
    await say(f"Provisioned Archipelago {version} with {len(manifest['apworlds_installed'])} "
              "apworlds. Analyzing every slot's go-mode requirements -- this is a one-time "
              "step and may take a few minutes...")

    # 2. Precompute the requirement tree (or conservative fallback) for every slot. Needs
    #    the AP env, so use the configured AP interpreter. Write to a TEMP cache and only
    #    swap it into place once the run is known-good, so a failed re-registration can't
    #    truncate the live cache and silently break the previously-registered seed.
    tmp_cache = CACHE_PATH + ".tmp"
    cmd = [AP_PYTHON, os.path.join(ANALYZER_DIR, "precompute.py"),
           "--ap-path", ap_path, "--seed-zip", seed_zip, "--out", tmp_cache]
    rc, out, err = await _run(cmd)
    if rc != 0:
        _quiet_remove(tmp_cache)
        raise RuntimeError(f"Precompute failed:\n{(err or out)[-1500:]}")
    try:
        summary = json.loads(out.strip().splitlines()[-1])  # last stdout line is the summary
    except (ValueError, IndexError) as exc:
        _quiet_remove(tmp_cache)
        raise RuntimeError(f"Precompute produced no valid summary: {exc}\n{(out or err)[-800:]}")
    os.replace(tmp_cache, CACHE_PATH)  # atomic adopt of the new, validated cache

    # 3. Record the active seed (atomically too). Only one seed is registered at a time.
    registry = {
        "seed": summary["seed"],
        "version": summary["version"],
        "slot_count": summary["slots"],
        "verified": summary["verified"],
        "exact": summary.get("exact", 0),
        "unsupported": summary["unsupported"],
        "unsupported_slots": summary.get("unsupported_slots", []),
        "cache_path": os.path.abspath(CACHE_PATH),
        "ap_path": ap_path,
        "seed_zip": os.path.abspath(seed_zip),
        "registered_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    tmp_reg = REGISTRY_PATH + ".tmp"
    with open(tmp_reg, "w", encoding="utf-8") as fh:
        json.dump(registry, fh, indent=2)
    os.replace(tmp_reg, REGISTRY_PATH)
    return registry


def load_registry() -> dict | None:
    try:
        with open(REGISTRY_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def load_cache() -> dict | None:
    """The precomputed per-slot requirements for the active seed, or None if unregistered."""
    reg = load_registry()
    path = reg["cache_path"] if reg else CACHE_PATH
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def slot_for_name(cache: dict, slot_name: str) -> dict | None:
    """Find a cached slot record by its (case-insensitive) slot name."""
    if not cache:
        return None
    for rec in cache.get("slots", {}).values():
        if rec.get("name", "").lower() == slot_name.lower():
            return rec
    return None


# --- player-facing: go-mode status + on-demand analysis ---------------------

def _load_items_received() -> dict:
    try:
        with open(os.path.join(DATA_DIR, "items_received.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def goal_completed(items_received: dict, slot_name: str) -> bool:
    """True once the tracker shows the slot has reached its goal ("Game Status" is "Goal
    Completed" in items_received.json, whether read from the API path or the HTML pages)."""
    for slot_entry in (items_received or {}).values():
        if slot_name in slot_entry:
            return str(slot_entry[slot_name].get("Game Status", "")).strip().lower() in (
                "goal completed", "completed")
    return False


def inventory_for_slot(items_received: dict, slot_name: str) -> dict:
    """Aggregate {item_name: total_count} for a slot from items_received.json, whose shape is
    {slot_num: {slot_name: {"Items": {key: {item_name, amount}}}}}."""
    for slot_entry in (items_received or {}).values():
        if slot_name in slot_entry:
            inv: dict = {}
            for info in slot_entry[slot_name].get("Items", {}).values():
                name = info.get("item_name")
                if not name:
                    continue
                try:
                    amt = int(info.get("amount", 0))
                except (TypeError, ValueError):
                    amt = 0
                inv[name] = inv.get(name, 0) + amt
            return inv
    return {}


def current_inventory(slot_name: str) -> dict:
    """The slot's current {item_name: count} from items_received.json on disk."""
    return inventory_for_slot(_load_items_received(), slot_name)


def _tracker_game_for_slot(items_received: dict, slot_name: str):
    """The game the live tracker reports for a slot name (used to catch a registered seed that
    doesn't match what's being tracked)."""
    for slot_entry in (items_received or {}).values():
        if slot_name in slot_entry:
            return slot_entry[slot_name].get("Game Name")
    return None


# --- persistent analyzer -----------------------------------------------------
# One long-running analyzer (gomode_analyzer/worker.py) holds every slot's rebuilt world, so a
# go-mode or checks query takes milliseconds instead of a new subprocess rebuilding worlds.

WORKER_LOG = os.path.join(RUNTIME_DIR, "worker.log")
WORKER_READY_TIMEOUT = 900     # building every slot of a large seed
WORKER_REQUEST_TIMEOUT = 600   # a full requirement analysis can take a while


class WorkerError(RuntimeError):
    """The analyzer answered, but the request itself failed."""


class _Worker:
    def __init__(self):
        self.proc = None
        self.key = None      # (seed, ap_path, seed_zip) the running worker was started for
        self.log = None
        self.lock = asyncio.Lock()
        self.next_id = 0

    async def _stop(self):
        if self.proc and self.proc.returncode is None:
            self.proc.kill()
            try:
                await self.proc.wait()
            except Exception:  # noqa: BLE001
                pass
        self.proc, self.key = None, None
        if self.log:
            self.log.close()
            self.log = None

    async def _start(self, reg: dict):
        os.makedirs(RUNTIME_DIR, exist_ok=True)
        # stderr to a file: Archipelago's chatter would otherwise fill the pipe and stall it.
        self.log = open(WORKER_LOG, "a", encoding="utf-8")
        self.proc = await asyncio.create_subprocess_exec(
            AP_PYTHON, os.path.join(ANALYZER_DIR, "worker.py"),
            "--ap-path", reg["ap_path"], "--seed-zip", reg["seed_zip"],
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=self.log,
            limit=64 * 1024 * 1024)
        line = await asyncio.wait_for(self.proc.stdout.readline(), WORKER_READY_TIMEOUT)
        ready = json.loads(line) if line else {}
        if not ready.get("ready"):
            await self._stop()
            raise RuntimeError(f"analyzer worker didn't start; see {WORKER_LOG}")
        self.key = (reg.get("seed"), reg.get("ap_path"), reg.get("seed_zip"))
        print(f"[worker] ready: seed {ready.get('seed')}, {ready.get('built')}/{ready.get('slots')} "
              f"slots built in {ready.get('seconds')}s")

    async def request(self, op: str, **payload):
        reg = load_registry()
        if not reg:
            raise RuntimeError("no seed registered")
        key = (reg.get("seed"), reg.get("ap_path"), reg.get("seed_zip"))
        async with self.lock:
            for attempt in (1, 2):
                try:
                    # (Re)start for a new registration, or after the worker died.
                    if self.proc is None or self.proc.returncode is not None or self.key != key:
                        await self._stop()
                        await self._start(reg)
                    self.next_id += 1
                    message = {"id": self.next_id, "op": op, **payload}
                    self.proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
                    await self.proc.stdin.drain()
                    line = await asyncio.wait_for(self.proc.stdout.readline(), WORKER_REQUEST_TIMEOUT)
                    if not line:
                        raise RuntimeError("analyzer worker exited")
                    reply = json.loads(line)
                    if reply.get("id") != message["id"]:
                        raise RuntimeError("analyzer worker replied out of order")
                except (RuntimeError, OSError, ValueError, asyncio.TimeoutError) as exc:
                    # Dead, hung or confused: start a fresh one and try once more.
                    await self._stop()
                    if attempt == 2:
                        raise RuntimeError(f"analyzer worker failed: {exc}") from exc
                    continue
                if not reply.get("ok"):
                    raise WorkerError(reply.get("error") or "request failed")
                return reply["result"]


_worker = _Worker()


async def warm_worker() -> None:
    """Start the analyzer for the registered seed ahead of the first query (bot startup)."""
    if load_registry() is None:
        return
    try:
        await _worker.request("ping")
    except Exception as e:  # noqa: BLE001 -- a later query will retry
        print(f"[worker] couldn't start: {e}")


async def go_mode_status(slot_names, *, items_received: dict | None = None) -> dict:
    """For each assigned slot name, return {status, in_go_mode, game, reason?}.

    Every supported slot is checked the way Universal Tracker does it: against its rebuilt
    world's real logic, in one request to the analyzer. Unsupported/unregistered slots are
    reported as such (in_go_mode = None).
    """
    cache, reg = load_cache(), load_registry()
    if not cache or not reg:
        return {name: {"status": "unregistered", "in_go_mode": None} for name in slot_names}
    if items_received is None:
        items_received = _load_items_received()

    result: dict = {}
    batch: dict = {}
    for name in slot_names:
        rec = slot_for_name(cache, name)
        if rec is None:
            result[name] = {"status": "unregistered", "in_go_mode": None}
            continue
        game = rec.get("game")
        if rec.get("status") != "ok":
            result[name] = {"status": rec.get("status", "error"), "in_go_mode": None,
                            "reason": rec.get("reason", ""), "game": game}
            continue
        # Defensive: if the live tracker reports a DIFFERENT game for this slot name than the
        # registered seed, they're different multiworlds -- don't evaluate go-mode on foreign
        # inventory (a same-name/different-game collision could otherwise misfire).
        tracker_game = _tracker_game_for_slot(items_received, name)
        if tracker_game and game and tracker_game.lower() != str(game).lower():
            result[name] = {"status": "tracker_mismatch", "in_go_mode": None, "game": game,
                            "tracker_game": tracker_game}
            continue

        batch[name] = inventory_for_slot(items_received, name)
        result[name] = {"status": "ok", "in_go_mode": None, "game": game}

    if batch:
        try:
            oracle = await _worker.request("go_mode", slots=batch)
        except Exception as e:  # noqa: BLE001 -- leave in_go_mode None; the next cycle retries
            print(f"[go-mode] analyzer request failed: {e}")
            oracle = {}
        for name, info in oracle.items():
            if name in result:
                result[name]["in_go_mode"] = info.get("in_go_mode")
                if info.get("status") and info["status"] != "ok":
                    result[name]["status"] = info["status"]
                    result[name]["reason"] = info.get("reason", "")
    return result


async def analyze_slot_live(slot_name: str, inventory: dict) -> dict | None:
    """Full on-demand analysis of one slot for the player's current inventory. Returns the
    analysis dict (incl. `requirements_text`), or None if no seed is registered."""
    if load_registry() is None:
        return None
    try:
        return await _worker.request("analyze", slot=slot_name, inventory=inventory)
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "reason": str(e)[-500:]}


def checked_for_slot(items_received: dict, slot_name: str) -> list:
    """The slot's checked location names, as the tracker page lists them."""
    for slot_entry in (items_received or {}).values():
        if slot_name in slot_entry:
            return list(slot_entry[slot_name].get("Checked Locations", []) or [])
    return []


async def checks_in_logic(slot_names, *, items_received: dict | None = None,
                          list_names: bool = False) -> dict:
    """For each slot name, {status, game, reason?} plus, when ok, {in_logic, in_logic_done,
    total, done}: its checks reachable with what the player holds (as UT counts them) and how
    many of those are done, and all its checks and how many are done. With list_names, also
    "to_do": the names of the in-logic checks not done yet. One analyzer request for every
    supported slot."""
    cache, reg = load_cache(), load_registry()
    if not cache or not reg:
        return {name: {"status": "unregistered"} for name in slot_names}
    if items_received is None:
        items_received = _load_items_received()

    result: dict = {}
    batch: dict = {}
    for name in slot_names:
        rec = slot_for_name(cache, name)
        if rec is None:
            result[name] = {"status": "unregistered"}
            continue
        game = rec.get("game")
        if rec.get("status") != "ok":
            result[name] = {"status": rec.get("status", "error"), "game": game,
                            "reason": rec.get("reason", "")}
            continue
        if not any(name in entry for entry in items_received.values()):
            # No tracker data for the slot (e.g. the file was unreadable): counting it as
            # holding nothing and having checked nothing would give wrong numbers.
            result[name] = {"status": "error", "game": game, "reason": "no tracker data"}
            continue
        tracker_game = _tracker_game_for_slot(items_received, name)
        if tracker_game and game and tracker_game.lower() != str(game).lower():
            result[name] = {"status": "tracker_mismatch", "game": game}
            continue
        batch[name] = {"inventory": inventory_for_slot(items_received, name),
                       "checked": checked_for_slot(items_received, name), "list_names": list_names}
        result[name] = {"status": "error", "game": game, "reason": "no result"}

    if batch:
        try:
            checks = await _worker.request("checks", slots=batch)
        except Exception as e:  # noqa: BLE001 -- slots stay "couldn't check right now"
            print(f"[checks] analyzer request failed: {e}")
            checks = {}
        for name, info in checks.items():
            if name in result:
                result[name] = {**info, "game": result[name].get("game")}
    return result


async def most_useful_items(slot_name: str, *, items_received: dict | None = None,
                            top: int = 5) -> dict:
    """{status, game, items?: [{"name", "opens"}], reason?}: the missing items that would each
    open the most new checks for the slot right now."""
    cache = load_cache()
    if not cache or load_registry() is None:
        return {"status": "unregistered"}
    rec = slot_for_name(cache, slot_name)
    if rec is None:
        return {"status": "unregistered"}
    game = rec.get("game")
    if rec.get("status") != "ok":
        return {"status": rec.get("status", "error"), "game": game, "reason": rec.get("reason", "")}
    if items_received is None:
        items_received = _load_items_received()
    tracker_game = _tracker_game_for_slot(items_received, slot_name)
    if tracker_game and game and tracker_game.lower() != str(game).lower():
        return {"status": "tracker_mismatch", "game": game}
    try:
        reply = await _worker.request("useful_items", slot=rec.get("name", slot_name), top=top,
                                      inventory=inventory_for_slot(items_received, slot_name),
                                      checked=checked_for_slot(items_received, slot_name))
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "game": game, "reason": str(e)[-300:]}
    return {**reply, "game": game}


# --- filler filtering for the item feed -------------------------------------
# The tracker page doesn't say what kind of item something is, but the registered seed does:
# every placed item carries flags (0b1 progression, 0b10 useful, 0b100 trap, 0 filler).
# The analyzer reads them from the seed; the bot keeps them for the life of the registration.

_item_flags_memo: dict = {}   # {seed: {slot_name: {"game", "items": {name: flags}}} or None}


async def load_item_flags() -> dict | None:
    """Every slot's {item name: flags} for the registered seed, or None if unavailable."""
    reg = load_registry()
    if not reg:
        return None
    seed = reg.get("seed")
    if seed in _item_flags_memo:
        return _item_flags_memo[seed]
    try:
        result = await _worker.request("item_flags")
    except Exception as e:  # noqa: BLE001
        result = None
        print(f"[item-flags] could not read item classifications: {e}")
    if not result or result.get("seed") != seed:
        # Remembered for this run so a broken setup doesn't retry every minute; registering a
        # seed (a new seed key) tries again.
        _item_flags_memo[seed] = None
        return None
    _item_flags_memo[seed] = result["item_flags"]
    return _item_flags_memo[seed]


def drop_filler(diff: dict, item_flags: dict, items_received: dict) -> dict:
    """The tracker diff ({slot: {slot_name: {"New Items": {name: count}, ...}}}) without filler.

    A name is filler only if every copy of it in the seed is (Celeste 64's extra strawberries
    are filler but the name isn't). Unknown names, and slots whose tracker game doesn't match
    the registered seed, are left alone. A slot left with nothing to report is dropped."""
    kept: dict = {}
    for slot, slot_entry in diff.items():
        for slot_name, details in slot_entry.items():
            rec = item_flags.get(slot_name)
            tracker_game = _tracker_game_for_slot(items_received, slot_name)
            if rec and not (tracker_game and tracker_game.lower() != str(rec.get("game", "")).lower()):
                flags = rec.get("items", {})
                new_items = {name: count for name, count in details.get("New Items", {}).items()
                             if flags.get(name) != 0}
                details = {**details, "New Items": new_items} if new_items else \
                    {k: v for k, v in details.items() if k != "New Items"}
            if details.get("New Items") or "Goal Completed" in details:
                kept.setdefault(slot, {})[slot_name] = details
    return kept


# --- go-mode notification dedup state (per registered seed) ------------------

NOTIFIED_PATH = os.path.join(DATA_DIR, "go_mode_notified.json")


def load_notified(current_seed: str) -> set:
    """Slot names already DM'd for the CURRENT seed. Auto-resets when the seed changes."""
    try:
        with open(NOTIFIED_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return set()
    if data.get("seed") != current_seed:
        return set()
    return set(data.get("notified", []))


def save_notified(current_seed: str, notified) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = NOTIFIED_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"seed": current_seed, "notified": sorted(notified)}, fh, indent=2)
    os.replace(tmp, NOTIFIED_PATH)


# --- unstuck alerts (opt-in, per player and slot) ----------------------------

UNSTUCK_SUBS_PATH = os.path.join(DATA_DIR, "unstuck_alerts.json")
UNSTUCK_STATE_PATH = os.path.join(DATA_DIR, "unstuck_state.json")


def _write_json(path: str, data) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def load_unstuck_subscriptions() -> dict:
    """{discord user id: [slot names]} of players who signed up for unstuck alerts."""
    try:
        with open(UNSTUCK_SUBS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_unstuck_subscriptions(subs: dict) -> None:
    _write_json(UNSTUCK_SUBS_PATH, {user: sorted(set(names)) for user, names in subs.items() if names})


def load_unstuck_state(current_seed: str) -> dict:
    """{slot name: {"to_do": int, "inventory": {...}}} last seen for the CURRENT seed."""
    try:
        with open(UNSTUCK_STATE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data.get("slots", {}) if data.get("seed") == current_seed else {}


def save_unstuck_state(current_seed: str, slots: dict) -> None:
    _write_json(UNSTUCK_STATE_PATH, {"seed": current_seed, "slots": slots})
