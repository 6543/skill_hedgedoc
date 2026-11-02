# HedgeDoc 2.x public API (alpha, from `develop` branch, 2.0.0-alpha.3)

2.x is a rewrite (NestJS backend, React frontend). As of late 2026 it is still alpha and rare
on public instances. Detect it: `GET /api/v2/monitoring` answers JSON on 2.x, 404 on 1.x.
`scripts/hedgedoc.py` targets 1.x only; on a 2.x server use these endpoints with curl or
requests directly.

Base: `https://<host>/api/v2`. Auth: `Authorization: Bearer <token>`. Tokens are created by
the user in the web UI (Preferences → API → access tokens). Errors are JSON.
`/monitoring` needs no token, everything else does.

## Notes

| Method + path | Body / result |
|---|---|
| `POST /notes` | body markdown, `Content-Type: text/markdown` (exactly). 201 → note JSON |
| `POST /notes/<alias>` | same, with chosen alias; 409 if taken |
| `GET /notes/<alias>` | note JSON (content + metadata) |
| `GET /notes/<alias>/content` | raw markdown |
| `PUT /notes/<alias>` | **replace content** (text/markdown). Real HTTP edit, no socket needed |
| `DELETE /notes/<alias>` | delete (FULL permission) |
| `GET /notes/<alias>/metadata` | title, tags, owner, permissions, times |
| `GET /notes/<alias>/metadata/permissions` | permissions |
| `PUT/DELETE /notes/<alias>/metadata/permissions/users/<user>` | grant/revoke user |
| `PUT/DELETE /notes/<alias>/metadata/permissions/groups/<group>` | grant/revoke group |
| `PUT /notes/<alias>/metadata/permissions/owner` | change owner |
| `PUT /notes/<alias>/metadata/permissions/visibility` | public listing |
| `GET /notes/<alias>/revisions`, `/revisions/<uuid>` | history |
| `GET /notes/<alias>/media` | media attached to note |

## Media

| Method + path | Notes |
|---|---|
| `POST /media` multipart field **`file`**, header **`HedgeDoc-Note: <alias>`** | 201 → `{uuid, …}`; WRITE permission on that note |
| `GET /media/<uuid>` | download |
| `DELETE /media/<uuid>` | delete |

## Aliases, user

`POST /alias`, `PUT /alias/<alias>` (make primary), `DELETE /alias/<alias>`;
`GET /me`, `GET /me/notes`, `GET /me/media`; `GET /monitoring`, `/monitoring/health`.

## Differences that matter

- 2.x has tokens and a real PUT, so editing is plain HTTP.
- 2.x bundles reveal.js 5 (1.x: reveal.js 3). `type: slide` / `type: document` still selects
  the mode; other YAML keys and reveal options may differ. Check the instance's built-in
  help before assuming 1.x syntax.
