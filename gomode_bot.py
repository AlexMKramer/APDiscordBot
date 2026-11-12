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
import runs
import sys

import log_setup
from gomode_analyzer.precompute import slot_limits  # plain Python: no Archipelago import

log_goal = log_setup.get('goal')
log_checks = log_setup.get('checks')
log_error = log_setup.get('error')

ANALYZER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gomode_analyzer")

# --- configuration (environment) --------------------------------------------
# A Python interpreter that already has Archipelago's runtime deps. Required for the
# precompute step (which imports AP). The bot's OWN interpreter deliberately lacks AP's
# deps, so leaving this unset is a misconfiguration -- is_configured() rejects it rather
# than letting precompute fail minutes later with an opaque ImportError.
_AP_PYTHON_SET = os.getenv("GOMODE_AP_PYTHON") is not None
AP_PYTHON = os.getenv("GOMODE_AP_PYTHON", sys.executable)
# The analyzer runs with asserts off (-O), as the released Archipelago generator does. Some
# apworlds trip an assert only a source run checks (a Choice with its own "random" option, an
# item object added to the pool twice): the seed was made without that check, so the rebuild
# must skip it too. It also skips a debug-only duplicate check over each world's item pool.
AP_PYTHON_FLAGS = ["-O"]
# The host's apworlds (placed on the bot server, e.g. via FTP) used to generate seeds.
APWORLDS_DIR = os.getenv("GOMODE_APWORLDS_DIR")
# Optional local AP git checkout for fast, offline provisioning (else download from GitHub).
AP_REPO = os.getenv("GOMODE_AP_REPO")
# Where provisioned AP trees + the precomputed seed cache live.
RUNTIME_DIR = os.getenv("GOMODE_RUNTIME_DIR", os.path.join(ANALYZER_DIR, "runtime"))


# Seconds one slot may take in the precompute before it is put off until the other slots are done
# (GOMODE_SLOT_TIMEOUT, default 300), and the longer limit it then gets on its retry before it is
# recorded as unsupported (GOMODE_SLOT_RETRY_TIMEOUT, default 1800; 0 means no retry).
SLOT_TIMEOUT, SLOT_RETRY_TIMEOUT = slot_limits()
# The registration's progress message is edited at most this often (seconds).
PROGRESS_INTERVAL = 5.0

DATA_DIR = "data"


def _rk(run):
    """Keyword arguments that hand a run on. Nothing for the single-run setup, so those calls
    look exactly as they always did."""
    return {"run": run} if run is not None else {}


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


async def _run_with_progress(cmd: list[str], on_event) -> tuple[int, str, str]:
    """Like _run, but reads stderr as it arrives: each JSON line with a "progress" key is handed
    to `await on_event(event)` and left out of the returned stderr text."""
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    err_lines: list[str] = []

    async def handle(raw: bytes) -> None:
        line = raw.decode("utf-8", "replace")
        if line.lstrip().startswith("{"):
            try:
                event = json.loads(line)
            except ValueError:
                event = None
            if isinstance(event, dict) and "progress" in event:
                try:
                    await on_event(event)
                except Exception:
                    log_error.exception("Could not report precompute progress")
                return
        err_lines.append(line)

    async def pump() -> None:
        buf = b""
        while True:
            chunk = await proc.stderr.read(65536)
            if not chunk:
                break
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for raw in lines:
                await handle(raw.rstrip(b"\r"))
        if buf:
            await handle(buf.rstrip(b"\r"))

    out, _ = await asyncio.gather(proc.stdout.read(), pump())
    await proc.wait()
    return proc.returncode, out.decode("utf-8", "replace"), "\n".join(err_lines)


def _duration(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def _limit_text(seconds) -> str:
    """'a 30-minute limit' / 'a 90-second limit'."""
    seconds = float(seconds or 0)
    if seconds >= 60 and seconds % 60 == 0:
        return f"a {int(seconds // 60)}-minute limit"
    return f"a {seconds:g}-second limit"


class _Progress:
    """Turns the precompute's progress events into bot log lines and a progress message that is
    sent at most every PROGRESS_INTERVAL seconds (the newest text wins; a held-back one is sent
    once the interval has passed)."""

    def __init__(self, say):
        self.say = say
        self.last = asyncio.get_running_loop().time()   # the message just sent stays up a while
        self.pending: str | None = None
        self.timer: asyncio.Task | None = None

    async def _send(self, text: str) -> None:
        self.last = asyncio.get_running_loop().time()
        self.pending = None
        await self.say(text)

    async def _later(self, delay: float) -> None:
        await asyncio.sleep(delay)
        self.timer = None
        if self.pending is not None:
            try:
                await self._send(self.pending)
            except Exception:
                log_error.exception("Could not show precompute progress")

    async def show(self, text: str, now: bool = False) -> None:
        wait = PROGRESS_INTERVAL - (asyncio.get_running_loop().time() - self.last)
        if now or wait <= 0:
            self.cancel()
            await self._send(text)
            return
        self.pending = text
        if self.timer is None:
            self.timer = asyncio.create_task(self._later(wait))

    def cancel(self) -> None:
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        self.pending = None

    async def event(self, ev: dict) -> None:
        kind = ev.get("progress")
        where = f"{ev.get('index')}/{ev.get('total')}"
        who = f"{ev.get('name')} ({ev.get('game')})"
        if kind == "loading":
            if ev.get("attempt", 1) > 1:
                log_goal.info("Precompute: restarting the analysis for the remaining slots")
            else:
                log_goal.info("Precompute: loading the seed and its worlds")
            await self.show("Loading the seed and its worlds for the go-mode analysis...")
        elif kind == "start":
            again = " (retry)" if ev.get("retry") else ""
            log_goal.info("Precompute: analyzing slot %s: %s%s", where, who, again)
            await self.show(f"{'Retrying' if again else 'Analyzing'} slot {where}: {who}...")
        elif kind == "done":
            log_goal.info("Precompute: slot %s %s finished in %ss%s: %s", where, who, ev.get("seconds"),
                          " on its retry" if ev.get("retry") else "", ev.get("status"))
        elif kind == "deferred":
            text = f"Slot {where} {who} ran past {ev.get('limit'):g}s, will retry at the end"
            log_goal.info("Precompute: %s", text)
            await self.show(text + ". Continuing with the rest...", now=True)
        elif kind == "retrying":
            n = ev.get("count")
            text = f"Retrying {n} slot{'s' if n != 1 else ''} with {_limit_text(ev.get('limit'))}"
            log_goal.info("Precompute: %s", text)
            await self.show(text + "...", now=True)
        elif kind == "timeout":
            log_goal.info("Precompute: slot %s %s stopped after %ss (over the %gs %slimit), marked unsupported",
                          where, who, ev.get("seconds"), ev.get("limit"), "retry " if ev.get("retry") else "")
            await self.show(f"Slot {where}: {who} took longer than {ev.get('limit'):g} seconds"
                            f"{' on its retry' if ev.get('retry') else ''} and was skipped. "
                            "Continuing with the rest...", now=True)
        elif kind == "finished":
            again = ev.get("retried_ok")
            over = ev.get("timed_out")
            text = (f"Analyzed {ev.get('slots')} slots in {_duration(ev.get('seconds') or 0)}: "
                    f"{ev.get('ok')} ok{f' ({again} on a retry)' if again else ''}, "
                    f"{ev.get('unsupported')} unsupported"
                    + (f" ({over} past {'both time limits' if ev.get('retry_limit') else 'the time limit'})"
                       if over else "")
                    + f", {ev.get('errors')} errors.")
            log_goal.info("Precompute: %s", text)
            await self.show(text, now=True)


def _quiet_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def apworlds_source(run=None) -> tuple[str, str]:
    """(folder, description) of the apworlds to provision for a run. A linked run uses the folder
    named after it (its name, else its id) inside GOMODE_APWORLDS_DIR when there is one, so two
    runs can hold different versions of a world; otherwise the shared GOMODE_APWORLDS_DIR."""
    if run is not None:
        for cand in (run.name, run.id):
            if cand and cand not in (".", "..") and os.path.basename(cand) == cand:
                folder = os.path.join(APWORLDS_DIR, cand)
                if os.path.isdir(folder):
                    return folder, f"the folder {cand!r} in the apworlds directory"
    return APWORLDS_DIR, "the shared apworlds directory"


def runtime_dir_for(run=None) -> str:
    """Where a run's Archipelago tree and manifest live: its own folder under the runtime directory.
    The single-run setup (no registry) keeps using the runtime directory itself."""
    return os.path.join(RUNTIME_DIR, run.id) if run is not None else RUNTIME_DIR


async def register_seed(seed_zip: str, *, progress=None, run=None) -> dict:
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

    runtime = runtime_dir_for(run)
    apworlds, apworlds_from = apworlds_source(run)
    os.makedirs(runtime, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)

    # 1. Provision the matching AP source + the host's apworlds (plain Python: no AP import).
    await say("Provisioning the matching Archipelago version (this can take a minute)...")
    cmd = [sys.executable, os.path.join(ANALYZER_DIR, "provision.py"),
           "--seed-zip", seed_zip, "--runtime-dir", runtime, "--apworlds", apworlds]
    if AP_REPO:
        cmd += ["--ap-repo", AP_REPO]
    rc, out, err = await _run(cmd)
    if rc != 0:
        raise RuntimeError(f"Provisioning failed:\n{(err or out)[-1500:]}")
    manifest = json.loads(out)
    ap_path, version = manifest["ap_path"], manifest["version"]
    await say(f"Provisioned Archipelago {version} with {len(manifest['apworlds_installed'])} "
              f"apworlds{f' from {apworlds_from}' if run is not None else ''}. Analyzing every slot's go-mode requirements -- this is a one-time "
              "step and may take a few minutes...")

    # 2. Precompute the requirement tree (or conservative fallback) for every slot. Needs
    #    the AP env, so use the configured AP interpreter. Write to a TEMP cache and only
    #    swap it into place once the run is known-good, so a failed re-registration can't
    #    truncate the live cache and silently break the previously-registered seed.
    tmp_cache = runs.seed_cache_path(run) + ".tmp"
    #    Each slot's start and finish arrive as progress events while it runs.
    cmd = [AP_PYTHON, *AP_PYTHON_FLAGS, os.path.join(ANALYZER_DIR, "precompute.py"),
           "--ap-path", ap_path, "--seed-zip", seed_zip, "--out", tmp_cache,
           "--slot-timeout", f"{SLOT_TIMEOUT:g}", "--retry-timeout", f"{SLOT_RETRY_TIMEOUT:g}"]
    shown = _Progress(say)
    try:
        rc, out, err = await _run_with_progress(cmd, shown.event)
    finally:
        shown.cancel()
    if rc != 0:
        _quiet_remove(tmp_cache)
        raise RuntimeError(f"Precompute failed:\n{(err or out)[-1500:]}")
    try:
        summary = json.loads(out.strip().splitlines()[-1])  # last stdout line is the summary
    except (ValueError, IndexError) as exc:
        _quiet_remove(tmp_cache)
        raise RuntimeError(f"Precompute produced no valid summary: {exc}\n{(out or err)[-800:]}")
    os.replace(tmp_cache, runs.seed_cache_path(run))  # atomic adopt of the new, validated cache

    # 3. Record the active seed (atomically too). Only one seed is registered at a time.
    registry = {
        "seed": summary["seed"],
        "version": summary["version"],
        "slot_count": summary["slots"],
        "verified": summary["verified"],
        "exact": summary.get("exact", 0),
        "unsupported": summary["unsupported"],
        "unsupported_slots": summary.get("unsupported_slots", []),
        "cache_path": os.path.abspath(runs.seed_cache_path(run)),
        "ap_path": ap_path,
        "seed_zip": os.path.abspath(seed_zip),
        "registered_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    tmp_reg = runs.run_path("registered_seed.json", run) + ".tmp"
    with open(tmp_reg, "w", encoding="utf-8") as fh:
        json.dump(registry, fh, indent=2)
    os.replace(tmp_reg, runs.run_path("registered_seed.json", run))
    return registry


def load_registry(run=None) -> dict | None:
    try:
        with open(runs.run_path("registered_seed.json", run), encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def load_cache(run=None) -> dict | None:
    """The precomputed per-slot requirements for the active seed, or None if unregistered."""
    reg = load_registry(**_rk(run))
    path = reg["cache_path"] if reg else runs.seed_cache_path(run)
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

def _load_items_received(run=None) -> dict:
    try:
        with open(runs.run_path("items_received.json", run), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


_go_mode_seen: dict = {}
_checks_seen: dict = {}


def _log_change(memo: dict, name: str, value, logger, text: str, *args, run=None) -> None:
    """Log a result the first time it is seen and whenever it changes (the loops repeat it every minute)."""
    key = (run.id, name) if run is not None else name
    if memo.get(key) != value:
        memo[key] = value
        logger.info(text if run is None else "[" + run.name + "] " + text, *args)


def _why(st: dict) -> str:
    """Plain words for a slot status that is not 'ok' (the reason, when the analyzer gave one)."""
    status = st.get("status") or "unknown"
    reason = (st.get("reason") or "").strip()
    return f"{status}: {reason[:120]}" if reason else status


def _go_mode_words(st: dict) -> str:
    if st.get("status") != "ok":
        return f"not checked ({_why(st)})"
    igm = st.get("in_go_mode")
    return "in go mode" if igm is True else "not in go mode yet" if igm is False else "could not tell"


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


def current_inventory(slot_name: str, run=None) -> dict:
    """The slot's current {item_name: count} from items_received.json on disk."""
    return inventory_for_slot(_load_items_received(**_rk(run)), slot_name)


def current_checked(slot_name: str, run=None) -> list:
    """The slot's checked location names from items_received.json on disk."""
    return checked_for_slot(_load_items_received(**_rk(run)), slot_name)


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
    def __init__(self, run=None):
        self.run = run       # the run this analyzer serves; None for the single-run (no registry) setup
        self.proc = None
        self.key = None      # (seed, ap_path, seed_zip, registered_at) the running worker was started for
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
        log_path = WORKER_LOG
        if self.run is not None:
            log_path = os.path.join(RUNTIME_DIR, self.run.id, "worker.log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        # stderr to a file: Archipelago's chatter would otherwise fill the pipe and stall it.
        self.log = open(log_path, "a", encoding="utf-8")
        self.proc = await asyncio.create_subprocess_exec(
            AP_PYTHON, *AP_PYTHON_FLAGS, os.path.join(ANALYZER_DIR, "worker.py"),
            "--ap-path", reg["ap_path"], "--seed-zip", reg["seed_zip"],
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=self.log,
            limit=64 * 1024 * 1024)
        line = await asyncio.wait_for(self.proc.stdout.readline(), WORKER_READY_TIMEOUT)
        ready = json.loads(line) if line else {}
        if not ready.get("ready"):
            await self._stop()
            raise RuntimeError(f"analyzer worker didn't start; see {WORKER_LOG}")
        self.key = (reg.get("seed"), reg.get("ap_path"), reg.get("seed_zip"), reg.get("registered_at"))
        log_setup.get('start').info("Go-mode analyzer ready: seed %s, %s of %s slots built in %ss",
                                    ready.get('seed'), ready.get('built'), ready.get('slots'), ready.get('seconds'))

    async def request(self, op: str, **payload):
        reg = load_registry(**_rk(self.run))
        if not reg:
            raise RuntimeError("no seed registered")
        key = (reg.get("seed"), reg.get("ap_path"), reg.get("seed_zip"), reg.get("registered_at"))
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


_worker = _Worker()          # the single-run (no registry) analyzer
_workers: dict = {}          # run id -> that run's analyzer, created the first time it is asked for


def worker_for(run=None) -> "_Worker":
    """The analyzer of this run (run=None: the only run, or the single-run one with no registry).
    Made on first use, so a run with no registered seed never starts a subprocess."""
    run = run or runs.default_run()
    if run is None:
        return _worker
    if run.id not in _workers:
        _workers[run.id] = _Worker(run)
    return _workers[run.id]


async def stop_worker(run) -> None:
    """Stop and forget one run's analyzer (the run was unlinked)."""
    worker = _workers.pop(run.id, None)
    if worker:
        await worker._stop()


async def warm_worker(run=None) -> None:
    """Start the analyzer for the registered seed ahead of the first query (bot startup)."""
    if load_registry(**_rk(run)) is None:
        return
    try:
        await worker_for(run).request("ping")
    except Exception as e:  # noqa: BLE001 -- a later query will retry
        log_error.exception("Go-mode analyzer could not start: %s", e)


async def go_mode_status(slot_names, *, items_received: dict | None = None, run=None) -> dict:
    """For each assigned slot name, return {status, in_go_mode, game, reason?}.

    Every supported slot is checked the way Universal Tracker does it: against its rebuilt
    world's real logic, in one request to the analyzer. Unsupported/unregistered slots are
    reported as such (in_go_mode = None).
    """
    cache, reg = load_cache(**_rk(run)), load_registry(**_rk(run))
    if not cache or not reg:
        return {name: {"status": "unregistered", "in_go_mode": None} for name in slot_names}
    if items_received is None:
        items_received = _load_items_received(**_rk(run))

    result: dict = {}
    batch: dict = {}
    checked: dict = {}
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
        checked[name] = checked_for_slot(items_received, name)
        result[name] = {"status": "ok", "in_go_mode": None, "game": game}

    if batch:
        try:
            oracle = await worker_for(run).request("go_mode", slots=batch, checked=checked)
        except Exception as e:  # noqa: BLE001 -- leave in_go_mode None; the next cycle retries
            log_error.exception("Go-mode analyzer request failed, will retry next cycle: %s", e)
            oracle = {}
        for name, info in oracle.items():
            if name in result:
                result[name]["in_go_mode"] = info.get("in_go_mode")
                if info.get("status") and info["status"] != "ok":
                    result[name]["status"] = info["status"]
                    result[name]["reason"] = info.get("reason", "")
    for name, st in result.items():
        _log_change(_go_mode_seen, name, (st.get("status"), st.get("in_go_mode")), log_goal,
                    "Go mode for %s (%s): %s", name, st.get("game") or "unknown game", _go_mode_words(st), run=run)
    return result


async def analyze_slot_live(slot_name: str, inventory: dict, colour: bool = False, run=None,
                            checked: list | None = None) -> dict | None:
    """Full on-demand analysis of one slot for the player's current inventory (and checked
    location names, when given). Returns the analysis dict (incl. `requirements_text`), or None
    if no seed is registered."""
    if load_registry(**_rk(run)) is None:
        return None
    extra = {} if checked is None else {"checked": checked}
    try:
        return await worker_for(run).request("analyze", slot=slot_name, inventory=inventory, colour=colour, **extra)
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "reason": str(e)[-500:]}


def checked_for_slot(items_received: dict, slot_name: str) -> list:
    """The slot's checked location names, as the tracker page lists them."""
    for slot_entry in (items_received or {}).values():
        if slot_name in slot_entry:
            return list(slot_entry[slot_name].get("Checked Locations", []) or [])
    return []


async def checks_in_logic(slot_names, *, items_received: dict | None = None,
                          list_names: bool = False, run=None) -> dict:
    """For each slot name, {status, game, reason?} plus, when ok, {in_logic, in_logic_done,
    total, done}: its checks reachable with what the player holds (as UT counts them) and how
    many of those are done, and all its checks and how many are done. With list_names, also
    "to_do": the names of the in-logic checks not done yet. One analyzer request for every
    supported slot."""
    cache, reg = load_cache(**_rk(run)), load_registry(**_rk(run))
    if not cache or not reg:
        return {name: {"status": "unregistered"} for name in slot_names}
    if items_received is None:
        items_received = _load_items_received(**_rk(run))

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
            checks = await worker_for(run).request("checks", slots=batch)
        except Exception as e:  # noqa: BLE001 -- slots stay "couldn't check right now"
            log_error.exception("Checks analyzer request failed: %s", e)
            checks = {}
        for name, info in checks.items():
            if name in result:
                result[name] = {**info, "game": result[name].get("game")}
    for name, st in result.items():
        if st.get("status") == "ok":
            words = f"{st.get('done', 0)} of {st.get('total', 0)} checks done, {st.get('in_logic_done', 0)} of {st.get('in_logic', 0)} in logic done"
        else:
            words = f"not checked ({_why(st)})"
        _log_change(_checks_seen, name, words, log_checks, "Checks for %s (%s): %s",
                    name, st.get("game") or "unknown game", words, run=run)
    return result


async def sphere_zero_checks(slot_names, *, items_received: dict | None = None, run=None) -> dict:
    """For each slot name, {status, game, reason?} plus, when ok, {total, done, to_do}: the slot's
    sphere 0 checks (reachable with only its start inventory), how many are done, and the names
    of those not done yet. One analyzer request for every supported slot."""
    cache, reg = load_cache(**_rk(run)), load_registry(**_rk(run))
    if not cache or not reg:
        return {name: {"status": "unregistered"} for name in slot_names}
    if items_received is None:
        items_received = _load_items_received(**_rk(run))

    result: dict = {}
    batch: dict = {}
    for name in slot_names:
        rec = slot_for_name(cache, name)
        if rec is None:
            result[name] = {"status": "unregistered"}
            continue
        game = rec.get("game")
        # The seed's own sphere 0 answers for a slot even when its game can't be rebuilt, so a slot
        # the rebuild doesn't support is still asked; it keeps its status if the seed has no answer.
        base = ({"status": "error", "game": game, "reason": "no result"} if rec.get("status") == "ok" else
                {"status": rec.get("status", "error"), "game": game, "reason": rec.get("reason", "")})
        if not any(name in entry for entry in items_received.values()):
            result[name] = ({"status": "error", "game": game, "reason": "no tracker data"}
                            if rec.get("status") == "ok" else base)
            continue
        tracker_game = _tracker_game_for_slot(items_received, name)
        if tracker_game and game and tracker_game.lower() != str(game).lower():
            result[name] = {"status": "tracker_mismatch", "game": game}
            continue
        batch[name] = {"checked": checked_for_slot(items_received, name)}
        result[name] = base

    if batch:
        try:
            found = await worker_for(run).request("sphere_zero", slots=batch)
        except Exception as e:  # noqa: BLE001 -- slots stay "couldn't check right now"
            log_error.exception("Sphere 0 analyzer request failed: %s", e)
            found = {}
        for name, info in found.items():
            if name in result and (info.get("status") == "ok" or result[name].get("status") == "error"):
                result[name] = {**info, "game": result[name].get("game")}
    return result


async def most_useful_items(slot_name: str, *, items_received: dict | None = None,
                            top: int = 5, run=None) -> dict:
    """{status, game, items?: [{"name", "opens"}], reason?}: the missing items that would each
    open the most new checks for the slot right now."""
    cache = load_cache(**_rk(run))
    if not cache or load_registry(**_rk(run)) is None:
        return {"status": "unregistered"}
    rec = slot_for_name(cache, slot_name)
    if rec is None:
        return {"status": "unregistered"}
    game = rec.get("game")
    if rec.get("status") != "ok":
        return {"status": rec.get("status", "error"), "game": game, "reason": rec.get("reason", "")}
    if items_received is None:
        items_received = _load_items_received(**_rk(run))
    tracker_game = _tracker_game_for_slot(items_received, slot_name)
    if tracker_game and game and tracker_game.lower() != str(game).lower():
        return {"status": "tracker_mismatch", "game": game}
    try:
        reply = await worker_for(run).request("useful_items", slot=rec.get("name", slot_name), top=top,
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


async def load_item_flags(run=None) -> dict | None:
    """Every slot's {item name: flags} for the registered seed, or None if unavailable."""
    reg = load_registry(**_rk(run))
    if not reg:
        return None
    seed = reg.get("seed")
    memo_key = (run.id if run is not None else None, seed)
    if memo_key in _item_flags_memo:
        return _item_flags_memo[memo_key]
    try:
        result = await worker_for(run).request("item_flags")
    except Exception as e:  # noqa: BLE001
        result = None
        log_error.exception("Could not read item classifications: %s", e)
    if not result or result.get("seed") != seed:
        # Remembered for this run so a broken setup doesn't retry every minute; registering a
        # seed (a new seed key) tries again.
        _item_flags_memo[memo_key] = None
        return None
    _item_flags_memo[memo_key] = result["item_flags"]
    return _item_flags_memo[memo_key]


def slot_item_flags(item_flags, items_received, slot_name) -> dict:
    """{item name: flags} for a slot, or {} when unknown or the tracker's game differs from the seed's."""
    rec = (item_flags or {}).get(slot_name)
    if not rec:
        return {}
    tracker_game = _tracker_game_for_slot(items_received, slot_name)
    if tracker_game and tracker_game.lower() != str(rec.get("game", "")).lower():
        return {}
    return rec.get("items", {})


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



def load_notified(current_seed: str, run=None) -> set:
    """Slot names already DM'd for the CURRENT seed. Auto-resets when the seed changes."""
    try:
        with open(runs.run_path("go_mode_notified.json", run), encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return set()
    if data.get("seed") != current_seed:
        return set()
    return set(data.get("notified", []))


def save_notified(current_seed: str, notified, run=None) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = runs.run_path("go_mode_notified.json", run) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"seed": current_seed, "notified": sorted(notified)}, fh, indent=2)
    os.replace(tmp, runs.run_path("go_mode_notified.json", run))


# --- unstuck alerts (opt-in, per player and slot) ----------------------------



def _write_json(path: str, data) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def load_unstuck_subscriptions(run=None) -> dict:
    """{discord user id: [slot names]} of players who signed up for unstuck alerts."""
    try:
        with open(runs.run_path("unstuck_alerts.json", run), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_unstuck_subscriptions(subs: dict, run=None) -> None:
    _write_json(runs.run_path("unstuck_alerts.json", run), {user: sorted(set(names)) for user, names in subs.items() if names})


def load_unstuck_state(current_seed: str, run=None) -> dict:
    """{slot name: {"to_do": int, "inventory": {...}}} last seen for the CURRENT seed."""
    try:
        with open(runs.run_path("unstuck_state.json", run), encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data.get("slots", {}) if data.get("seed") == current_seed else {}


def save_unstuck_state(current_seed: str, slots: dict, run=None) -> None:
    _write_json(runs.run_path("unstuck_state.json", run), {"seed": current_seed, "slots": slots})
