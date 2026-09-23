"""Provision a version-pinned Archipelago environment for a registered seed.

Runs in PLAIN Python (no Archipelago import needed) so the bot can trigger it on
`/register_seed`. It:

  1. Determines the AP version that generated the seed (from the spoiler header or the
     per-player patch filenames -- no multidata decode needed).
  2. Materializes the matching AP *source* tree at that version, either from a local AP
     git checkout (`git archive <tag>`, fast, no network) or by downloading the GitHub
     release source zip.
  3. Installs the host-supplied apworlds into the tree's custom_worlds/.
  4. Writes a manifest the analyzer/bot reads to know the AP path + version.

Building a Python venv with the right dependencies is intentionally left as an explicit
step (`--build-venv`) because dependency needs vary by host; by default the caller points
the analyzer at an existing interpreter that already has AP's runtime deps.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile

GITHUB_TAG_ZIP = "https://github.com/ArchipelagoMW/Archipelago/archive/refs/tags/{tag}.zip"
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def detect_version(seed_zip: str) -> tuple:
    """Return (major, minor, build) by reading the spoiler header, falling back to the
    version suffix embedded in the per-player patch filenames. No AP import required."""
    with zipfile.ZipFile(seed_zip) as zf:
        names = zf.namelist()
        sp = next((n for n in names if n.endswith("_Spoiler.txt")), None)
        if sp:
            head = zf.read(sp).decode("utf-8-sig", "replace")[:400]
            m = re.search(r"Archipelago Version\s+" + _VERSION_RE.pattern, head)
            if m:
                return tuple(int(g) for g in m.groups())
        # Fallback: patch filenames look like ..._0.6.7.zip / ..._0.6.7.apXYZ
        for n in names:
            m = re.search(r"_(\d+)\.(\d+)\.(\d+)\.[A-Za-z0-9]+$", n)
            if m:
                return tuple(int(g) for g in m.groups())
    raise ValueError(f"Could not determine AP version from {seed_zip}")


def materialize_ap_source(version: tuple, dest: str, *, ap_repo: str | None = None) -> str:
    """Create an AP source tree at `dest` for the given version. Prefer a local git
    checkout (no network); otherwise download the GitHub release source zip."""
    tag = ".".join(str(p) for p in version)
    if os.path.isdir(dest) and os.listdir(dest):
        return dest
    os.makedirs(dest, exist_ok=True)

    if ap_repo and os.path.isdir(os.path.join(ap_repo, ".git")):
        # git archive streams the tree at the tag; extract into dest. No worktree, so
        # the source repo's working tree is untouched.
        archive = subprocess.run(
            ["git", "-C", ap_repo, "archive", "--format=tar", tag],
            check=True, stdout=subprocess.PIPE,
        )
        subprocess.run(["tar", "-x", "-C", dest], check=True, input=archive.stdout)
        return dest

    # Network fallback: download and unwrap the GitHub release source zip.
    url = GITHUB_TAG_ZIP.format(tag=tag)
    tmp_zip = dest.rstrip("/\\") + ".download.zip"
    urllib.request.urlretrieve(url, tmp_zip)
    with zipfile.ZipFile(tmp_zip) as zf:
        zf.extractall(dest)
    os.remove(tmp_zip)
    # GitHub zips wrap everything in Archipelago-<tag>/; flatten it.
    entries = os.listdir(dest)
    if len(entries) == 1 and os.path.isdir(os.path.join(dest, entries[0])):
        inner = os.path.join(dest, entries[0])
        for item in os.listdir(inner):
            shutil.move(os.path.join(inner, item), os.path.join(dest, item))
        os.rmdir(inner)
    return dest


def _fix_separators(path: str) -> None:
    """Rewrite an apworld zipped with Windows backslashes in its entry names ("pkg\\__init__.py").
    Windows tolerates them, but Linux's zipimport can't find the package, so the world silently
    fails to load."""
    with zipfile.ZipFile(path) as zf:
        if not any("\\" in info.orig_filename for info in zf.infolist()):
            return
        entries = [(info.orig_filename.replace("\\", "/"), zf.read(info)) for info in zf.infolist()]
    tmp = path + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as out:
        for name, data in entries:
            out.writestr(name, data)
    os.replace(tmp, path)


def _apworld_module(path: str) -> tuple[str | None, bool]:
    """(top-level package name, has Python source) for an .apworld file."""
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
    tops = {n.split("/")[0] for n in names if "/" in n}
    return (tops.pop() if len(tops) == 1 else None), any(n.endswith(".py") for n in names)


def install_apworlds(apworlds_src: str, ap_path: str) -> tuple[list[str], list[str]]:
    """Copy .apworld files from a directory or zip into <ap_path>/custom_worlds/, the way the
    launcher treats them: an apworld replaces the built-in world of the same name.

    In a source tree the built-in worlds/<name> folder would load first and silently win, so
    it's moved to shadowed_worlds/. An apworld with only compiled bytecode (a copy of the
    launcher's own built-in worlds) can't load under a different Python, so it's skipped and
    the built-in world stays. Returns (installed, skipped)."""
    target = os.path.join(ap_path, "custom_worlds")
    shadowed = os.path.join(ap_path, "shadowed_worlds")
    worlds_dir = os.path.join(ap_path, "worlds")
    os.makedirs(target, exist_ok=True)

    # Start from the stock tree each time, so re-provisioning only reflects the current set.
    if os.path.isdir(shadowed):
        for name in os.listdir(shadowed):
            shutil.move(os.path.join(shadowed, name), os.path.join(worlds_dir, name))

    staged = []
    if os.path.isdir(apworlds_src):
        for fn in os.listdir(apworlds_src):
            if fn.endswith(".apworld"):
                shutil.copy2(os.path.join(apworlds_src, fn), os.path.join(target, fn))
                staged.append(fn)
    elif zipfile.is_zipfile(apworlds_src):
        with zipfile.ZipFile(apworlds_src) as zf:
            for info in zf.infolist():
                if info.filename.endswith(".apworld"):
                    info.filename = os.path.basename(info.filename)
                    zf.extract(info, target)
                    staged.append(info.filename)

    installed, skipped = [], []
    for fn in staged:
        path = os.path.join(target, fn)
        _fix_separators(path)
        module, has_source = _apworld_module(path)
        if not has_source:
            os.remove(path)
            skipped.append(fn)
            continue
        installed.append(fn)
        if module and os.path.isdir(os.path.join(worlds_dir, module)):
            os.makedirs(shadowed, exist_ok=True)
            shutil.move(os.path.join(worlds_dir, module), os.path.join(shadowed, module))
    return installed, skipped


def provision(seed_zip: str, runtime_dir: str, *, apworlds_src: str | None = None,
              ap_repo: str | None = None) -> dict:
    version = detect_version(seed_zip)
    tag = ".".join(str(p) for p in version)
    ap_path = os.path.join(runtime_dir, f"ap-{tag}")
    materialize_ap_source(version, ap_path, ap_repo=ap_repo)
    installed, skipped = install_apworlds(apworlds_src, ap_path) if apworlds_src else ([], [])

    manifest = {
        "seed_zip": os.path.abspath(seed_zip),
        "version": tag,
        "ap_path": os.path.abspath(ap_path),
        "apworlds_installed": sorted(installed),
        "apworlds_skipped_no_source": sorted(skipped),
    }
    os.makedirs(runtime_dir, exist_ok=True)
    with open(os.path.join(runtime_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    return manifest


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Provision a version-pinned AP env for a seed")
    p.add_argument("--seed-zip", required=True)
    p.add_argument("--runtime-dir", required=True, help="Where to materialize the AP tree + manifest")
    p.add_argument("--apworlds", help="Directory or zip of .apworld files used to generate the seed")
    p.add_argument("--ap-repo", help="Path to a local AP git checkout (uses 'git archive' instead of downloading)")
    args = p.parse_args(argv)
    manifest = provision(args.seed_zip, args.runtime_dir, apworlds_src=args.apworlds, ap_repo=args.ap_repo)
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
