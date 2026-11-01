# conductor

The one piece of framework code: drives Red's Downloader so that cogs living in their own
git repos are installed, updated on push, and rolled back without touching Discord.

## Modes

| `CONDUCTOR_MODE` | What runs |
|---|---|
| `prod` (default) | aiohttp API on `CONDUCTOR_HOST:CONDUCTOR_PORT` (default `0.0.0.0:8080`), GitHub webhook, update poll every `CONDUCTOR_POLL_SECONDS` (default 900). |
| `dev` | Watches `CONDUCTOR_WATCH` (colon-separated, default `/cogs`) and hot-reloads loaded cogs on file change. Syntax errors are DMed and the old version keeps running. New repo folders are added as cog paths automatically; `[p]load <cog>` once. |

Prod env: `CONDUCTOR_API_TOKEN` (≥32 chars), `GITHUB_WEBHOOK_SECRET`.

## HTTP API

All routes need `Authorization: Bearer $CONDUCTOR_API_TOKEN` except `/github` and `/health`.

| Route | Action |
|---|---|
| `GET /health` | liveness |
| `GET /cogs` | installed cogs: commit, head, pinned, loaded |
| `POST /cogs` `{"repo": "<url>", "cog": "<optional pkg name>"}` | repo add → install → load |
| `POST /cogs/{name}/update` | pull → reinstall → reload (no-op if pinned/unchanged) |
| `POST /cogs/{name}/rollback` `{"commit": "<sha>"}` | install at commit → pin |
| `POST /cogs/{name}/unpin` | unpin → update |
| `DELETE /cogs/{name}` | unload → uninstall → repo remove |
| `POST /github` | push webhook, `X-Hub-Signature-256` verified, only the installed branch |

If an update fails to load, the previous commit is reinstalled and reloaded, the bad commit is
remembered and skipped until a newer push arrives. Every operation DMs the owner.

## Commands (owner)

`[p]conductor status | install <url> [cog] | update <cog> | rollback <cog> <sha> | unpin <cog> | remove <cog>`

## Events emitted / public methods

None.

## Updating the conductor itself

Not self-updating (it would reload mid-request). Use `[p]cog update conductor`.
