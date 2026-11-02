"""Offline export: a HedgeDoc slide deck as one self-contained HTML file.

The HTML presents offline with the bundled reveal.js, styled like HedgeDoc 1.x
slide mode, and embeds the markdown and every referenced media file unchanged,
so extract_offline.py (stdlib only) restores them for `hedgedoc.py push`.

Input is the layout `hedgedoc.py pull --localize` writes: a markdown file with
media/<file> links and a media/ folder next to it.
"""

from __future__ import annotations

import base64
import hashlib
import html
import io
import json
import lzma
import re
import tarfile
import zipfile
from pathlib import Path

from extract_offline import FORMAT, PAYLOAD_ID, VERSION

HERE = Path(__file__).resolve().parent
RUNTIME = HERE / "offline_runtime.js"
PARITY_CSS = HERE / "offline_parity.css"
EXTRACTOR = HERE / "extract_offline.py"
ASSETS_MANIFEST = HERE / "vendor" / "ASSETS.json"

MEDIA_TYPES = {
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".webp": "image/webp",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
}
# Same layout as `hedgedoc.py pull --localize`: media/<file>.<ext>
MEDIA_REF = re.compile(
    r"(?<![\w./-])media/[A-Za-z0-9_-][A-Za-z0-9._-]*\.(?:svg|png|webp|jpe?g|gif)(?![\w.-])",
    re.IGNORECASE,
)
MD_IMAGE = re.compile(r"!\[[^\]\n]*\]\(\s*<?([^\s)>]+)")
HTML_IMAGE = re.compile(r"""<img\b[^>]*?\ssrc\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
FENCED_CODE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,}).*?^[ \t]{0,3}\1[ \t]*$", re.MULTILINE | re.DOTALL)
FRONT_MATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n(?:---|\.\.\.)[ \t]*(?:\r?\n|\Z)", re.DOTALL)
THEME_NAME = re.compile(r"[a-z][a-z0-9-]*")

# HedgeDoc 1.x public/js/slide.js: defaults and the slideOptions keys it passes on.
HEDGEDOC_DEFAULTS = {
    "controls": True,
    "progress": True,
    "slideNumber": True,
    "history": True,
    "center": True,
    "transition": "none",
}
HEDGEDOC_SLIDE_OPTIONS = frozenset(
    """autoPlayMedia autoSlide autoSlideStoppable backgroundTransition center controls
    controlsBackArrows controlsLayout controlsTutorial defaultTiming display embedded
    fragmentInURL fragments hash height help hideAddressBar hideCursorTime
    hideInactiveCursor history keyboard loop margin maxScale minScale
    minimumTimePerSlide mobileViewDistance mouseWheel navigationMode overview
    parallaxBackgroundHorizontal parallaxBackgroundImage parallaxBackgroundSize
    parallaxBackgroundVertical preloadIframes previewLinks progress rtl showNotes
    shuffle slideNumber totalTime touch transition transitionSpeed viewDistance width""".split()
)
DEFAULT_THEME = "black"  # HedgeDoc 1.x slide.ejs falls back to black.css


class OfflineError(ValueError):
    pass


class RevealSource:
    """reveal.js dist files from an unpacked dist/ directory or a .tar.xz archive."""

    def __init__(self, path: Path | str, sha256: str | None = None):
        self.path = Path(path)
        self.sha256 = sha256
        self._files: dict[str, bytes] | None = None

    def _archive(self) -> dict[str, bytes]:
        if self._files is None:
            data = self.path.read_bytes()
            if self.sha256 and hashlib.sha256(data).hexdigest() != self.sha256:
                raise OfflineError(f"{self.path.name} fails its sha256 check; reinstall the skill")
            with tarfile.open(fileobj=io.BytesIO(lzma.decompress(data))) as tar:
                self._files = {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}
        return self._files

    def exists(self, rel: str) -> bool:
        if self.path.is_dir():
            return (self.path / rel).is_file()
        return rel in self._archive()

    def read(self, rel: str) -> str:
        if self.path.is_dir():
            target = self.path / rel
            if not target.is_file():
                raise OfflineError(f"reveal.js file {rel} missing in {self.path}")
            return target.read_text(encoding="utf-8")
        files = self._archive()
        if rel not in files:
            raise OfflineError(f"reveal.js file {rel} missing in {self.path.name}")
        return files[rel].decode("utf-8")


def bundled_reveal() -> RevealSource:
    entry = json.loads(ASSETS_MANIFEST.read_text())["reveal"]
    return RevealSource(ASSETS_MANIFEST.parent / entry["file"], sha256=entry["sha256"])


def script_safe_json(data: object) -> str:
    """JSON that can sit inside <script> without ending or confusing it."""
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    for char, escape in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"), (" ", "\\u2028"), (" ", "\\u2029")):
        text = text.replace(char, escape)
    return text


def inline_js(source: str) -> str:
    return re.sub(r"</(script)", r"<\\/\1", source, flags=re.IGNORECASE).replace("<!--", "<\\!--")


def inline_css(source: str) -> str:
    return re.sub(r"</(style)", r"<\\/\1", source, flags=re.IGNORECASE)


def _yaml_load(text: str):
    """PyYAML when installed, else the bundled pure-Python ruamel.yaml (see ensure_deps)."""
    try:
        import yaml

        loader, error = yaml.safe_load, yaml.YAMLError
    except ImportError:
        from ruamel.yaml import YAML
        from ruamel.yaml.error import YAMLError

        loader, error = YAML(typ="safe", pure=True).load, YAMLError
    try:
        return loader(text)
    except error as exc:
        raise OfflineError(f"front matter is not valid YAML: {exc}") from exc


def parse_front_matter(markdown: str) -> dict:
    match = FRONT_MATTER.match(markdown)
    if not match:
        return {}
    meta = _yaml_load(match.group(1))
    return meta if isinstance(meta, dict) else {}


def render_settings(meta: dict) -> dict:
    slide_options = meta.get("slideOptions")
    slide_options = slide_options if isinstance(slide_options, dict) else {}
    reveal = dict(HEDGEDOC_DEFAULTS)
    reveal.update({k: v for k, v in slide_options.items() if k in HEDGEDOC_SLIDE_OPTIONS and v is not None})
    reveal["rtl"] = meta.get("dir") == "rtl"
    return {"reveal": reveal, "breaks": meta.get("breaks") is not False}


def collect_media(markdown: str, base_dir: Path) -> list[dict]:
    media = []
    for ref in sorted(set(MEDIA_REF.findall(markdown))):
        path = base_dir / ref
        if not path.is_file():
            raise OfflineError(f"{ref} is referenced in the markdown but missing in {base_dir}")
        data = path.read_bytes()
        media.append(
            {
                "path": ref,
                "type": MEDIA_TYPES[path.suffix.lower()],
                "sha256": hashlib.sha256(data).hexdigest(),
                "base64": base64.b64encode(data).decode("ascii"),
            }
        )
    return media


def unbundled_images(markdown: str) -> list[str]:
    """Image targets that would not be in the package (not media/<file>, not data:), in document order."""
    text = FENCED_CODE.sub("", markdown)
    found = sorted((m.start(), m.group(1)) for rx in (MD_IMAGE, HTML_IMAGE) for m in rx.finditer(text))
    targets = [t for _, t in found if not MEDIA_REF.fullmatch(t) and not t.startswith("data:")]
    return list(dict.fromkeys(targets))


def load_assets(reveal: RevealSource, theme: str) -> tuple[str, str]:
    if not THEME_NAME.fullmatch(theme) or not reveal.exists(f"theme/{theme}.css"):
        raise OfflineError(f"reveal.js theme '{theme}' not found")
    # HedgeDoc 1.x uses zenburn for code; the parity CSS goes last so it overrides the theme
    css = [reveal.read(f) for f in ("reveal.css", f"theme/{theme}.css", "plugin/highlight/zenburn.css")]
    css.append(PARITY_CSS.read_text(encoding="utf-8"))
    js = [reveal.read(f) for f in ("reveal.js", "plugin/markdown.js", "plugin/highlight.js", "plugin/notes.js")]
    js.append(RUNTIME.read_text(encoding="utf-8"))
    return "\n".join(map(inline_css, css)), "\n".join(map(inline_js, js))


def render_html(payload: dict, css: str, js: str, title: str, lang: str | None = None) -> str:
    lang_attr = f' lang="{html.escape(lang)}"' if lang else ""
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="generator" content="{FORMAT} {VERSION}">
<title>{html.escape(title)}</title>
<style>
{css}
</style>
</head>
<body>
<div class="reveal"{lang_attr}><div class="slides"></div></div>
<!--
  {FORMAT} package: the original HedgeDoc markdown and media are stored unchanged
  in the JSON below (markdown.text, media[].base64). Restore them with
  extract_offline.py, then upload with: hedgedoc.py push <dir>/<markdown file>
-->
<script type="application/json" id="{PAYLOAD_ID}">{script_safe_json(payload)}</script>
<script>
{js}
</script>
<script>HedgedocOffline.boot();</script>
</body>
</html>
"""


def build_html(markdown_path: Path, reveal: RevealSource, source_url: str | None = None) -> str:
    markdown_path = Path(markdown_path)
    raw = markdown_path.read_bytes()
    try:
        markdown = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OfflineError(f"{markdown_path} is not UTF-8: {exc}") from exc

    outside = unbundled_images(markdown)
    if outside:
        raise OfflineError(
            "images outside media/ would not work offline: "
            + ", ".join(outside)
            + " (for a pad, export-offline fetches them; pass --external for non-pad URLs)"
        )
    meta = parse_front_matter(markdown)
    slide_options = meta.get("slideOptions") if isinstance(meta.get("slideOptions"), dict) else {}
    css, js = load_assets(reveal, str(slide_options.get("theme") or DEFAULT_THEME))

    payload = {
        "format": FORMAT,
        "version": VERSION,
        "markdown": {"name": markdown_path.name, "sha256": hashlib.sha256(raw).hexdigest(), "text": markdown},
        "media": collect_media(markdown, markdown_path.parent),
        "render": render_settings(meta),
    }
    if source_url:
        payload["source"] = {"url": source_url}

    title = str(meta.get("title") or markdown_path.stem)
    lang = meta.get("lang") if isinstance(meta.get("lang"), str) else None
    return render_html(payload, css, js, title, lang)


def _readme(html_name: str, title: str, source_url: str | None) -> str:
    source = f"Source pad: {source_url}\n\n" if source_url else ""
    return f"""# {title}: offline package

{source}## Present

Open `{html_name}` in a browser. No network is needed: reveal.js, fonts, code
highlighting and all images are inside the file. Keys as in HedgeDoc: arrows,
`S` speaker notes, `F` fullscreen, `O` or `Esc` overview, links like `#/3/1`.

## Get the source back

The HTML stores the original HedgeDoc markdown and images unchanged.

```sh
python3 extract_offline.py {html_name} -o deck
```

writes `deck/<name>.md` and `deck/media/*`, byte-identical (SHA-256 checked),
using only the Python standard library. Upload as a new pad with the hedgedoc
skill: `hedgedoc.py --server <pad server> push deck/<name>.md`.

Without Python: the data is JSON in the `<script id="hedgedoc-offline-source">`
tag (`markdown.text`, `media[].base64`).
"""


def export(markdown_path: Path, out_path: Path, reveal: RevealSource, source_url: str | None = None) -> Path:
    """Write out_path: .html = the page alone, .zip = page + extractor + README."""
    out_path = Path(out_path)
    suffix = out_path.suffix.lower()
    if suffix not in (".html", ".zip"):
        raise OfflineError(f"output must end in .html or .zip, got {out_path.name}")
    page = build_html(markdown_path, reveal, source_url)
    if suffix == ".html":
        out_path.write_text(page, encoding="utf-8")
        return out_path
    stem = out_path.stem
    title = html.unescape(re.search(r"<title>(.*?)</title>", page).group(1))
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr(f"{stem}/{stem}.html", page)
        z.writestr(f"{stem}/extract_offline.py", EXTRACTOR.read_text(encoding="utf-8"))
        z.writestr(f"{stem}/README.md", _readme(f"{stem}.html", title, source_url))
    return out_path
