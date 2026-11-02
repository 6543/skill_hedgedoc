# Offline export

`export-offline` turns a slide deck into one HTML file that presents without network and
still contains the original markdown and media. `extract-offline` reverses it.

## Contents

1. What is in the package
2. How close it looks to HedgeDoc
3. Limits
4. Checking a package
5. Payload format
6. Updating the bundled reveal.js

## 1. What is in the package

| Part | Source |
|---|---|
| reveal.js 6, markdown/highlight/notes plugins, the deck's theme | `scripts/vendor/reveal.js-<v>.tar.xz` (sha256 in `ASSETS.json`) |
| HedgeDoc look (system font, headings keep case, monospace code) | `scripts/offline_parity.css` (own CSS; HedgeDoc's slide.css is AGPL and not copied) |
| front matter, `=WxH` image sizes, media → blob URLs, reveal init | `scripts/offline_runtime.js` |
| original markdown + every `media/<file>` it references | JSON payload, see 5 |

`.zip` output adds `extract_offline.py` (stdlib only) and a README so the recipient can
restore the source without this skill. Typical size: the deck's images plus about 1.2 MB
of reveal.js (highlight.js is most of it) plus the theme (0.6 MB for themes with inlined
fonts, e.g. black, white).

## 2. How close it looks to HedgeDoc

Mirrored from HedgeDoc 1.10.3 `public/js/slide.js` and `views/slide.ejs`:

- separators `---` and `----` with blank lines around; speaker notes after `Note:`
- defaults `controls`, `progress`, `slideNumber`, `history`, `center` on, `transition: none`
- only HedgeDoc's allow-listed `slideOptions` keys reach reveal.js (`theme` picks the CSS)
- theme fallback `black`; code highlighting zenburn
- front matter `breaks` (default true: single newlines are line breaks), `lang`, `dir: rtl`
- `<!-- .slide: -->` / `<!-- .element: -->` attributes, `![alt](src =WxH)` sizes
- slide links `#/h/v` match the pad's `/p/` links

Differences: HedgeDoc runs reveal.js 3.9 with markdown-it, the package reveal.js 6 with
marked. Ordinary decks render the same; pixel-exact layout of tight slides can shift a
little, so run the render check on decks that were already near the 720px limit.

## 3. Limits

- **Images must be embeddable.** For a pad, its uploads are fetched; `--external` also
  fetches other image URLs. For a local deck, `media/` files and other local image files
  are embedded and `--external` downloads web images (into a temp copy: the deck on disk
  is not changed, the package then holds the rewritten `media/` links). Anything still
  missing stops the export with a list.
  Links (`[text](url)`) stay links and need network when clicked.
- **Not rendered offline:** HedgeDoc markdown-it extras (`:::info` containers, `[TOC]`,
  `:emoji:`), mermaid/graphviz/flow code blocks, MathJax, `{%youtube %}`/`{%pdf %}`
  embeds. They show as plain text or code. Grep the deck for them and tell the user.
- **Speaker view** (`S`) opens a popup that loads the same file; browsers that block
  popups or `file://` iframes may show it empty. Presenting itself is unaffected.
- **Documents** (no `type: slide`) still export and round-trip, but are shown as slides.
  For reading a document offline, `pull --localize` is the better fit.

## 4. Checking a package

Round trip, should print nothing:

```bash
python3 scripts/hedgedoc.py pull <note> -o /tmp/a --localize
python3 scripts/hedgedoc.py extract-offline deck.html -o /tmp/b
diff -r /tmp/a /tmp/b
```

Render: the render check in `formatting-and-modes.md` §4 works on the file too
(`python3 render_check.py file:///abs/path/deck.html`). To prove it is really offline, add
`page.route("**/*", lambda r: r.continue_() if r.request.url.startswith(("file:", "blob:", "data:")) else r.abort())`
before `goto` and expect zero aborted requests and every `img` with a `blob:` src.

## 5. Payload format (version 1)

```html
<script type="application/json" id="hedgedoc-offline-source">{...}</script>
```

```json
{
  "format": "hedgedoc-offline", "version": 1,
  "markdown": {"name": "note.md", "sha256": "...", "text": "---\ntitle: ..."},
  "media": [{"path": "media/x.png", "type": "image/png", "sha256": "...", "base64": "..."}],
  "render": {"reveal": {"transition": "slide", "...": "..."}, "breaks": true},
  "source": {"url": "https://pad.example.org/p/abc"}
}
```

`<`, `>`, `&`, U+2028 and U+2029 are written as `\u` escapes, so the markdown cannot end the
script tag; any JSON parser undoes them. `extract_offline.py` checks every sha256, accepts
only `media/<file>` paths and a plain `.md` name, and does not overwrite without `--force`.

## 6. Updating the bundled reveal.js

Set `REVEAL_VERSION` in `scripts/update_vendor.py` and run it (needs network). It checks
the npm tarball against the registry's integrity hash, keeps only the needed dist files
and writes a reproducible archive plus `ASSETS.json`. Then rerun the tests and export one
real deck with the render check.
