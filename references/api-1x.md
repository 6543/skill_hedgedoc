# HedgeDoc 1.x server interface (verified against 1.12.0 source + live instance)

Most public instances run 1.x (check: `GET /config` → `window.version = '1.x.y-…'`).
`scripts/hedgedoc.py` wraps all of this; read this file when a command fails, when you
need an endpoint the script lacks, or when explaining behaviour to the user.

## Contents
1. Note IDs and URLs
2. HTTP endpoints
3. Auth and sessions
4. Editing existing notes (socket.io + OT)
5. Media uploads
6. Gotchas

## 1. Note IDs and URLs

A note answers to several IDs, all accepted wherever `<NOTE>` appears:
- random id (base64url of UUID, 22 chars) e.g. `yEHR8mQLREONMOf4gAWUxw`
- alias (FreeURL), e.g. `team-meeting`
- shortid (publish/slide links), e.g. `LC8JdSF43z`
- legacy LZString ids (old CodiMD)

| URL | What it shows |
|---|---|
| `/<NOTE>` | editor, default mode (both on desktop) |
| `/<NOTE>?view` / `?both` / `?edit` | force view-only / split / editor-only |
| `/s/<shortid>` | published read-only page (`/<NOTE>/publish` redirects here) |
| `/p/<shortid>` | reveal.js slide view (`/<NOTE>/slide` redirects here) |
| `/p/<shortid>?print-pdf` | slide print layout (browser print → PDF) |
| `/p/<shortid>?transition=zoom&controls=false` | reveal options via URL query |
| `/s/<shortid>/edit`, `/p/<shortid>/edit` | redirect back to the editor (`?both`) |

## 2. HTTP endpoints

| Method + path | Result | Notes |
|---|---|---|
| `POST /new` body=markdown, `Content-Type: text/markdown` | 302 → `/<newid>` | `text/plain` also accepted. Body limit 10 MB, but note limit `documentMaxLength` (default 100000 JS chars) → 413 |
| `POST /new/<alias>` | 302 → `/<alias>` | only with `allowFreeURL`; may need login (`requireFreeURLAuthentication`). 409 if exists |
| `GET /new` | 302 → new note from template | creates a note; avoid in scripts |
| `GET /<NOTE>/download` (also `/s/<id>/download`) | raw markdown, `text/markdown` | filename = title.md |
| `GET /<NOTE>/info` | JSON `{title, description, viewcount, createtime, updatetime}` | safe existence probe |
| `GET /<NOTE>/revision` | JSON `{revision:[{time, length}]}` | `time` ms epoch; saved every ~5 min while edited |
| `GET /<NOTE>/revision/<time>` | JSON `{content, authorship, …}` | |
| `GET /<NOTE>/publish`, `/slide` | 302 to `/s/…`, `/p/…` | how to learn the shortid |
| `POST /uploadimage` multipart field `image` | JSON `{link}` | see §5 |
| `GET /config` | JS: `window.version`, `enableUploads`, `allowedUploadMimeTypes`, … | parse lines `window.x = …` |
| `GET /status` | JSON stats (notesCount, online users) | |
| `GET /me` | `{status:"ok", id, name, photo}` or `{status:"forbidden"}` | login check |
| `GET /me/export` | zip of all own notes | login |
| `GET/POST/DELETE /history[/<NOTE>]` | user history, pin (`pinned=true`) | login; DELETE all needs `?token=` from `/config` |
| `POST /login` form `email,password` | 302 → `/` | email auth; success or failure both redirect, check `/me` |
| `POST /auth/ldap` form `username,password` | 302 → `/` | LDAP auth |

No HTTP endpoint changes or deletes an existing note. Delete and permission change are
socket events (`delete`, `permission`) and owner-only.

## 3. Auth and sessions

- No API tokens in 1.x. Auth = session cookie `connect.sid` (signed, HttpOnly).
- Get one: `hedgedoc.py login you@example.org` (email) or `--ldap user`, or copy
  `connect.sid` from the browser dev tools. Then `export HEDGEDOC_COOKIE=<value>`
  (fish: `set -x HEDGEDOC_COOKIE <value>`). OAuth/SAML-only instances: browser cookie only.
- Anonymous: works when `allowAnonymous` (create) / `allowAnonymousEdits` (edit). Anonymous
  notes have no owner and get permission `freely`.
- Rate limits: `/new` default 20 per 5 min per IP; login endpoints 10 per 5 min. 429 → wait.

## 4. Editing existing notes (socket.io + OT)

The browser edits through socket.io 4 (engine.io v4), and so does the script:

1. Have a session cookie (any request sets one; the socket middleware rejects connections
   without a signed `connect.sid`).
2. Connect `GET /socket.io/?noteId=<NOTE>&EIO=4&transport=polling` with the cookie
   (sub-path installs: `/<urlpath>/socket.io/`).
3. Server emits `refresh` `{title, permission, owner, docmaxlength, …}` and
   `doc` `{str, revision, clients}`. Events carry an extra trailing arg (connection-state
   recovery offset); handlers must accept extra args.
4. Client emits `operation` `(revision, ops, selection|null)`. `ops` = ot.js TextOperation JSON:
   positive int retain, string insert, negative int delete. Lengths are **UTF-16 code units**
   (JS string length): an emoji counts 2. Base length must equal the doc length or the
   server rejects it.
5. Server answers `ack` `(newRevision)`. Rejected ops (no permission) get no ack; malformed ones
   trigger a re-sent `doc`.
6. Server writes dirty notes to the DB on an interval and on last disconnect; poll
   `/download` to verify.

Write permission by note permission:

| permission | who may write |
|---|---|
| freely | everyone incl. guests |
| editable, limited | logged-in users |
| locked, protected, private | owner only |

Read: `limited`/`protected` need login, `private` owner only.

## 5. Media uploads

- `POST /uploadimage`, multipart field **`image`**. Response `{"link": "https://…/uploads/<uuid>.<ext>"}`.
- `enableUploads`: `all` | `registered` | `none` (see `/config`).
- Default allowed MIME types: jpeg, png, gif, svg+xml, webp (`allowedUploadMimeTypes` in `/config`).
  Animated WebP is accepted; APNG is rejected (convert to animated WebP or GIF).
  **No PDFs, videos, text files.** For those: host elsewhere and embed (`{%pdf URL %}`, link, YouTube).
- Server checks the magic bytes AND that the file extension matches them: `photo.jpg` that is
  really a PNG → 400. SVG is sanitised with DOMPurify.
- Errors come back as HTML pages with status 400/403/500, not JSON.
- Media download is a plain `GET` of the link (no auth on default filesystem backend).
- Uploads are not tied to a note and are not deleted with it.

## 6. Gotchas

- **Forbidden ≠ 403 for guests.** Anonymous forbidden requests get `302 → <server>/` with a
  flash message. A `302` from `POST /new` is only success when `Location` is a note URL.
- **Do not `GET /<id>` to test existence.** With `allowFreeURL`, visiting a missing id creates
  that note. Use `/<id>/info` (404 if missing).
- Title = YAML `title:` else first `# H1`. Changing the H1 changes the download filename.
- `\r` is stripped on create.
- Revisions list lags: revisions are written every 5 minutes.
- Old CodiMD/HackMD links (`/<id>#`, `/<id>?view`) keep working on HedgeDoc 1.x.
