"""Precompute every slot's go-mode requirements for a registered seed, ONCE, into a cache
the bot reads. Runs in the version-pinned AP environment (invoked by the bot as a
subprocess). For each slot it stores the requirement tree (verified) or the conservative
fallback, so the bot can later answer `/items_to_go_mode` and run the go-mode notification
without re-running the heavy analysis -- verified slots evaluate `satisfies(tree, inventory)`
in pure Python; only fallback slots need a live oracle check.

Output cache: {seed, version, slots: {slot_number: {name, game, status, requirements, ...}}}
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import os
import sys


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Precompute go-mode requirement trees for a seed")
    p.add_argument("--ap-path", required=True, help="version-matched AP source tree")
    p.add_argument("--seed-zip", required=True)
    p.add_argument("--out", required=True, help="cache JSON to write")
    p.add_argument("--slots", help="comma-separated slot numbers to limit (testing)")
    args = p.parse_args(argv)

    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    sys.path.insert(0, os.path.abspath(args.ap_path))
    logging.disable(logging.CRITICAL)

    only = {int(x) for x in args.slots.split(",")} if args.slots else None

    real_stdout = sys.stdout
    cache = None
    with contextlib.redirect_stdout(io.StringIO()):  # mute AP world-loading chatter
        import seed_data
        import engine

        seed = seed_data.load_seed(args.seed_zip)
        slots = {}
        for sid, sd in seed.slots.items():
            if only is not None and sid not in only:
                continue
            res = engine.analyze_slot(sd.game, sd.options, {}, slot=sid, name=sd.name,
                                      spoiler_settings=sd.spoiler_settings,
                                      precollected=sd.precollected)
            slots[str(sid)] = {
                "name": sd.name,
                "game": sd.game,
                "status": res.status,
                "reason": res.reason,
                "options_source": res.options_source,
                "requirements": res.requirements,   # verified tree OR conservative fallback
            }
        cache = {"seed": seed.seed_name, "version": seed.version_str, "slots": slots}

    sys.stdout = real_stdout
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, indent=2)

    summary = {
        "seed": cache["seed"],
        "version": cache["version"],
        "slots": len(cache["slots"]),
        "verified": sum(1 for s in cache["slots"].values()
                        if s["requirements"].get("verified")),
        "unsupported": sum(1 for s in cache["slots"].values() if s["status"] != "ok"),
        "out": os.path.abspath(args.out),
    }
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
