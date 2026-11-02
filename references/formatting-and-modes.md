# Formatting for HedgeDoc 1.x: documents, published pages, slides

Verified against 1.12.0 (markdown-it 14, MathJax 2.7, Mermaid 9.1.7, reveal.js 3.9.2,
abcjs 6). Same markdown renders in the editor preview, `/s/` (published) and, split into
slides, in `/p/` (slide view).

## Contents
1. Pick the mode
2. YAML metadata
3. Document syntax beyond CommonMark
4. Slides (reveal.js 3)
5. Recipes from real-world use
6. Pitfalls

## 1. Pick the mode

| User wants | Write it as | Share this URL |
|---|---|---|
| shared working doc, others edit | plain note | `/<id>` (or `?both`) |
| read-only page for readers (minutes, handout, blog-ish) | doc + `title`, `description`, `tags` | `/s/<shortid>` |
| presentation | `type: slide` + `---` separators | `/p/<shortid>`; PDF via `/p/<shortid>?print-pdf` + browser print |
| show rendered note in editor UI without editor | any | `/<id>?view` |
| editor only (fast typing, mobile) | any | `/<id>?edit` |

`hedgedoc.py links <note>` prints all of them. There is no "book mode" in HedgeDoc 1.x
(that is HackMD). Emulate a book with an index note that links chapter notes, each with
`[TOC]`.

## 2. YAML metadata

Must be the very first thing in the note (line 1 is `---`). Indent with exactly two spaces.

```yaml
---
title: Board meeting 2026-10-28        # overrides first H1 as title / filename
description: Minutes and decisions      # <meta description> on /s/ page
tags: meeting, board                    # shown in history, comma list
lang: de-de                             # ISO 639-1 (+region)
dir: ltr                                # or rtl
breaks: true                            # default true: every newline is a <br>
robots: noindex, nofollow               # for published pages
type: slide                             # editor preview shows slides
slideOptions:                           # reveal.js 3 config
  theme: white
  transition: fade
opengraph:
  title: Share title
  image: https://example.org/cover.png
---
```

Also accepted: `GA: UA-…` (Google Analytics), `disqus: shortname`. Avoid unless asked.

Slide themes (reveal 3): black (default), white, league, beige, sky, night, serif, simple,
solarized, blood, moon. Transitions: none, fade, slide, convex, concave, zoom. Other
`slideOptions` keys are reveal.js 3 config: `controls`, `progress`, `slideNumber`, `center`,
`width`, `height`, `margin`, `transitionSpeed` (default/fast/slow), `backgroundTransition`,
`loop`, `rtl`. The same options can go in the URL: `/p/<id>?transition=zoom&slideNumber=true`.

## 3. Document syntax beyond CommonMark

````markdown
[TOC]                               ← own line; up to 3 heading levels

==marked==  ++inserted++  ~~deleted~~  19^th^  H~2~O  :smile:

- [ ] open task
- [x] done task

:::info        (also: success, warning, danger)
Callout box
:::

> quote text
> [name=Alice] [time=2026-10-28 14:00] [color=#907bf7]

![alt](https://…/uploads/x.png =300x)     ← resize: =WxH, =Wx, =xH
<img src="https://…" width="300">          ← raw HTML allowed (sanitised)

Term
: definition                               ← definition lists
*[HTML]: Hyper Text Markup Language        ← abbreviations
Footnote[^1].   [^1]: Text.                 ← footnotes

$E = mc^2$   $$\int_0^1 x\,dx$$             ← MathJax 2 (LaTeX)

```js=        ← line numbers; =101 start at 101; =+ continue previous block
```!          ← wrap long lines

```mermaid    ← Mermaid 9.1.7 syntax only (no newer diagram types)
```graphviz   ```sequence   ```flow   ```abc      ← other diagram/music blocks

{%youtube VIDEOID %}  {%vimeo 123 %}  {%gist user/id %}
{%slideshare user/deck %}  {%speakerdeck user/deck %}  {%pdf https://…/file.pdf %}
````

Relative links between notes: `[Chapter 2](/<noteid>)` or full URLs. Header anchors are
generated; style per server `linkifyHeaderStyle` in `/config` (default `keep-case`:
`## My Part` → `#My-Part`; `lower-case`/`gfm` → `#my-part`).

## 4. Slides (reveal.js 3)

Rules the slide parser enforces (regex `^\n---\n$`, `^\n----\n$`):
- `---` alone on a line **with an empty line above and below** = next horizontal slide.
- `----` same rule = next vertical (sub) slide below the current one.
- `---` directly under a text line is a setext H2 underline, not a break. This is the most
  common broken deck. Run `hedgedoc.py lint deck.md`.
- First `---` block at line 1 is YAML, not a slide break.

```markdown
---
type: slide
slideOptions:
  theme: white
  transition: slide
---

# Title slide

Subtitle

---

## Agenda

- Item 1 <!-- .element: class="fragment" -->
- Item 2 <!-- .element: class="fragment" -->

Note:
  Speaker notes go after `Note:` (indented). Press S in the slide view.

----

### Detail below Agenda (vertical slide)

---

<!-- .slide: data-background="#1A237E" data-transition="zoom" -->

## Coloured slide

---

<!-- .slide: data-background="https://example.org/bg.jpg" -->

<div style="color:#fff">

## Text on image background

</div>
```

Fragments: `<!-- .element: class="fragment" -->` after an element; styles `grow`, `shrink`,
`fade-out`, `fade-up`/`down`/`left`/`right`, `current-visible`, `highlight-red`/`green`/`blue`;
order with `data-fragment-index="1"`. Per-slide: `data-transition`, `data-transition-speed`,
`data-background`, `data-background-color`, `data-background-size`, `data-background-video`.
Keys in slide view: Space/arrows navigate, Esc overview, S speaker view, B pause.

Fit content: aim for a title + at most ~6 bullets or one image per slide. Long tables and
code overflow because reveal does not auto-shrink. Split them.

## 5. Recipes from real-world use

Collected from instance docs (universities, server.camp, community forum):

- **Meeting minutes**: title `YYYY-MM-DD Meeting <topic>`, `tags: meeting`, sections
  Attendees / Agenda / Decisions / Action items (`- [ ] @name task`). Edit live during the
  meeting via `/<id>`, then send `/s/<shortid>` to readers.
- **Docs / runbook**: `[TOC]` under the H1, `:::warning` for dangerous steps, numbered code
  blocks (`bash=`), `tags` for findability.
- **Index note**: one note linking all key notes, pinned in history. Notes have random URLs,
  so the index is how people find things.
- **Workshop / lecture**: deck in slide mode, handout = same note via `/s/`, attendees
  collaborate in a separate `freely` note.
- **Sensitive content**: permission `limited` (login to read) or `private`. Anonymous notes
  are `freely`: anyone with the link can edit. Say so when publishing secrets would leak.
- **Slide images**: size with `=WxH`; for borderless images in slides use
  `<img src="…" style="border:none;box-shadow:none" width="400">`.
- **PDF export**: slides via `/p/<id>?print-pdf` then browser print; documents via browser
  print of `/s/<id>`. No server-side PDF in 1.x.
- Slide view (`/p/`) and the editor's slide preview differ slightly (margins, image size).
  Check the real `/p/` link before presenting.

## 6. Pitfalls

- YAML not on line 1, tabs or 4-space indent in YAML → metadata ignored.
- `breaks: true` default: single newlines are line breaks (unlike GitHub).
- Images must be uploaded (`hedgedoc.py upload`) or public URLs. Local paths break.
  Avoid spaces in image file names; if present, write `%20`.
- Max note length 100000 JS chars (emoji count 2). Split long material into several notes.
- Mermaid is 9.1.7: no `mindmap`, `timeline`, `quadrantChart`, `xychart`, `block`, etc.
