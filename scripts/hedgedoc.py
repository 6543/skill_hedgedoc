#!/usr/bin/env python3
"""hedgedoc.py: talk to a HedgeDoc 1.x server (create, read, edit, media, lint, offline export).

Deps: requests (all commands), python-socketio[client] (update/append/push to existing note),
      PyYAML or ruamel.yaml (export-offline). Bundled wheels in vendor/ cover all of them.
    pip install requests "python-socketio[client]"

Server:  --server URL  or env HEDGEDOC_URL  (not needed when a full note URL is given)
Login:   --cookie VALUE or env HEDGEDOC_COOKIE (value of the connect.sid cookie);
         get one with the `login` command. Anonymous works when the instance allows it.

Run `hedgedoc.py -h` or `hedgedoc.py <command> -h` for usage.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
import tempfile
import threading
import time
from urllib.parse import unquote, urlsplit

DOC_MAX_LENGTH = 100000  # 1.x default documentMaxLength (JS string length)
SESSION_COOKIE = "connect.sid"
EXT_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".jpe": "image/jpeg",
    ".jfif": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mp3": "audio/mpeg",
    ".zip": "application/zip",
}
RESERVED_PATHS = {
    "",
    "new",
    "uploads",
    "status",
    "config",
    "me",
    "history",
    "login",
    "logout",
    "register",
    "auth",
    "socket.io",
    "build",
    "js",
    "css",
    "vendor",
    "docs",
    "_health",
    "metrics",
    "_link",
    "403",
    "404",
    "500",
    "icons",
    "banner",
    "favicon.ico",
}
NOTE_ACTIONS = {"download", "publish", "pretty", "slide", "info", "revision", "gist", "edit"}


# --------------------------------------------------------------------------- pure helpers


def utf16_len(s: str) -> int:
    """Length as JavaScript sees it (UTF-16 code units). HedgeDoc OT counts in these."""
    return len(s.encode("utf-16-le")) // 2


def _units(s: str) -> list[int]:
    b = s.encode("utf-16-le", "surrogatepass")
    return [b[i] | (b[i + 1] << 8) for i in range(0, len(b), 2)]


def _text(units: list[int]) -> str:
    return b"".join(u.to_bytes(2, "little") for u in units).decode("utf-16-le", "surrogatepass")


def _is_high(u: int) -> bool:
    return 0xD800 <= u <= 0xDBFF


def _is_low(u: int) -> bool:
    return 0xDC00 <= u <= 0xDFFF


def make_text_operation(old: str, new: str) -> list:
    """ot.js TextOperation JSON turning old into new: int>0 retain, str insert, int<0 delete.

    Uses common prefix/suffix, so unchanged text keeps its authorship. Never splits a
    surrogate pair. Base length == utf16_len(old) as the server requires.
    """
    if old == new:
        return []
    a, b = _units(old), _units(new)
    pre = 0
    while pre < len(a) and pre < len(b) and a[pre] == b[pre]:
        pre += 1
    if pre and _is_high(a[pre - 1]):
        pre -= 1
    suf = 0
    while suf < len(a) - pre and suf < len(b) - pre and a[-1 - suf] == b[-1 - suf]:
        suf += 1
    if suf and _is_low(a[len(a) - suf]):
        suf -= 1
    ops: list = []
    if pre:
        ops.append(pre)
    ins = b[pre : len(b) - suf]
    dele = len(a) - suf - pre
    if ins:
        ops.append(_text(ins))
    if dele:
        ops.append(-dele)
    if suf:
        ops.append(suf)
    return ops


def apply_text_operation(doc: str, ops: list) -> str:
    """Apply ot.js TextOperation JSON to doc (UTF-16 semantics). Raises on length mismatch."""
    src, out, i = _units(doc), [], 0
    for op in ops:
        if isinstance(op, str):
            out.extend(_units(op))
        elif op > 0:
            out.extend(src[i : i + op])
            i += op
        else:
            i += -op
    if i != len(src):
        raise ValueError(f"operation base length {i} != document length {len(src)}")
    return _text(out)


def normalize_server(server: str) -> str:
    server = server.strip()
    if not re.match(r"^https?://", server):
        server = "https://" + server
    return server.rstrip("/")


def parse_note_ref(ref: str, server: str | None = None) -> tuple[str, str]:
    """Return (server, note_id) from a note URL (edit/view/publish/slide/action) or bare id."""
    ref = ref.strip()
    if not re.match(r"^https?://", ref):
        if not server:
            raise ValueError(f"bare note id {ref!r} needs --server or HEDGEDOC_URL")
        if "/" in ref or not ref:
            raise ValueError(f"not a note id: {ref!r}")
        return normalize_server(server), ref
    parts = urlsplit(ref)
    origin = f"{parts.scheme}://{parts.netloc}"
    path = parts.path
    base = origin
    if server:
        srv = normalize_server(server)
        if ref.startswith(srv + "/") or ref == srv:
            base = srv
            path = ref[len(srv) :].split("?")[0].split("#")[0]
    segs = [unquote(s) for s in path.split("/") if s]
    if segs and segs[0] in ("s", "p") and len(segs) >= 2:
        segs = segs[1:]
    if len(segs) == 2 and segs[1] in NOTE_ACTIONS:
        segs = segs[:1]
    elif len(segs) == 3 and segs[1] == "revision":
        segs = segs[:1]
    if len(segs) != 1 or segs[0] in RESERVED_PATHS:
        raise ValueError(f"not a note URL: {ref}")
    return base, segs[0]


def is_forbidden_redirect(location: str, server: str) -> bool:
    """1.x answers forbidden (anon) with 302 to serverURL + '/' instead of 403."""
    loc = (location or "").rstrip("/")
    return loc in ("", normalize_server(server))


_IMG_MD = re.compile(r"!\[[^\]]*\]\(\s*(<[^>]+>|[^)\s]+)")
_IMG_HTML = re.compile(r"<img\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.I)
_REFDEF = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*(<[^>]+>|\S+)", re.M)
_URL = re.compile(r"https?://[^\s)\"'<>\]]+")


def _image_targets(md: str) -> list[str]:
    found = []
    for rx in (_IMG_MD, _IMG_HTML):
        for m in rx.finditer(md):
            found.append((m.start(), m.group(1)))
    return [t for _, t in sorted(found)]


def _unbracket(t: str) -> str:
    return t[1:-1] if t.startswith("<") and t.endswith(">") else t


def find_server_media(md: str, server: str, include_external: bool = False) -> list[str]:
    """Uploads hosted on this server (any reference), plus external image URLs if asked."""
    uploads = normalize_server(server) + "/uploads/"
    hits = [(m.start(), m.group(0)) for m in _URL.finditer(md) if m.group(0).startswith(uploads)]
    if include_external:
        for m in list(_IMG_MD.finditer(md)) + list(_IMG_HTML.finditer(md)):
            t = _unbracket(m.group(1))
            if re.match(r"^https?://", t):
                hits.append((m.start(1), t))
    seen, out = set(), []
    for _, u in sorted(hits):
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def find_local_media(md: str, base_dir: str) -> dict[str, str]:
    """Image targets that are existing local files: {target as written: absolute path}."""
    out = {}
    for t in _image_targets(md):
        raw = _unbracket(t)
        if re.match(r"^[a-z][a-z0-9+.-]*:", raw, re.I) or raw.startswith("#"):
            continue
        path = unquote(raw)
        full = path if os.path.isabs(path) else os.path.join(base_dir, path)
        if os.path.isfile(full):
            out[raw] = os.path.abspath(full)
    return out


def rewrite_links(md: str, mapping: dict[str, str]) -> str:
    """Replace link/image targets (markdown, HTML src/href, reference defs). Not plain text."""
    for old, new in mapping.items():
        e = re.escape(old)
        md = re.sub(r"(\]\(\s*<?)" + e + r"(?=[>\s)])", lambda m, new=new: m.group(1) + new, md)
        md = re.sub(r"((?:src|href)\s*=\s*[\"'])" + e + r"(?=[\"'])", lambda m, new=new: m.group(1) + new, md)
        md = re.sub(
            r"(^\s{0,3}\[[^\]]+\]:\s*<?)" + e + r"(?=[>\s]|$)", lambda m, new=new: m.group(1) + new, md, flags=re.M
        )
    return md


def upload_problem(path: str, allowed: list[str]) -> str | None:
    """None if the server should accept this file (1.x checks extension == magic mime)."""
    ext = os.path.splitext(path)[1].lower()
    mime = EXT_MIME.get(ext)
    if not mime:
        return f"unknown extension {ext or '(none)'}; server allows {', '.join(allowed)}"
    if mime not in allowed:
        return f"{os.path.basename(path)} is {mime}; server allows only {', '.join(allowed)}"
    return None


def split_frontmatter(md: str) -> tuple[str | None, str]:
    m = re.match(r"^---[ \t]*\n(.*?)\n(?:---|\.\.\.)[ \t]*(?:\n|$)", md, re.S)
    if not m:
        return None, md
    return m.group(1), md[m.end() :]


def _body_lines_outside_fences(body: str):
    fence = None
    for i, line in enumerate(body.split("\n")):
        m = re.match(r"^\s*(```+|~~~+)", line)
        if m:
            tok = m.group(1)[0] * 3
            fence = None if fence == tok else (fence or tok)
            continue
        if fence is None:
            yield i, line


def lint_markdown(md: str) -> list[str]:
    """Warnings ("code: message") for HedgeDoc-specific formatting pitfalls."""
    warns = []
    if utf16_len(md) > DOC_MAX_LENGTH:
        warns.append(f"too-long: {utf16_len(md)} > {DOC_MAX_LENGTH} chars, server rejects it (413)")
    meta, body = split_frontmatter(md)
    if meta is None and re.match(r"^\s+---[ \t]*\n[\w-]+:", md):
        warns.append("frontmatter-position: YAML metadata must start on the very first line")
    if meta is not None:
        for ln in meta.split("\n"):
            if ln.startswith("\t") or re.match(r"^( | {3}|  {3})[\w-]", ln):
                warns.append("yaml-indent: use exactly two spaces per indent level in YAML metadata")
                break
    is_slide = bool(meta and re.search(r"^type:\s*slide\s*$", meta, re.M))
    lines = body.split("\n")
    seps = 0
    for i, line in _body_lines_outside_fences(body):
        if line.strip() in ("---", "----") and line.strip() == line.rstrip():
            prev = lines[i - 1] if i > 0 else ""
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            if prev.strip() == "" and nxt.strip() == "":
                seps += 1
            elif is_slide:
                warns.append(
                    f"slide-separator: line {i + 1} '{line.strip()}' needs an empty line before and after "
                    "(otherwise it is a setext heading/rule, not a slide break)"
                )
    if seps >= 2 and not is_slide:
        warns.append(
            "missing-type-slide: looks like a deck; add 'type: slide' to YAML so the editor previews slides "
            "(/p/ slide view works either way)"
        )
    return warns


def parse_config_js(js: str) -> dict:
    """Parse the /config script (window.x = ...) into a dict."""
    out = {}
    for m in re.finditer(r"^window\.(\w+)\s*=\s*(.+?)\s*;?\s*$", js, re.M):
        k, v = m.group(1), m.group(2)
        if len(v) >= 2 and v[0] == v[-1] == "'":
            out[k] = v[1:-1]
        else:
            try:
                out[k] = json.loads(v)
            except ValueError:
                out[k] = v
    return out


# --------------------------------------------------------------------------- HTTP client


class HedgeDocError(RuntimeError):
    pass


VENDOR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")
PIP_HINT = 'pip install requests "python-socketio[client]"'


def _importable(name: str) -> bool:
    try:
        __import__(name)
        return True
    except ImportError:
        return False


def _cache_roots(cache_root: str | None) -> list[str]:
    if cache_root:
        return [cache_root]
    import tempfile

    roots = [os.environ.get("HEDGEDOC_VENDOR_CACHE"), os.path.join(os.path.expanduser("~"), ".cache", "hedgedoc-skill")]
    return [r for r in roots if r] + [os.path.join(tempfile.gettempdir(), "hedgedoc-skill")]


def _extract_wheels(wheels: list[str], key: str, roots: list[str]) -> str | None:
    import shutil
    import zipfile

    for root in roots:
        target = os.path.join(root, "vendor-" + key)
        if os.path.isfile(os.path.join(target, ".complete")):
            return target
        try:
            os.makedirs(root, exist_ok=True)
            staging = f"{target}.tmp{os.getpid()}"
            shutil.rmtree(staging, ignore_errors=True)
            for whl in wheels:
                with zipfile.ZipFile(whl) as z:
                    z.extractall(staging)
            open(os.path.join(staging, ".complete"), "w").close()
            try:
                os.replace(staging, target)
            except OSError:  # another process won the race
                shutil.rmtree(staging, ignore_errors=True)
            if os.path.isfile(os.path.join(target, ".complete")):
                return target
        except OSError:
            continue
    return None


def ensure_deps(modules=("requests",), vendor_dir: str = VENDOR_DIR, cache_root: str | None = None) -> str:
    """Make `modules` importable. Installed packages win ("installed"); otherwise the bundled
    pure-Python wheels in vendor_dir are verified (sha256 in MANIFEST.json), extracted to a
    cache dir and put on sys.path ("vendored"). No writable dir: import from the wheels directly.
    """
    import hashlib
    import importlib

    missing = [m for m in modules if not _importable(m)]
    if not missing:
        return "installed"
    manifest_path = os.path.join(vendor_dir, "MANIFEST.json")
    if not os.path.isfile(manifest_path):
        raise HedgeDocError(f"missing Python modules {missing} and no bundled wheels; run: {PIP_HINT}")
    with open(manifest_path, "rb") as fh:
        raw = fh.read()
    manifest = json.loads(raw)
    wheels = []
    for name, digest in sorted(manifest.items()):
        path = os.path.join(vendor_dir, name)
        with open(path, "rb") as fh:
            if hashlib.sha256(fh.read()).hexdigest() != digest:
                raise HedgeDocError(f"bundled wheel {name} fails sha256 check; reinstall the skill or run: {PIP_HINT}")
        wheels.append(path)
    key = hashlib.sha256(raw).hexdigest()[:16]
    extracted = _extract_wheels(wheels, key, _cache_roots(cache_root))
    paths = [extracted] if extracted else wheels
    for p in reversed(paths):
        if p not in sys.path:
            sys.path.insert(0, p)
    importlib.invalidate_caches()
    still = [m for m in missing if not _importable(m)]
    if still:
        raise HedgeDocError(f"bundled wheels do not provide {still}; run: {PIP_HINT}")
    return "vendored"


class Client:
    def __init__(self, server: str, cookie: str | None = None, timeout: float = 30):
        ensure_deps(("requests",))
        import requests  # lazy: pure helpers/tests work without it

        self.server = normalize_server(server)
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "hedgedoc-skill/1.0"
        if cookie:
            cookie = cookie.strip()
            if cookie.startswith(SESSION_COOKIE + "="):
                cookie = cookie.split("=", 1)[1]
            self.s.cookies.set(SESSION_COOKIE, cookie, domain=urlsplit(self.server).hostname, path="/")

    def url(self, path: str) -> str:
        return self.server + "/" + path.lstrip("/")

    def get(self, path: str, **kw):
        kw.setdefault("timeout", self.timeout)
        return self.s.get(self.url(path) if not path.startswith("http") else path, **kw)

    def _check(self, r, what: str):
        if r.status_code == 302 and is_forbidden_redirect(r.headers.get("Location", ""), self.server):
            raise HedgeDocError(f"{what}: forbidden (server redirected to /). Log in or check permissions.")
        if r.status_code >= 400:
            hint = {
                403: "forbidden",
                404: "not found (or no read permission)",
                413: "too large",
                429: "rate limited, wait a few minutes",
                409: "already exists",
            }.get(r.status_code, "")
            raise HedgeDocError(f"{what}: HTTP {r.status_code} {hint}".strip())
        return r

    # ---- server
    def config(self) -> dict:
        return parse_config_js(self._check(self.get("config"), "config").text)

    def status(self) -> dict:
        return self._check(self.get("status"), "status").json()

    def me(self) -> dict:
        return self.get("me").json()

    def login(self, user: str, password: str, ldap: bool = False) -> str:
        path, data = (
            ("auth/ldap", {"username": user, "password": password})
            if ldap
            else ("login", {"email": user, "password": password})
        )
        self.get("")  # obtain a session first
        self.s.post(self.url(path), data=data, allow_redirects=False, timeout=self.timeout)
        if self.me().get("status") != "ok":
            raise HedgeDocError("login failed (wrong credentials, or that login method is disabled)")
        return self.s.cookies.get(SESSION_COOKIE)

    # ---- notes
    def create(self, markdown: str, alias: str | None = None) -> str:
        if utf16_len(markdown) > DOC_MAX_LENGTH:
            raise HedgeDocError(f"note too long ({utf16_len(markdown)} > {DOC_MAX_LENGTH})")
        r = self.s.post(
            self.url("new/" + alias if alias else "new"),
            data=markdown.encode("utf-8"),
            headers={"Content-Type": "text/markdown; charset=utf-8"},
            allow_redirects=False,
            timeout=self.timeout,
        )
        self._check(r, "create" + (f" alias {alias!r} (needs allowFreeURL and maybe login)" if alias else ""))
        loc = r.headers.get("Location", "")
        if r.status_code != 302 or not loc:
            raise HedgeDocError(f"create: unexpected HTTP {r.status_code}")
        return parse_note_ref(loc, self.server)[1]

    def exists(self, note: str) -> bool:
        # never GET /<id> to probe: with allowFreeURL that CREATES the note
        return self.get(f"{note}/info", allow_redirects=False).status_code == 200

    def download(self, note: str) -> str:
        r = self._check(self.get(f"{note}/download", allow_redirects=False), f"download {note}")
        r.encoding = "utf-8"
        return r.text

    def info(self, note: str) -> dict:
        return self._check(self.get(f"{note}/info", allow_redirects=False), f"info {note}").json()

    def revisions(self, note: str) -> list:
        return self._check(self.get(f"{note}/revision", allow_redirects=False), "revisions").json()["revision"]

    def revision(self, note: str, time_ms: str) -> dict:
        return self._check(self.get(f"{note}/revision/{time_ms}", allow_redirects=False), "revision").json()

    def _redirect(self, note: str, action: str) -> str:
        r = self._check(self.get(f"{note}/{action}", allow_redirects=False), action)
        return r.headers["Location"]

    def links(self, note: str) -> dict:
        info = self.info(note)
        publish = self._redirect(note, "publish")
        slide = self._redirect(note, "slide")
        edit = self.url(note)
        return {
            "title": info.get("title"),
            "edit": edit,
            "view": edit + "?view",
            "both": edit + "?both",
            "edit_only": edit + "?edit",
            "publish": publish,
            "slide": slide,
            "slide_print_pdf": slide + "?print-pdf",
            "download": edit + "/download",
            "info": edit + "/info",
            "revisions": edit + "/revision",
        }

    # ---- media
    def upload(self, path: str) -> str:
        cfg = self.config()
        if cfg.get("enableUploads") == "none":
            raise HedgeDocError("uploads are disabled on this server")
        allowed = cfg.get("allowedUploadMimeTypes") or list(set(EXT_MIME.values()))
        problem = upload_problem(path, allowed)
        if problem:
            raise HedgeDocError(problem)
        mime = EXT_MIME[os.path.splitext(path)[1].lower()]
        with open(path, "rb") as fh:
            r = self.s.post(
                self.url("uploadimage"),
                files={"image": (os.path.basename(path), fh, mime)},
                allow_redirects=False,
                timeout=max(self.timeout, 120),
            )
        self._check(
            r,
            f"upload {os.path.basename(path)}"
            + (" (anonymous uploads disabled: log in)" if cfg.get("enableUploads") == "registered" else ""),
        )
        return r.json()["link"]

    def fetch(self, url: str, out_dir: str) -> str:
        os.makedirs(out_dir, exist_ok=True)
        name = os.path.basename(urlsplit(url).path) or "file"
        dest = os.path.join(out_dir, name)
        r = self._check(self.s.get(url, stream=True, timeout=max(self.timeout, 120)), f"fetch {url}")
        with open(dest, "wb") as fh:
            for chunk in r.iter_content(65536):
                fh.write(chunk)
        return dest

    # ---- realtime edit (socket.io + OT); 1.x has no HTTP endpoint for changing a note
    def edit(self, note: str, transform, transport: str = "polling", verify_s: float = 30) -> dict:
        ensure_deps(("socketio", "engineio", "websocket"))
        import socketio

        if not self.exists(note):
            raise HedgeDocError(f"note {note!r} not found (or not readable)")
        logged_in = self.me().get("status") == "ok"
        if SESSION_COOKIE not in self.s.cookies:
            self.get(f"{note}/info")
        if SESSION_COOKIE not in self.s.cookies:
            self.get("")
        cookie = "; ".join(f"{k}={v}" for k, v in self.s.cookies.items())
        st = {}
        ev = {k: threading.Event() for k in ("doc", "refresh", "ack", "fail")}
        sio = socketio.Client(http_session=self.s, reconnection=False)

        # server has connectionStateRecovery on: events carry an extra offset arg -> *_
        @sio.on("doc")
        def _doc(d, *_):
            if ev["doc"].is_set() and not ev["ack"].is_set():
                st["resent"] = True  # server re-sends doc after a failed operation
            st["doc"] = d
            ev["doc"].set()

        @sio.on("refresh")
        def _refresh(d, *_):
            st["refresh"] = d
            ev["refresh"].set()

        @sio.on("ack")
        def _ack(rev, *_):
            st["ack"] = rev
            ev["ack"].set()

        @sio.on("info")
        def _info(d, *_):
            st["info"] = d
            ev["fail"].set()

        @sio.on("operation")
        def _op(*args):
            st["concurrent"] = st.get("concurrent", 0) + 1

        u = urlsplit(self.server)
        sio_path = (u.path.strip("/") + "/socket.io") if u.path.strip("/") else "socket.io"
        try:
            sio.connect(
                f"{u.scheme}://{u.netloc}/?noteId={note}",
                headers={"Cookie": cookie},
                transports=[transport],
                socketio_path=sio_path,
                wait_timeout=self.timeout,
            )
        except Exception as e:  # noqa: BLE001
            raise HedgeDocError(f"realtime connect failed: {e}") from e
        try:
            for k in ("doc", "refresh"):
                if not ev[k].wait(self.timeout) or ev["fail"].is_set():
                    raise HedgeDocError(f"realtime: no '{k}' from server {st.get('info', '')}")
            ref, doc = st["refresh"], st["doc"]
            perm = ref.get("permission")
            owner = ref.get("owner")
            my_id = self.me().get("id") if logged_in else None
            may = (
                perm == "freely"
                or (perm in ("editable", "limited") and logged_in)
                or (perm in ("locked", "protected", "private") and owner and owner == my_id)
            )
            if not may:
                raise HedgeDocError(
                    f"no write permission: note is {perm!r}"
                    + ("" if logged_in else ", you are anonymous (set HEDGEDOC_COOKIE)")
                )
            old = doc["str"]
            new = transform(old)
            limit = ref.get("docmaxlength") or DOC_MAX_LENGTH
            if utf16_len(new) > limit:
                raise HedgeDocError(f"result too long: {utf16_len(new)} > {limit}")
            ops = make_text_operation(old, new)
            if not ops:
                return {"note": note, "changed": False}
            sio.emit("operation", (doc["revision"], ops, None))
            if not ev["ack"].wait(self.timeout):
                raise HedgeDocError(
                    "server did not acknowledge the edit" + (" (it rejected the operation)" if st.get("resent") else "")
                )
        finally:
            sio.disconnect()
        result = {
            "note": note,
            "changed": True,
            "revision": st["ack"],
            "chars_before": utf16_len(old),
            "chars_after": utf16_len(new),
        }
        deadline = time.time() + verify_s
        while time.time() < deadline:  # server flushes dirty notes to DB on an interval
            if self.download(note) == new:
                result["verified"] = True
                return result
            time.sleep(1.5)
        result["verified"] = False
        result["warning"] = "saved content differs from what was sent" + (
            ": someone else edited concurrently" if st.get("concurrent") else ", re-download to check"
        )
        return result


# --------------------------------------------------------------------------- CLI


def _read_input(src: str) -> str:
    if src == "-":
        return sys.stdin.read()
    with open(src, encoding="utf-8") as fh:
        return fh.read()


def _out(obj, as_json=True):
    print(json.dumps(obj, indent=2, ensure_ascii=False) if as_json else obj)


def _client_for(args, note_ref: str | None = None):
    server = args.server or os.environ.get("HEDGEDOC_URL")
    note = None
    if note_ref is not None:
        server, note = parse_note_ref(note_ref, server)
    if not server:
        raise HedgeDocError("no server: pass --server or set HEDGEDOC_URL")
    return Client(server, args.cookie or os.environ.get("HEDGEDOC_COOKIE"), args.timeout), note


def _push_media(c: Client, md: str, base_dir: str) -> tuple[str, dict]:
    local = find_local_media(md, base_dir)
    uploaded = {}
    for target, path in local.items():
        if path not in uploaded.values():
            uploaded[target] = c.upload(path)
        else:
            uploaded[target] = next(uploaded[t] for t, p in local.items() if p == path and t in uploaded)
    return rewrite_links(md, uploaded), uploaded


def _pull(c: Client, note: str, out_dir: str, external: bool = False, localize: bool = False) -> dict:
    """Note + its uploaded media into out_dir (note.md, media/); returns the pull result."""
    md = c.download(note)
    media_dir = os.path.join(out_dir, "media")
    mapping = {}
    for u in find_server_media(md, c.server, external):
        try:
            mapping[u] = "media/" + os.path.basename(c.fetch(u, media_dir))
        except Exception as e:  # noqa: BLE001
            print(f"warn: {u}: {e}", file=sys.stderr)
    if localize:
        md = rewrite_links(md, mapping)
    os.makedirs(out_dir, exist_ok=True)
    dest = os.path.join(out_dir, "note.md")
    with open(dest, "w", encoding="utf-8", newline="") as fh:
        fh.write(md)
    return {"note": dest, "media": mapping}


def _download_image(url: str, dest_dir: str, a) -> str:
    return Client(url, a.cookie or os.environ.get("HEDGEDOC_COOKIE"), a.timeout).fetch(url, dest_dir)


def _stage_local_deck(src: str, tmp: str, a) -> tuple[str, list[str]]:
    """Copy a local deck into tmp in the pull --localize layout: images outside media/
    (other local paths, web URLs with --external) become media/<file>. src stays untouched."""
    import shutil

    import offline

    with open(src, encoding="utf-8", newline="") as fh:
        md = fh.read()
    base = os.path.dirname(os.path.abspath(src))
    outside = offline.unbundled_images(md)
    web = [t for t in outside if re.match(r"^https?://", t)]
    if web and not a.external:
        raise HedgeDocError(
            "images on the web would not work offline: " + ", ".join(web) + " (pass --external to download "
            "them into the package; your files stay unchanged)"
        )
    local = find_local_media(md, base)
    unknown = [t for t in outside if t not in web and t not in local]
    if unknown:
        raise HedgeDocError("image files not found: " + ", ".join(unknown))
    media_dir = os.path.join(tmp, "media")
    os.makedirs(media_dir, exist_ok=True)
    for ref in set(offline.MEDIA_REF.findall(md)):
        if os.path.isfile(os.path.join(base, ref)):
            shutil.copyfile(os.path.join(base, ref), os.path.join(tmp, ref))
    taken = set(os.listdir(media_dir))
    mapping = {}
    for target in outside:
        with tempfile.TemporaryDirectory(dir=tmp) as dl:
            path = _download_image(target, dl, a) if target in web else local[target]
            stem, ext = os.path.splitext(re.sub(r"[^A-Za-z0-9._-]", "-", os.path.basename(path)))
            if ext.lower() not in offline.MEDIA_TYPES:
                raise HedgeDocError(f"{target}: not a supported image type ({', '.join(offline.MEDIA_TYPES)})")
            name, n = stem + ext, 1
            while name in taken:
                n += 1
                name = f"{stem}-{n}{ext}"
            taken.add(name)
            shutil.copyfile(path, os.path.join(media_dir, name))
        mapping[target] = "media/" + name
    staged = os.path.join(tmp, os.path.basename(src))
    with open(staged, "w", encoding="utf-8", newline="") as fh:
        fh.write(rewrite_links(md, mapping))
    return staged, sorted(mapping)


def _export_offline(a) -> dict:
    import offline

    if not _importable("yaml"):
        ensure_deps(("ruamel.yaml",))
    reveal = offline.RevealSource(a.reveal) if a.reveal else offline.bundled_reveal()
    if os.path.isfile(a.src):
        with open(a.src, encoding="utf-8") as fh:
            if not offline.unbundled_images(fh.read()):  # already media/ layout: package the file as is
                offline.export(a.src, a.output, reveal, a.source_url)
                return {"written": a.output, "source": a.src}
        with tempfile.TemporaryDirectory(prefix="hedgedoc-offline-") as tmp:
            staged, embedded = _stage_local_deck(a.src, tmp, a)
            offline.export(staged, a.output, reveal, a.source_url)
        return {"written": a.output, "source": a.src, "embedded": embedded}
    c, note = _client_for(a, a.src)
    source_url = a.source_url
    if not source_url:
        try:
            source_url = c.links(note)["slide"]
        except HedgeDocError:
            source_url = c.url(note)
    with tempfile.TemporaryDirectory(prefix="hedgedoc-offline-") as tmp:
        # media that failed to download stays a URL; the builder refuses it instead of shipping broken images
        pulled = _pull(c, note, tmp, a.external, localize=True)
        offline.export(pulled["note"], a.output, reveal, source_url)
    return {"written": a.output, "source": source_url, "media": len(pulled["media"])}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="hedgedoc.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--server", help="base URL, e.g. https://pad.example.org (env HEDGEDOC_URL)")
    p.add_argument("--cookie", help="connect.sid session cookie value (env HEDGEDOC_COOKIE)")
    p.add_argument("--timeout", type=float, default=30)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("server-info", help="version, upload policy, status")
    sub.add_parser("whoami", help="show logged-in user (needs cookie)")
    sp = sub.add_parser("login", help="email or LDAP login, prints connect.sid cookie value")
    sp.add_argument("user", help="email (or LDAP username with --ldap)")
    sp.add_argument("--ldap", action="store_true")
    sp.add_argument("--password", help="default: env HEDGEDOC_PASSWORD or prompt")

    sp = sub.add_parser("create", help="new note from file or - (stdin)")
    sp.add_argument("src")
    sp.add_argument("--alias", help="custom note id; needs allowFreeURL on the server")
    for name, hlp in (
        ("download", "print or save note markdown"),
        ("info", "title/description/views/times"),
        ("links", "all URLs: edit/view/both/publish/slide/print-pdf"),
        ("revisions", "list revision timestamps"),
    ):
        sp = sub.add_parser(name, help=hlp)
        sp.add_argument("note", help="note URL (any view) or id")
        if name == "download":
            sp.add_argument("-o", "--output")
    sp = sub.add_parser("revision", help="content of one revision")
    sp.add_argument("note")
    sp.add_argument("time", help="revision time in ms from `revisions`")

    for name, hlp in (
        ("update", "replace whole note content (realtime OT edit)"),
        ("append", "append text to note (realtime OT edit)"),
    ):
        sp = sub.add_parser(name, help=hlp)
        sp.add_argument("note")
        sp.add_argument("src", help="file or - (stdin)")
        sp.add_argument("--transport", default="polling", choices=["polling", "websocket"])

    sp = sub.add_parser("upload", help="upload media file(s), print markdown")
    sp.add_argument("files", nargs="+")
    sp = sub.add_parser("fetch", help="download media URL(s) into a directory")
    sp.add_argument("urls", nargs="+")
    sp.add_argument("-o", "--out-dir", default=".")

    sp = sub.add_parser("push", help="upload local images of a .md, rewrite links, create or update note")
    sp.add_argument("src", help="markdown file")
    sp.add_argument("--note", help="existing note to replace instead of creating a new one")
    sp.add_argument("--alias")
    sp.add_argument("--transport", default="polling", choices=["polling", "websocket"])
    sp = sub.add_parser("pull", help="download note + its uploaded media into a directory")
    sp.add_argument("note")
    sp.add_argument("-o", "--out-dir", required=True)
    sp.add_argument("--external", action="store_true", help="also fetch external image URLs")
    sp.add_argument("--localize", action="store_true", help="rewrite media links to local media/ paths")

    sp = sub.add_parser("lint", help="check HedgeDoc formatting pitfalls (offline)")
    sp.add_argument("src")

    sp = sub.add_parser("export-offline", help="deck as one offline HTML (.html) or HTML + extractor (.zip)")
    sp.add_argument("src", help="note URL/id, or a local .md with media/ next to it (pull --localize layout)")
    sp.add_argument("-o", "--output", required=True, help="deck.html or deck.zip")
    sp.add_argument("--source-url", help="link stored in the package (default: the pad's /p/ URL)")
    sp.add_argument("--external", action="store_true", help="also embed external image URLs")
    sp.add_argument("--reveal", help="other reveal.js: unpacked dist/ dir or .tar.xz (default: bundled)")
    sp = sub.add_parser("extract-offline", help="restore markdown + media from an export-offline HTML")
    sp.add_argument("html")
    sp.add_argument("-o", "--out-dir", required=True)
    sp.add_argument("--force", action="store_true", help="overwrite existing files")

    a = p.parse_args(argv)
    try:
        if a.cmd == "lint":
            warns = lint_markdown(_read_input(a.src))
            _out(warns)
            return 1 if warns else 0
        if a.cmd == "server-info":
            c, _ = _client_for(a)
            v2 = c.get("api/v2/monitoring", allow_redirects=False)
            if v2.status_code == 200 and v2.headers.get("Content-Type", "").startswith("application/json"):
                _out(
                    {
                        "server": c.server,
                        "major": 2,
                        "monitoring": v2.json(),
                        "note": "HedgeDoc 2.x: this CLI targets 1.x, use references/api-2x.md",
                    }
                )
                return 0
            cfg = c.config()
            if "version" not in cfg:
                raise HedgeDocError(f"{c.server}/config has no version: not a HedgeDoc 1.x server?")
            _out(
                {
                    "server": c.server,
                    "major": 1,
                    "version": cfg.get("version"),
                    "enableUploads": cfg.get("enableUploads"),
                    "allowedUploadMimeTypes": cfg.get("allowedUploadMimeTypes"),
                    "status": c.status(),
                }
            )
        elif a.cmd == "whoami":
            c, _ = _client_for(a)
            _out(c.me())
        elif a.cmd == "login":
            c, _ = _client_for(a)
            pw = a.password or os.environ.get("HEDGEDOC_PASSWORD") or getpass.getpass("password: ")
            print(c.login(a.user, pw, a.ldap))
        elif a.cmd == "create":
            c, _ = _client_for(a)
            md = _read_input(a.src)
            for w in lint_markdown(md):
                print("lint " + w, file=sys.stderr)
            nid = c.create(md, a.alias)
            _out({"id": nid, "url": c.url(nid)})
        elif a.cmd in ("download", "info", "links", "revisions", "revision"):
            c, note = _client_for(a, a.note)
            if a.cmd == "download":
                md = c.download(note)
                if a.output:
                    with open(a.output, "w", encoding="utf-8", newline="") as fh:
                        fh.write(md)
                    _out({"saved": a.output, "chars": utf16_len(md)})
                else:
                    sys.stdout.write(md)
            elif a.cmd == "revision":
                _out(c.revision(note, a.time))
            else:
                _out(getattr(c, a.cmd)(note))
        elif a.cmd in ("update", "append"):
            c, note = _client_for(a, a.note)
            text = _read_input(a.src)
            if a.cmd == "update":
                for w in lint_markdown(text):
                    print("lint " + w, file=sys.stderr)
                fn = lambda old: text  # noqa: E731
            else:
                fn = lambda old: old + ("" if old.endswith("\n") or not old else "\n") + text  # noqa: E731
            _out(c.edit(note, fn, a.transport))
        elif a.cmd == "upload":
            c, _ = _client_for(a)
            for f in a.files:
                link = c.upload(f)
                print(f"![{os.path.splitext(os.path.basename(f))[0]}]({link})")
        elif a.cmd == "fetch":
            c, _ = (
                _client_for(a, None)
                if (a.server or os.environ.get("HEDGEDOC_URL"))
                else (Client(a.urls[0], a.cookie or os.environ.get("HEDGEDOC_COOKIE"), a.timeout), None)
            )
            _out([c.fetch(u, a.out_dir) for u in a.urls])
        elif a.cmd == "push":
            c, note = _client_for(a, a.note) if a.note else _client_for(a)
            md = _read_input(a.src)
            md, uploaded = _push_media(c, md, os.path.dirname(os.path.abspath(a.src)))
            for w in lint_markdown(md):
                print("lint " + w, file=sys.stderr)
            if note:
                res = c.edit(note, lambda old: md, a.transport)
            else:
                nid = c.create(md, a.alias)
                res = {"id": nid, "url": c.url(nid)}
            res["uploaded"] = uploaded
            _out(res)
        elif a.cmd == "pull":
            c, note = _client_for(a, a.note)
            _out(_pull(c, note, a.out_dir, a.external, a.localize))
        elif a.cmd == "export-offline":
            _out(_export_offline(a))
        elif a.cmd == "extract-offline":
            import extract_offline

            written = extract_offline.extract(a.html, a.out_dir, a.force)
            _out({"markdown": str(written[0]), "files": len(written), "next": f"push {written[0]}"})
    except (HedgeDocError, ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
