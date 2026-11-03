"""Runs: which Archipelago runs the bot knows about, and where each one keeps its files.

data/runs.json is the registry. Each run keeps its own files in data/runs/<run_id>/ and its
go-mode seed cache in <runtime dir>/<run_id>/seed_cache.json. Files shared by every run
(data_package.json, datapackage_cache/, logs/) stay directly in data/.

With no registry at all (a fresh install, or one that never set a tracker) the bot works in
"legacy" mode and keeps per-run files directly in data/, exactly as it did before runs existed.
"""
import dataclasses
import json
import os
import re
import shutil
import time

DATA_DIR = "data"
REGISTRY_NAME = "runs.json"
BACKUP_NAME = "_pre_multirun"
FALLBACK_NAME = "Run 1"

# Every file that belongs to one run (and so moves into its folder).
PER_RUN_FILES = (
    "slot_info.json", "listeners.json", "items_received.json", "announced_marks.json",
    "hints.json", "hints_pin_state.json", "percentage_state.json", "registered_seed.json",
    "start_inventory.json", "go_mode_notified.json", "unstuck_alerts.json", "unstuck_state.json",
    "server_address.txt",
)
SEED_CACHE_NAME = "seed_cache.json"


class AmbiguousRun(RuntimeError):
    """More than one run is linked and the caller did not say which one."""


@dataclasses.dataclass
class Run:
    id: str
    name: str
    channel_id: str = ""
    channel_name: str = ""
    guild_id: str = ""
    tracker_url: str = ""
    server_address: str = ""
    created: str = ""
    active: bool = True

    @property
    def dir(self) -> str:
        return os.path.join(DATA_DIR, "runs", self.id)

    def path(self, name: str) -> str:
        os.makedirs(self.dir, exist_ok=True)
        return os.path.join(self.dir, name)

    @property
    def cache_path(self) -> str:
        return os.path.join(runtime_dir(), self.id, SEED_CACHE_NAME)


def runtime_dir() -> str:
    return os.getenv("GOMODE_RUNTIME_DIR") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "gomode_analyzer", "runtime")


# --- registry ---------------------------------------------------------------------------------

def registry_path() -> str:
    return os.path.join(DATA_DIR, REGISTRY_NAME)


_cache = {"key": None, "runs": {}}


def load_runs() -> dict:
    """{run_id: Run}, in the order they were linked. Empty with no registry."""
    path = registry_path()
    try:
        st = os.stat(path)
    except OSError:
        _cache.update(key=None, runs={})
        return {}
    key = (os.path.abspath(path), st.st_mtime_ns, st.st_size)
    if _cache["key"] == key:
        return dict(_cache["runs"])
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return dict(_cache["runs"])  # half-written or unreadable: keep the last good copy
    fields = {f.name for f in dataclasses.fields(Run)}
    runs = {}
    for run_id, entry in (raw.get("runs") or {}).items():
        if isinstance(entry, dict):
            runs[run_id] = Run(**{**{k: v for k, v in entry.items() if k in fields}, "id": run_id})
    _cache.update(key=key, runs=runs)
    return dict(runs)


def save_runs(runs: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    body = {"runs": {rid: {k: v for k, v in dataclasses.asdict(r).items() if k != "id"}
                     for rid, r in runs.items()}}
    tmp = registry_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(body, fh, indent=2)
    os.replace(tmp, registry_path())
    _cache["key"] = None


def active_runs() -> list:
    return [r for r in load_runs().values() if r.active]


def get_run(run_id: str):
    return load_runs().get(run_id)


def run_for_channel(channel_id):
    """The active run linked to this channel id, or None."""
    wanted = str(channel_id) if channel_id is not None else ""
    for run in active_runs():
        if wanted and run.channel_id == wanted:
            return run
    return None


def run_for_ctx(ctx):
    """The run for the channel a command or autocomplete was used in; None when the channel
    is not linked (or it is a DM). A thread belongs to its parent channel's run."""
    inter = getattr(ctx, "interaction", None)
    channel = getattr(ctx, "channel", None) or getattr(inter, "channel", None)
    ids = [getattr(ctx, "channel_id", None), getattr(inter, "channel_id", None),
           getattr(channel, "id", None), getattr(channel, "parent_id", None)]
    for cid in ids:
        run = run_for_channel(cid) if isinstance(cid, (int, str)) else None
        if run:
            return run
    return None


def default_run():
    """The only active run, or None (legacy mode, or no runs). Raises AmbiguousRun with several."""
    runs = active_runs()
    if len(runs) > 1:
        raise AmbiguousRun("more than one run is linked; say which one")
    return runs[0] if runs else None


def run_path(name: str, run=None) -> str:
    """Path of a per-run file. Defaults to the only run; in legacy mode it is data/<name>."""
    run = run or default_run()
    if run is None:
        os.makedirs(DATA_DIR, exist_ok=True)
        return os.path.join(DATA_DIR, name)
    return run.path(name)


def seed_cache_path(run=None) -> str:
    run = run or default_run()
    if run is None:
        return os.path.join(runtime_dir(), SEED_CACHE_NAME)
    os.makedirs(os.path.dirname(run.cache_path), exist_ok=True)
    return run.cache_path


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:40] or "run"


def make_run_id(name: str, taken=()) -> str:
    base = slug(name)
    run_id, n = base, 2
    while run_id in taken:
        run_id, n = f"{base}-{n}", n + 1
    return run_id


# --- migration of the old single run ----------------------------------------------------------

def _copy_verified(src: str, dst: str) -> None:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if dst.endswith(SEED_CACHE_NAME):
        try:
            if os.path.exists(dst):
                os.remove(dst)
            os.link(src, dst)  # the cache can be huge: share the file instead of copying it
            return
        except OSError:
            pass
    shutil.copy2(src, dst + ".part")
    if os.path.getsize(src) != os.path.getsize(dst + ".part"):
        raise OSError(f"copy of {src} did not match")
    os.replace(dst + ".part", dst)


def migrate_single_run(channel_id, tracker_url, channel_name=None, server_address=None):
    """Turn the old single-run setup into the first linked run. Returns the new Run, or None
    when there is nothing to do (a registry exists, or no channel/tracker was configured).

    The per-run files are copied into data/runs/<id>/ and checked, then the originals are moved
    to data/_pre_multirun/ (kept, so a rollback is just moving them back). runs.json is written
    last, so a run that dies midway is simply done again on the next start."""
    if os.path.exists(registry_path()):
        return None
    channel_id = str(channel_id or "").strip()
    tracker_url = (tracker_url or "").strip()
    if not channel_id or not tracker_url:
        return None
    name = (channel_name or "").strip() or FALLBACK_NAME
    run = Run(id=slug(name), name=name, channel_id=channel_id,
              channel_name=(channel_name or "").strip(), tracker_url=tracker_url,
              created=time.strftime("%Y-%m-%d %H:%M:%S"))
    backup = os.path.join(DATA_DIR, BACKUP_NAME)

    moves = []  # (original path, backup path)
    for fname in PER_RUN_FILES:
        src = os.path.join(DATA_DIR, fname)
        if os.path.isfile(src):
            _copy_verified(src, run.path(fname))
            moves.append((src, os.path.join(backup, fname)))
    cache_src = os.path.join(runtime_dir(), SEED_CACHE_NAME)
    if os.path.isfile(cache_src):
        _copy_verified(cache_src, run.cache_path)
        moves.append((cache_src, os.path.join(backup, "runtime", SEED_CACHE_NAME)))

    # the registered seed remembers where its cache lives
    reg_path = os.path.join(run.dir, "registered_seed.json")
    if os.path.isfile(reg_path):
        with open(reg_path, encoding="utf-8") as fh:
            reg = json.load(fh)
        if isinstance(reg, dict) and reg.get("cache_path"):
            reg["cache_path"] = os.path.abspath(run.cache_path)
            with open(reg_path + ".tmp", "w", encoding="utf-8") as fh:
                json.dump(reg, fh, indent=2)
            os.replace(reg_path + ".tmp", reg_path)

    run.server_address = (server_address or "").strip()
    if not run.server_address:
        try:
            with open(os.path.join(run.dir, "server_address.txt"), encoding="utf-8") as fh:
                run.server_address = fh.read().strip()
        except OSError:
            pass

    for src, dst in moves:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.replace(src, dst)  # an earlier interrupted attempt may have left one: newest wins
    save_runs({run.id: run})
    return run
