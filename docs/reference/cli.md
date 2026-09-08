# CLI

```bash
pip install inherent
inherent --help
```

The CLI talks to the public API over HTTP. It never opens a database
connection. Environment variables override `~/.inherent/config.toml`:

| Variable | Overrides |
| --- | --- |
| `INHERENT_URL` | `[api].url` |
| `INHERENT_API_KEY` | `[api].key` |
| `INHERENT_HOME` | default `~/.inherent` |

`--json` on a read command writes only JSON to stdout. Exit codes: `0`
success, `1` error, `2` stack not running / not configured.

Global `--workspace` sets `X-Workspace-Id`. `inherent --version` is the CLI
version. Pin engine images with `inherent up --engine-version X.Y.Z`.

`up` defaults the engine image tag to the CLI's own version, so a CLI
published ahead of its engine images fails on the image pull. When that
happens `up` names the version it tried and points at `--engine-version`.

A `403` is a scope error, not a bad key: the message names the workspace the
key may not reach. Pass `--workspace <id>` rather than rotating the key.

## Stack lifecycle

Requires Docker Engine and Compose v2 (`docker compose`).

```bash
inherent up
inherent status
inherent --json status
inherent logs inh-public-api-svc --tail 50
inherent doctor
inherent down
inherent down --volumes --yes
```

`up` generates `~/.inherent/compose.env` (0600) if missing — existing
secrets are never rotated — then runs:

```text
docker compose -p inherent -f <bundled docker-compose.release.yml> \
  --env-file ~/.inherent/compose.env up -d --wait
```

and checks `GET /v1/whoami`. Service counts come from
`docker compose ps --format json`, not a hardcoded total.

## Documents, chunks, search

These commands work against a remote deployment with only env vars set.

```bash
inherent docs upload ./README.md
inherent docs list --page 1 --page-size 20
inherent --json docs show <doc-id>
inherent docs lineage <doc-id>
inherent docs refresh <doc-id>
inherent docs delete <doc-id> --yes
inherent chunks <doc-id>
inherent search "what is inherent?" --mode hybrid --limit 10
inherent --json search "what is inherent?"
```

`--mode` is `hybrid` (default), `keyword`, or `semantic`. Unsupported
extensions are rejected locally from `inh_contracts.file_types`. A 400
"Multiple workspaces found" becomes: pass `--workspace` or set it in
`config.toml`. Failed documents print the reason and
`Run: inherent logs inh-ingestion-svc`.

## Identity

```bash
inherent whoami
inherent workspaces list
inherent keys list
inherent keys create --name "ci" --save
inherent keys revoke ink_abc12345 --yes
```

`whoami` is `GET /v1/whoami` and works remote.

`workspaces list` tries `GET /v1/admin/workspaces`. **Only HTTP 404**
falls back to the caller's `workspace_ids` from `/v1/whoami`. 401/403/500
are errors.

`keys list` is local-only (`ADMIN_API_ENABLED`). A 404 explains that and
exits 1. Output is prefixes, never full keys.

`keys create` / `keys revoke` run the bootstrap compose service against
the local stack. A remote `INHERENT_URL` exits 2. Revoking the key in
`config.toml` requires `--force`.

## Connect

Writes Streamable HTTP MCP config. `--print` is the safe path: no files
touched, JSON includes the API key.

```bash
inherent connect claude --print
inherent connect cursor --print
inherent connect claude --config-path /tmp/mcp.json
```

| Agent | Default config path | Env override |
| --- | --- | --- |
| `claude` (Linux/macOS) | `~/.claude.json` | `CLAUDE_CONFIG_DIR/.claude.json` |
| `cursor` (Linux/macOS) | `~/.cursor/mcp.json` | `--config-path` |

Pin (Claude Code, 2026-08-30): HTTP entries need `"type": "http"`. A
`url` without `type` is treated as stdio.
https://code.claude.com/docs/en/mcp

Pin (Cursor, 2026-08-30): remote servers are `url` + `headers`.
`type: "http"` is accepted; `streamable-http` breaks `cursor-agent`.
https://docs.cursor.com/en/context/mcp

Existing files are parsed and only `mcpServers.inherent` is upserted.
Malformed JSON is refused. A backup `<file>.bak-<timestamp>` is written
first. Mode is 0600. After write, the CLI `POST`s `{url}/mcp` initialize.

## `~/.inherent/`

```
config.toml    # identity + endpoint, 0600
compose.env    # generated compose secrets, 0600
```
