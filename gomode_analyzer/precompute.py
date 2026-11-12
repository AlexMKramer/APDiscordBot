"""Precompute every slot's go-mode requirements for a registered seed, ONCE, into a cache
the bot reads. Runs in the version-pinned AP environment (invoked by the bot as a
subprocess). For each slot it stores the requirement tree (verified) or the conservative
fallback, so the bot can later answer `/items_to_go_mode` and run the go-mode notification
without re-running the heavy analysis -- verified slots evaluate `satisfies(tree, inventory)`
in pure Python; only fallback slots need a live oracle check.

Output cache: {seed, version, slots: {slot_number: {name, game, status, requirements, ...}}}

The slots are analyzed by a worker process (this script with `--worker`), which loads the seed
and the worlds once and reports each slot as it starts and finishes. This process supervises it
without importing Archipelago. A slot that runs past the first time limit (`--slot-timeout`,
default GOMODE_SLOT_TIMEOUT or 300 seconds) is deferred: the worker is stopped and a new one
carries on with the other slots. Once they are all done, the deferred slots are tried again with
the retry limit (`--retry-timeout`, default GOMODE_SLOT_RETRY_TIMEOUT or 1800 seconds). Only a
slot that also runs past that one is recorded as unsupported. A retry limit of 0, or one not above
the first limit, turns the retry off: a slot over the first limit is then unsupported at once.

Output streams:
  * stdout: one line, the JSON summary (the last stdout line, which the bot reads).
  * stderr: one JSON line per progress event, `{"progress": ..., ...}`, mixed with any plain
    error text. Slot events carry index/total (the slot's place in the seed), slot, name, game:
      {"progress": "loading", "attempt": n}               a worker is loading the seed
      {"progress": "start", ...slot..., "retry": bool}
      {"progress": "done", ...slot..., "retry": bool, "status": ..., "seconds": s}
      {"progress": "deferred", ...slot..., "seconds": s, "limit": l}    retried at the end
      {"progress": "retrying", "count": n, "limit": l}    the retries start
      {"progress": "timeout", ...slot..., "retry": bool, "seconds": s, "limit": l}   unsupported
      {"progress": "finished", "slots": n, "ok": a, "unsupported": b, "errors": c,
       "timed_out": d, "retried_ok": e, "retry_limit": l, "seconds": s}
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time


def _child_flags() -> list:
    """The interpreter flags the analysis worker is started with: the same optimize level as this
    process (-O from the bot), so the worker also runs with asserts off, as the released generator does."""
    return ["-" + "O" * sys.flags.optimize] if sys.flags.optimize else []


DEFAULT_SLOT_TIMEOUT = 300.0
DEFAULT_RETRY_TIMEOUT = 1800.0
# Marks the worker's protocol lines on its stdout, so anything else a world prints there is ignored.
_TAG = "@@gomode-worker@@ "


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def slot_limits(first=None, retry=None) -> tuple[float, float]:
    """(first limit, retry limit) in seconds, from the given values, else GOMODE_SLOT_TIMEOUT and
    GOMODE_SLOT_RETRY_TIMEOUT. The first limit falls back to 300 when missing, not a number or not
    above 0. The retry limit falls back to 1800 (or twice the first limit, if that is more) when
    missing, not a number or below 0. A retry limit of 0, or one not above the first limit, comes
    back as 0: no retry."""
    first = _number(first if first is not None else os.getenv("GOMODE_SLOT_TIMEOUT"))
    if first is None or first <= 0:
        first = DEFAULT_SLOT_TIMEOUT
    retry = _number(retry if retry is not None else os.getenv("GOMODE_SLOT_RETRY_TIMEOUT"))
    if retry is None or retry < 0:
        retry = max(DEFAULT_RETRY_TIMEOUT, 2 * first)
    if retry <= first:
        retry = 0.0
    return first, retry


def _entry(sd, res) -> dict:
    return {
        "name": sd.name,
        "game": sd.game,
        "status": res.status,
        "reason": res.reason,
        "options_source": res.options_source,
        "regen": res.regen,
        "mismatch": res.mismatch,
        "requirements": res.requirements,   # verified tree OR conservative fallback
    }


def _timeout_entry(name: str, game: str, first: float, retry: float) -> dict:
    if retry:
        reason = (f"Rebuilding this slot took longer than {first:g} seconds, and again longer than "
                  f"{retry:g} seconds when it was retried, so it was stopped (the limits are "
                  "GOMODE_SLOT_TIMEOUT and GOMODE_SLOT_RETRY_TIMEOUT).")
    else:
        reason = (f"Rebuilding this slot took longer than {first:g} seconds, so it was stopped "
                  "(the limit is GOMODE_SLOT_TIMEOUT).")
    return {
        "name": name,
        "game": game,
        "status": "unsupported",
        "reason": reason,
        "options_source": "",
        "regen": "",
        "mismatch": None,
        "requirements": {},
    }


# --- worker -------------------------------------------------------------------------------------

def _worker(args) -> int:
    """Load the seed once and analyze the slots in order, reporting each on stdout."""
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    sys.path.insert(0, os.path.abspath(args.ap_path))
    logging.disable(logging.CRITICAL)
    import ModuleUpdate  # worlds that call it on import must not stop to ask to install packages
    ModuleUpdate.update_ran = True

    only = {int(x) for x in args.slots.split(",")} if args.slots else None
    real_stdout = sys.stdout

    def send(msg: dict) -> None:
        real_stdout.write(_TAG + json.dumps(msg) + "\n")
        real_stdout.flush()

    with contextlib.redirect_stdout(io.StringIO()):  # mute AP world-loading chatter
        import seed_data
        import engine

        seed = seed_data.load_seed(args.seed_zip)
        todo = [(sid, sd) for sid, sd in seed.slots.items() if only is None or sid in only]
        send({"event": "ready", "seed": seed.seed_name, "version": seed.version_str,
              "slots": [{"slot": sid, "name": sd.name, "game": sd.game} for sid, sd in todo]})
        for sid, sd in todo:
            send({"event": "start", "slot": sid})
            res = engine.analyze_slot(sd.game, sd.options, {}, **seed.engine_kwargs(sd))
            send({"event": "result", "slot": sid, "entry": _entry(sd, res)})
    send({"event": "end"})
    return 0


# --- supervisor ---------------------------------------------------------------------------------

class _Supervisor:
    def __init__(self, args, first: float, retry: float):
        self.args = args
        self.first = first
        self.retry = retry
        self.err_lock = threading.Lock()

    def emit(self, event: dict) -> None:
        with self.err_lock:
            sys.stderr.write(json.dumps(event) + "\n")
            sys.stderr.flush()

    def _forward_stderr(self, stream, tail: list) -> None:
        """Pass the worker's stderr on line by line (so it never splits a progress line) and keep
        the end of it for an error message."""
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", "replace")
            tail.append(line)
            del tail[:-200]
            with self.err_lock:
                sys.stderr.write(line)
                sys.stderr.flush()
        stream.close()

    @staticmethod
    def _read_stdout(stream, q: queue.Queue) -> None:
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith(_TAG):
                try:
                    q.put(json.loads(line[len(_TAG):]))
                except ValueError:
                    pass
        stream.close()
        q.put(None)  # end of output: the worker has exited

    def _spawn(self, slots):
        cmd = [sys.executable, *_child_flags(), os.path.abspath(__file__), "--worker",
               "--ap-path", self.args.ap_path, "--seed-zip", self.args.seed_zip, "--out", self.args.out]
        if slots is not None:
            cmd += ["--slots", ",".join(str(s) for s in slots)]
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        q: queue.Queue = queue.Queue()
        tail: list = []
        threads = [threading.Thread(target=self._read_stdout, args=(proc.stdout, q), daemon=True),
                   threading.Thread(target=self._forward_stderr, args=(proc.stderr, tail), daemon=True)]
        for t in threads:
            t.start()
        return proc, q, tail, threads

    @staticmethod
    def _stop(proc, threads) -> None:
        try:
            proc.kill()
        except OSError:
            pass
        proc.wait()
        for t in threads:
            t.join(timeout=5)

    def _pass(self, slots, limit: float, retry: bool) -> list:
        """Analyze `slots` (None: every slot of the seed) with `limit` seconds each, starting a new
        worker after a slot that runs past it. Returns the slots that ran past it."""
        over: list = []
        pending = slots         # slots the next worker should do (None: all of them)
        while True:
            self.attempt += 1
            self.emit({"progress": "loading", "attempt": self.attempt})
            proc, q, tail, threads = self._spawn(pending)
            current = None      # (slot id, start time) of the slot being analyzed
            finished = False
            stopped = None      # (slot id, seconds) of a slot that ran past the limit
            while True:
                wait = None if current is None else max(0.0, limit - (time.monotonic() - current[1]))
                try:
                    msg = q.get(timeout=wait)
                except queue.Empty:
                    sid, t0 = current
                    self._stop(proc, threads)
                    stopped = (sid, round(time.monotonic() - t0, 1))
                    break
                if msg is None:
                    proc.wait()
                    for t in threads:
                        t.join(timeout=5)
                    text = "".join(tail).strip()
                    if current is not None:
                        sid = current[0]
                        raise RuntimeError(f"The analysis stopped while rebuilding slot {sid} "
                                           f"({self.info[sid]['name']}, {self.info[sid]['game']}), "
                                           f"exit code {proc.returncode}.\n{text[-1500:]}")
                    if not finished:
                        raise RuntimeError(f"The analysis stopped before it finished, exit code "
                                           f"{proc.returncode}.\n{text[-1500:]}")
                    break
                event = msg.get("event")
                if event == "ready":
                    self.seed, self.version = msg["seed"], msg["version"]
                    for s in msg["slots"]:
                        if s["slot"] not in self.info:
                            self.order.append(s["slot"])
                            self.info[s["slot"]] = {"name": s["name"], "game": s["game"]}
                    if slots is None:
                        slots = list(self.order)
                elif event == "start":
                    current = (msg["slot"], time.monotonic())
                    self.emit(dict(self._describe(msg["slot"]), progress="start", retry=retry))
                elif event == "result":
                    sid, t0 = current
                    self.entries[sid] = msg["entry"]
                    current = None
                    self.emit(dict(self._describe(sid), progress="done", retry=retry,
                                   status=msg["entry"]["status"],
                                   seconds=round(time.monotonic() - t0, 1)))
                elif event == "end":
                    finished = True
            if stopped is not None:
                sid, seconds = stopped
                over.append(sid)
                if retry or not self.retry:
                    # Past the last limit there is: the slot is not supported.
                    self.entries[sid] = _timeout_entry(self.info[sid]["name"], self.info[sid]["game"],
                                                       self.first, self.retry if retry else 0)
                    self.emit(dict(self._describe(sid), progress="timeout", retry=retry,
                                   seconds=seconds, limit=limit))
                else:
                    self.emit(dict(self._describe(sid), progress="deferred", seconds=seconds, limit=limit))
            pending = [s for s in (slots or []) if s not in self.entries and s not in over]
            if finished or not pending:
                return over

    def run(self) -> dict:
        only = [int(x) for x in self.args.slots.split(",")] if self.args.slots else None
        started = time.monotonic()
        self.order: list = []       # slot ids in the seed's order, from the first worker
        self.info: dict = {}        # slot id -> {"name", "game"}
        self.entries: dict = {}     # slot id -> cache entry
        self.seed = self.version = None
        self.attempt = 0

        deferred = self._pass(only, self.first, retry=False)
        retried_ok = 0
        if deferred and self.retry:
            deferred = [s for s in self.order if s in deferred]     # retried in the seed's order
            self.emit({"progress": "retrying", "count": len(deferred), "limit": self.retry})
            gave_up = self._pass(deferred, self.retry, retry=True)
            retried_ok = sum(1 for s in deferred if s not in gave_up)

        slots = {str(sid): self.entries[sid] for sid in self.order if sid in self.entries}
        statuses = [e["status"] for e in slots.values()]
        self.emit({"progress": "finished", "slots": len(slots),
                   "ok": statuses.count("ok"), "unsupported": statuses.count("unsupported"),
                   "errors": statuses.count("error"),
                   "timed_out": sum(1 for e in slots.values() if "GOMODE_SLOT_TIMEOUT" in e["reason"]),
                   "retried_ok": retried_ok, "retry_limit": self.retry,
                   "seconds": round(time.monotonic() - started, 1)})
        return {"seed": self.seed, "version": self.version, "slots": slots}

    def _describe(self, sid) -> dict:
        return {"index": self.order.index(sid) + 1, "total": len(self.order), "slot": sid,
                "name": self.info[sid]["name"], "game": self.info[sid]["game"]}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Precompute go-mode requirement trees for a seed")
    p.add_argument("--ap-path", required=True, help="version-matched AP source tree")
    p.add_argument("--seed-zip", required=True)
    p.add_argument("--out", required=True, help="cache JSON to write")
    p.add_argument("--slots", help="comma-separated slot numbers to limit (testing)")
    p.add_argument("--slot-timeout", type=float, default=None,
                   help="seconds one slot may take before it is put off to be retried at the end "
                        "(default GOMODE_SLOT_TIMEOUT, else 300)")
    p.add_argument("--retry-timeout", type=float, default=None,
                   help="seconds a put-off slot may take on its retry before it is recorded as "
                        "unsupported; 0, or not above the first limit, means no retry "
                        "(default GOMODE_SLOT_RETRY_TIMEOUT, else 1800)")
    p.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = p.parse_args(argv)

    if args.worker:
        return _worker(args)

    first, retry = slot_limits(args.slot_timeout, args.retry_timeout)
    try:
        cache = _Supervisor(args, first, retry).run()
    except RuntimeError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, indent=2)

    summary = {
        "seed": cache["seed"],
        "version": cache["version"],
        "slots": len(cache["slots"]),
        "verified": sum(1 for s in cache["slots"].values()
                        if s["requirements"].get("verified")),
        # Rebuilt as the same world the real generation built (locations + progression items).
        "exact": sum(1 for s in cache["slots"].values() if s["status"] == "ok" and s["mismatch"] == 0),
        "unsupported": sum(1 for s in cache["slots"].values() if s["status"] != "ok"),
        "unsupported_slots": [{"name": s["name"], "game": s["game"], "reason": s["reason"]}
                              for s in cache["slots"].values() if s["status"] != "ok"],
        "out": os.path.abspath(args.out),
    }
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
