"""Bot-side orchestration for the go-mode feature.

The heavy lifting (Archipelago logic) lives in `gomode_analyzer/` and runs as SUBPROCESSES
so the bot's own environment never has to import Archipelago (wrong deps, and importing an
apworld is code execution). This module just:

  * provisions a version-pinned AP env for a registered seed (`gomode_analyzer/provision.py`),
  * precomputes every slot's go-mode requirements once (`gomode_analyzer/precompute.py`),
  * manages the small JSON registry the player-facing commands + notification loop read.

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
import tempfile

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
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
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
        "unsupported": summary["unsupported"],
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
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


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


_req_mod = None


def _satisfies(tree: dict, held: dict) -> bool:
    """Evaluate a verified requirement tree against held items -- pure Python, no AP needed."""
    global _req_mod
    if _req_mod is None:
        if ANALYZER_DIR not in sys.path:
            sys.path.insert(0, ANALYZER_DIR)
        import requirements as _r  # itertools + random only; safe to import in the bot env
        _req_mod = _r
    return _req_mod.satisfies(tree, held)


async def _oracle_go_mode(ap_path: str, seed_zip: str, slot_inv_map: dict) -> dict:
    """One fast-path subprocess returning {slot: {status, in_go_mode}} for several slots at once
    (used for fallback slots, which have no verified tree to evaluate in-process)."""
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".json", dir=RUNTIME_DIR, prefix="gomode_batch_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(slot_inv_map, fh)
        cmd = [AP_PYTHON, os.path.join(ANALYZER_DIR, "cli.py"),
               "--ap-path", ap_path, "--seed-zip", seed_zip, "--go-mode-batch", "@" + tmp]
        rc, out, err = await _run(cmd)
        if rc != 0:
            return {}
        try:
            return json.loads(out).get("go_mode", {})
        except ValueError:
            return {}
    finally:
        _quiet_remove(tmp)


async def go_mode_status(slot_names, *, items_received: dict | None = None) -> dict:
    """For each assigned slot name, return {status, in_go_mode, kind, game, reason?}.

    Verified slots are evaluated instantly in pure Python (satisfies on the cached tree);
    fallback slots share ONE fast oracle subprocess; unsupported/unregistered slots are
    reported as such (in_go_mode = None).
    """
    cache, reg = load_cache(), load_registry()
    if not cache or not reg:
        return {name: {"status": "unregistered", "in_go_mode": None} for name in slot_names}
    if items_received is None:
        items_received = _load_items_received()

    result: dict = {}
    fallback_batch: dict = {}
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

        inv = inventory_for_slot(items_received, name)
        req = rec.get("requirements", {})
        if req.get("verified") and req.get("tree"):
            try:
                igm = _satisfies(req["tree"], inv)
            except Exception:  # a corrupt/hand-edited cached tree must not blank the whole call
                igm = None
            result[name] = {"status": "ok", "kind": "verified", "game": game, "in_go_mode": igm}
        else:
            fallback_batch[name] = inv
            result[name] = {"status": "ok", "kind": "fallback", "in_go_mode": None, "game": game}

    if fallback_batch:
        oracle = await _oracle_go_mode(reg["ap_path"], reg["seed_zip"], fallback_batch)
        for name, info in oracle.items():
            if name in result:
                result[name]["in_go_mode"] = info.get("in_go_mode")
                if info.get("status") and info["status"] != "ok":
                    result[name]["status"] = info["status"]
                    result[name]["reason"] = info.get("reason", "")
    return result


async def analyze_slot_live(slot_name: str, inventory: dict) -> dict | None:
    """Full on-demand analysis of one slot for the player's current inventory (a subprocess in
    the AP env). Returns cli.py's result dict (incl. `requirements_text`), or None if no seed
    is registered."""
    reg = load_registry()
    if not reg:
        return None
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".json", dir=RUNTIME_DIR, prefix="gomode_inv_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(inventory, fh)
        cmd = [AP_PYTHON, os.path.join(ANALYZER_DIR, "cli.py"),
               "--ap-path", reg["ap_path"], "--seed-zip", reg["seed_zip"],
               "--slot", slot_name, "--inventory", "@" + tmp]
        rc, out, err = await _run(cmd)
        try:
            return json.loads(out)
        except ValueError:
            return {"status": "error", "reason": (err or out)[-500:]}
    finally:
        _quiet_remove(tmp)


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
