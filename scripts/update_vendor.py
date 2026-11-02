#!/usr/bin/env python3
"""Refresh scripts/vendor/: pure-Python wheels for offline use + MANIFEST.json (sha256).

Needs network + pip. Run from anywhere: python3 scripts/update_vendor.py
Wheels are py3-none-any only, resolved for Python 3.9+, so they work on any OS/arch.
"""

import glob
import hashlib
import json
import os
import subprocess
import sys

REQUIREMENTS = ["requests", "python-socketio[client]"]
VENDOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")


def main() -> int:
    os.makedirs(VENDOR, exist_ok=True)
    for old in glob.glob(os.path.join(VENDOR, "*.whl")):
        os.remove(old)
    subprocess.check_call(
        [
            sys.executable,
            "-m",
            "pip",
            "download",
            "--quiet",
            "--only-binary=:all:",
            "--platform",
            "any",
            "--implementation",
            "py",
            "--python-version",
            "3.9",
            "-d",
            VENDOR,
            *REQUIREMENTS,
        ]
    )
    manifest = {}
    for whl in sorted(glob.glob(os.path.join(VENDOR, "*.whl"))):
        if not whl.endswith("-none-any.whl"):
            sys.exit(f"not a pure-Python wheel: {whl}")
        with open(whl, "rb") as fh:
            manifest[os.path.basename(whl)] = hashlib.sha256(fh.read()).hexdigest()
    with open(os.path.join(VENDOR, "MANIFEST.json"), "w") as fh:
        json.dump(manifest, fh, indent=2)
        fh.write("\n")
    print(f"{len(manifest)} wheels in {VENDOR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
