#!/usr/bin/env python3
"""Refresh scripts/vendor/: everything the CLI needs offline.

* pure-Python wheels + MANIFEST.json (sha256), for the HedgeDoc client and
  front matter parsing (ruamel.yaml, only used when PyYAML is not installed)
* reveal.js for `export-offline`: the dist files it needs, repacked as a
  reproducible reveal.js-<version>.tar.xz, with sha256 in ASSETS.json

Needs network + pip. Run from anywhere: python3 scripts/update_vendor.py
Wheels are py3-none-any only, resolved for Python 3.9+, so they work on any OS/arch.
"""

import base64
import glob
import hashlib
import io
import json
import lzma
import os
import subprocess
import sys
import tarfile
import urllib.request

REQUIREMENTS = ["requests", "python-socketio[client]", "ruamel.yaml"]
VENDOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")

REVEAL_VERSION = "6.0.2"
REVEAL_FILES = [
    "LICENSE",
    "dist/reveal.js",
    "dist/reveal.css",
    "dist/plugin/markdown.js",
    "dist/plugin/highlight.js",
    "dist/plugin/notes.js",
    "dist/plugin/highlight/zenburn.css",
]  # plus every dist/theme/*.css


def update_wheels() -> int:
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
    return len(manifest)


def _get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as resp:  # noqa: S310 (fixed https registry URL)
        return resp.read()


def update_reveal() -> str:
    meta = json.loads(_get(f"https://registry.npmjs.org/reveal.js/{REVEAL_VERSION}"))
    tgz = _get(meta["dist"]["tarball"])
    algo, _, expected = meta["dist"]["integrity"].partition("-")
    if base64.b64encode(hashlib.new(algo, tgz).digest()).decode() != expected:
        sys.exit("reveal.js tarball does not match the registry's integrity hash")

    wanted = {"package/" + f for f in REVEAL_FILES}
    files = {}
    with tarfile.open(fileobj=io.BytesIO(tgz), mode="r:gz") as src:
        for member in src.getmembers():
            name = member.name
            is_theme = name.startswith("package/dist/theme/") and name.endswith(".css") and name.count("/") == 3
            if member.isfile() and (name in wanted or is_theme):
                files[name[len("package/") :].removeprefix("dist/")] = src.extractfile(member).read()
    missing = {f.removeprefix("dist/") for f in REVEAL_FILES} - set(files)
    if missing:
        sys.exit(f"reveal.js {REVEAL_VERSION} lacks {sorted(missing)}")

    # reproducible archive: sorted names, zero mtime/owner, so reruns give the same sha256
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as out:
        for name in sorted(files):
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(files[name]), 0o644, 0
            out.addfile(info, io.BytesIO(files[name]))
    archive_name = f"reveal.js-{REVEAL_VERSION}.tar.xz"
    data = lzma.compress(raw.getvalue(), preset=9 | lzma.PRESET_EXTREME)
    for old in glob.glob(os.path.join(VENDOR, "reveal.js-*.tar.xz")):
        os.remove(old)
    with open(os.path.join(VENDOR, archive_name), "wb") as fh:
        fh.write(data)
    with open(os.path.join(VENDOR, "ASSETS.json"), "w") as fh:
        json.dump(
            {"reveal": {"file": archive_name, "version": REVEAL_VERSION, "sha256": hashlib.sha256(data).hexdigest()}},
            fh,
            indent=2,
        )
        fh.write("\n")
    return archive_name


def main() -> int:
    os.makedirs(VENDOR, exist_ok=True)
    wheels = update_wheels()
    archive = update_reveal()
    print(f"{wheels} wheels and {archive} in {VENDOR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
