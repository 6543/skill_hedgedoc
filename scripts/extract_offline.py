#!/usr/bin/env python3
"""Restore the HedgeDoc markdown and media from an offline package HTML.

Writes <out>/<markdown file> and <out>/media/..., byte-identical to what the
package was built from, ready for:  hedgedoc.py push <out>/<markdown file>

Usage:
  extract_offline.py deck.html [-o DIR] [--force]

Only the Python standard library is needed.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import re
import sys
from pathlib import Path

FORMAT = "hedgedoc-offline"
VERSION = 1
PAYLOAD_ID = "hedgedoc-offline-source"
PAYLOAD = re.compile(r'<script type="application/json" id="' + PAYLOAD_ID + r'">(.*?)</script>', re.DOTALL)
SAFE_MEDIA_PATH = re.compile(r"media/[A-Za-z0-9_-][A-Za-z0-9._-]*")
SAFE_MARKDOWN_NAME = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._ -]*\.md")


class ExtractError(ValueError):
    pass


def read_payload(page: str) -> dict:
    match = PAYLOAD.search(page)
    if not match:
        raise ExtractError("no HedgeDoc offline payload found in this HTML")
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ExtractError(f"payload is not valid JSON: {exc}") from exc
    if payload.get("format") != FORMAT:
        raise ExtractError(f"unknown payload format {payload.get('format')!r}")
    if payload.get("version") != VERSION:
        raise ExtractError(f"unsupported payload version {payload.get('version')!r}, expected {VERSION}")
    return payload


def _checked(data: bytes, expected: str, what: str) -> bytes:
    if hashlib.sha256(data).hexdigest() != expected:
        raise ExtractError(f"checksum mismatch for {what}")
    return data


def files_from_payload(payload: dict) -> dict[str, bytes]:
    """Validated relative path -> content, nothing written yet."""
    try:
        return _files_from_payload(payload)
    except (KeyError, TypeError, AttributeError, binascii.Error, UnicodeEncodeError) as exc:
        raise ExtractError(f"malformed payload: {exc!r}") from exc


def _files_from_payload(payload: dict) -> dict[str, bytes]:
    markdown = payload["markdown"]
    name = markdown["name"]
    if not SAFE_MARKDOWN_NAME.fullmatch(name):
        raise ExtractError(f"unsafe path {name!r}")
    files = {name: _checked(markdown["text"].encode("utf-8"), markdown["sha256"], name)}
    for item in payload["media"]:
        path = item["path"]
        if not SAFE_MEDIA_PATH.fullmatch(path):
            raise ExtractError(f"unsafe path {path!r}")
        files[path] = _checked(base64.b64decode(item["base64"], validate=True), item["sha256"], path)
    return files


def extract(html_path: Path, out_dir: Path, force: bool = False) -> list[Path]:
    files = files_from_payload(read_payload(Path(html_path).read_text(encoding="utf-8")))
    targets = {Path(out_dir) / rel: data for rel, data in files.items()}
    existing = [str(p) for p in targets if p.exists()]
    if existing and not force:
        raise ExtractError(f"{', '.join(existing)} exists, use --force to overwrite")
    for path, data in targets.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return list(targets)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("html", type=Path, help="offline package HTML")
    parser.add_argument("-o", "--output", type=Path, help="target directory (default: <html name>-source)")
    parser.add_argument("--force", action="store_true", help="overwrite existing files")
    args = parser.parse_args(argv)
    out = args.output or args.html.with_name(f"{args.html.stem}-source")
    try:
        written = extract(args.html, out, args.force)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"written": [str(p) for p in written], "markdown": str(written[0])}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
