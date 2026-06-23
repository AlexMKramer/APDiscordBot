"""Command-line / JSON entrypoint for the go-mode analyzer.

Runs inside the version-pinned AP environment (the bot invokes it as a subprocess,
one fork per request). Reads a seed + slot + inventory, prints a JSON result.

Examples:
  # analyze one slot for a given inventory
  python cli.py --ap-path <AP> --seed-zip TEST-SEED.zip --slot Alex_Crab \
      --inventory '{"Katana": 1, "Hammer": 1}'

  # survey every slot with empty inventory (validation / coverage)
  python cli.py --ap-path <AP> --seed-zip TEST-SEED.zip --survey
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import os
import sys


@contextlib.contextmanager
def _quiet_stdout():
    """Redirect stdout to a throwaway buffer. Importing Archipelago and building worlds
    prints chatter ("Copied vendor...", per-world debug) straight to stdout; we must keep
    the real stdout clean so the only thing the caller parses is our JSON."""
    real = sys.stdout
    sys.stdout = io.StringIO()
    try:
        yield real
    finally:
        sys.stdout = real


def _bootstrap(ap_path: str) -> None:
    # Make sibling modules importable regardless of cwd, and put the AP source first.
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    if ap_path:
        sys.path.insert(0, os.path.abspath(ap_path))
    # Silence AP's noisy world-loading logs so stdout stays clean JSON.
    logging.disable(logging.CRITICAL)


def _resolve_slot(seed, selector: str):
    """Selector may be a slot number or a slot name."""
    if selector is None:
        return None
    if selector.isdigit() and int(selector) in seed.slots:
        return seed.slots[int(selector)]
    return seed.find_slot(selector)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Archipelago go-mode analyzer")
    parser.add_argument("--ap-path", required=True, help="Path to the version-matched AP source tree")
    parser.add_argument("--seed-zip", required=True, help="Path to the generated AP_<seed>.zip")
    parser.add_argument("--slot", help="Slot number or slot name to analyze")
    parser.add_argument("--inventory", default="{}",
                        help='Inventory as JSON {"Item Name": count}, or @path to a JSON file')
    parser.add_argument("--survey", action="store_true",
                        help="Analyze every slot (empty inventory) and print a summary")
    parser.add_argument("--go-mode-batch",
                        help='Fast go-mode check for many slots at once: JSON (or @path) '
                             '{slot: inventory} -> {go_mode: {slot: {status, in_go_mode}}}')
    args = parser.parse_args(argv)

    # Do all AP work with stdout muted, build the result, then print clean JSON.
    rc = 0
    with _quiet_stdout():
        _bootstrap(args.ap_path)
        import seed_data
        import engine

        seed = seed_data.load_seed(args.seed_zip)

        if args.go_mode_batch:
            spec = args.go_mode_batch
            if spec.startswith("@"):
                with open(spec[1:], "r", encoding="utf-8") as fh:
                    requested = json.load(fh)
            else:
                requested = json.loads(spec)
            go_mode = {}
            for selector, inv in requested.items():
                sd = _resolve_slot(seed, selector)
                if sd is None:
                    go_mode[selector] = {"status": "error", "reason": "slot not found"}
                    continue
                try:
                    res = engine.analyze_slot(sd.game, sd.options, inv or {}, slot=sd.slot,
                                              name=sd.name, spoiler_settings=sd.spoiler_settings,
                                              precollected=sd.precollected, fast=True)
                    go_mode[selector] = {"status": res.status, "in_go_mode": res.in_go_mode,
                                         "reason": res.reason}
                except Exception as exc:  # noqa: BLE001 -- one bad slot must not abort the batch
                    go_mode[selector] = {"status": "error", "reason": f"{type(exc).__name__}: {exc}"}
            output = {"go_mode": go_mode}
        elif args.survey:
            rows = []
            for sid, sd in seed.slots.items():
                try:
                    res = engine.analyze_slot(sd.game, sd.options, {}, slot=sid, name=sd.name,
                                              spoiler_settings=sd.spoiler_settings,
                                              precollected=sd.precollected)
                    rows.append(res.to_dict())
                except Exception as exc:  # noqa: BLE001 -- never let one slot abort the survey
                    rows.append({"slot": sid, "name": sd.name, "game": sd.game,
                                 "status": "error", "reason": f"{type(exc).__name__}: {exc}"})
            ok = sum(1 for r in rows if r["status"] == "ok")
            output = {"seed": seed.seed_name, "version": seed.version_str,
                      "slots": len(rows), "analyzable": ok, "results": rows}
        else:
            sd = _resolve_slot(seed, args.slot)
            if sd is None:
                output = {"status": "error", "reason": f"Slot {args.slot!r} not found in seed"}
                rc = 2
            else:
                inv_arg = args.inventory
                if inv_arg.startswith("@"):
                    with open(inv_arg[1:], "r", encoding="utf-8") as fh:
                        inventory = json.load(fh)
                else:
                    inventory = json.loads(inv_arg)
                res = engine.analyze_slot(sd.game, sd.options, inventory, slot=sd.slot, name=sd.name,
                                          spoiler_settings=sd.spoiler_settings,
                                          precollected=sd.precollected)
                output = res.to_dict()
                output["seed"] = seed.seed_name
                output["version"] = seed.version_str
                if res.status == "ok" and not res.in_go_mode:
                    import requirements
                    output["requirements_text"] = requirements.render_requirements(res.requirements)

    print(json.dumps(output, indent=2))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
