#!/usr/bin/env python3
"""Fetch PAL DOS assets into a local, gitignored directory.

The upstream repository does not declare a clear asset license. This script is
for local development only and intentionally keeps assets outside version
control.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path


REPO = "https://github.com/handoing/Legend-of-Sword-and-Fairy.git"
REQUIRED = ["DATA.MKF", "MAP.MKF", "GOP.MKF", "SSS.MKF", "MGO.MKF", "WORD.DAT"]


def has_required_assets(path: Path) -> bool:
    return all((path / name).exists() for name in REQUIRED)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default="data/pal", help="asset output directory")
    parser.add_argument("--repo", default=REPO, help="source git repository")
    args = parser.parse_args()

    target = Path(args.target).resolve()
    if has_required_assets(target):
        print(f"assets already present: {target}")
        return 0

    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pal-assets-") as tmp:
        clone_dir = Path(tmp) / "repo"
        subprocess.check_call(["git", "clone", "--depth", "1", args.repo, str(clone_dir)])
        source = clone_dir / "pal"
        if not has_required_assets(source):
            missing = [name for name in REQUIRED if not (source / name).exists()]
            raise SystemExit(f"asset repo is missing required files: {', '.join(missing)}")

        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)

    print(f"assets copied to {target}")
    print("note: assets are gitignored and should not be redistributed from this repo")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

