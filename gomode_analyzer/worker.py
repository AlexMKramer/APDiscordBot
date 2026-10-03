"""Long-running analyzer for the bot: builds every slot of the registered seed once, then answers
requests from memory instead of rebuilding worlds in a new subprocess for each one.

Runs inside the version-pinned AP environment. Protocol: one JSON object per line on stdin,
one JSON reply per line on stdout. The first line out is {"ready": true, ...} once every slot
is built. Each request is {"id": ..., "op": ..., ...}; each reply is {"id": ..., "ok": true,
"result": ...} or {"id": ..., "ok": false, "error": "..."}. Archipelago's console chatter goes
to stderr, so stdout carries nothing but replies.

  python worker.py --ap-path <AP> --seed-zip AP_<seed>.zip
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import sys
import time


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Persistent go-mode analyzer")
    parser.add_argument("--ap-path", required=True, help="Path to the version-matched AP source tree")
    parser.add_argument("--seed-zip", required=True, help="Path to the generated AP_<seed>.zip")
    args = parser.parse_args(argv)

    out = sys.stdout
    sys.stdout = sys.stderr  # world loading and building print to stdout; keep replies clean
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path[:0] = [here, os.path.abspath(args.ap_path)]
    logging.disable(logging.CRITICAL)
    import ModuleUpdate  # worlds that call it on import must not stop to ask to install packages
    ModuleUpdate.update_ran = True
    import seed_data
    import engine
    import requirements

    started = time.time()
    seed = seed_data.load_seed(args.seed_zip)
    slots = {}  # lowercase slot name -> (SlotData, PreparedSlot or None, SlotResult)
    for sd in seed.slots.values():
        try:
            prepared, result = engine.prepare_slot(sd.game, sd.options, **seed.engine_kwargs(sd))
        except Exception as exc:  # noqa: BLE001 -- one bad slot must not stop the others
            prepared = None
            result = engine.SlotResult(slot=sd.slot, name=sd.name, game=sd.game, status="error",
                                       reason=f"{type(exc).__name__}: {exc}")
        slots[sd.name.lower()] = (sd, prepared, result)

    def reply(message: dict) -> None:
        out.write(json.dumps(message) + "\n")
        out.flush()

    reply({"ready": True, "seed": seed.seed_name, "slots": len(slots),
           "built": sum(1 for _, prepared, _ in slots.values() if prepared is not None),
           "seconds": round(time.time() - started, 1)})

    def lookup(name: str):
        entry = slots.get(str(name).lower())
        if entry is None:
            raise KeyError(f"slot {name!r} not found")
        return entry

    def fresh(result):
        """A clean copy of the slot's build result to fill in for one query."""
        return dataclasses.replace(result, in_go_mode=None, items_needed=[], requirements={},
                                   unknown_inventory=[])

    def checked_ids(sd, names) -> set:
        # The tracker lists names from the seed's own datapackage; compare by ID.
        name_to_id = sd.datapackage.get("location_name_to_id", {})
        return {name_to_id[n] for n in names or [] if n in name_to_id}

    def per_slot(requested: dict, answer) -> dict:
        results = {}
        for name, request in requested.items():
            try:
                sd, prepared, result = lookup(name)
                if prepared is None:
                    results[name] = {"status": result.status, "reason": result.reason}
                else:
                    results[name] = answer(sd, prepared, result, request)
            except Exception as exc:  # noqa: BLE001 -- one bad slot must not fail the batch
                results[name] = {"status": "error", "reason": f"{type(exc).__name__}: {exc}"}
        return results

    def go_mode(sd, prepared, result, inventory):
        res = engine.analyze_prepared(prepared, fresh(result), inventory or {}, fast=True)
        return {"status": res.status, "in_go_mode": res.in_go_mode, "reason": res.reason}

    def checks(sd, prepared, result, request):
        counts = engine.checks_in_logic(prepared, request.get("inventory") or {},
                                        checked_ids(sd, request.get("checked")),
                                        list_names=bool(request.get("list_names")))
        return {"status": "ok", **counts}

    def handle(request: dict):
        op = request.get("op")
        if op == "ping":
            return {"seed": seed.seed_name}
        if op == "go_mode":
            return per_slot(request.get("slots") or {}, lambda sd, p, r, inv: go_mode(sd, p, r, inv))
        if op == "checks":
            return per_slot(request.get("slots") or {}, checks)
        if op == "analyze":
            sd, prepared, result = lookup(request["slot"])
            if prepared is None:
                return result.to_dict()
            res = engine.analyze_prepared(prepared, fresh(result), request.get("inventory") or {})
            output = {**res.to_dict(), "seed": seed.seed_name, "version": seed.version_str}
            if res.status == "ok" and not res.in_go_mode:
                output["requirements_text"] = requirements.render_requirements(res.requirements)
            return output
        if op == "useful_items":
            sd, prepared, result = lookup(request["slot"])
            if prepared is None:
                return {"status": result.status, "reason": result.reason}
            items = engine.most_useful_items(prepared, request.get("inventory") or {},
                                             checked_ids(sd, request.get("checked")),
                                             top=int(request.get("top", 5)))
            return {"status": "ok", "items": items}
        if op == "item_flags":
            return {"seed": seed.seed_name,
                    "item_flags": {sd.name: {"game": sd.game, "items": sd.item_flags}
                                   for sd, _, _ in slots.values()}}
        raise ValueError(f"unknown op {op!r}")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        request_id = None
        try:
            request = json.loads(line)
            request_id = request.get("id")
            reply({"id": request_id, "ok": True, "result": handle(request)})
        except Exception as exc:  # noqa: BLE001 -- report it and keep serving
            reply({"id": request_id, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
