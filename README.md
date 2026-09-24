# MCP iDO Sport

An unofficial, read-only MCP server for planned training sessions in
[iDO Sport](https://www.idosport.app/). The upstream service does not provide a public API,
so this project may need updates when its private web endpoints change.

The server exposes two namespaced tools:

- `ido_get_calendar` lists planned sessions in a bounded date range.
- `ido_get_event_plan` returns the structured plan for a calendar event.

Completed activities are not returned and no training data is modified.

## Credentials

Copy the example file and fill in your own values:

```console
cp .env.example .env
```

```dotenv
IDO_USERNAME='your-login-or-email'
IDO_PASSWORD='your-literal-password'
```

Single quotes preserve spaces and characters such as `#` and `$`. Escape a literal single quote
inside a value as `\'`. Variable interpolation is disabled when this file is loaded, so passwords
containing `${...}` remain literal. The real `.env` file is ignored by Git and excluded from the
container build context. Never commit it or bake credentials into an image.

## Local development

[uv](https://docs.astral.sh/uv/) provides the reproducible development environment:

```console
uv sync --locked --all-extras
uv run mcp-idosport
```

The default transport is stdio. A local MCP client can launch the same command from this
repository. The compatibility command `uv run python server.py` is also supported.

Run the checks with:

```console
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

## Streamable HTTP

Set the transport explicitly to expose the server over HTTP:

```dotenv
MCP_TRANSPORT='streamable-http'
MCP_HOST='0.0.0.0'
MCP_PORT='8000'
MCP_PATH='/mcp'
MCP_ALLOWED_HOSTS='localhost,localhost:*,127.0.0.1,127.0.0.1:*'
```

The MCP endpoint is `/mcp`. Liveness and readiness endpoints are available at `/healthz` and
`/readyz`. `MCP_ALLOWED_HOSTS` is a comma-separated allowlist for the HTTP `Host` header; add the
service DNS names or LAN hostname that will actually be used. `MCP_ALLOWED_ORIGINS` optionally
accepts a comma-separated origin allowlist.

The server uses one authenticated session, serializes access to it, retries once after session
expiry, and caches identical reads for 30 seconds. Set `IDO_CACHE_TTL_SECONDS` from `0` to `300`
to change that behavior.

## Container

Build and run the production image locally:

```console
docker build -t mcp-idosport:local .
export IDO_USERNAME='your-login-or-email'
export IDO_PASSWORD='your-literal-password'
docker run --rm -e IDO_USERNAME -e IDO_PASSWORD -p 8000:8000 mcp-idosport:local
```

The default container command enables Streamable HTTP on port 8000. The image runs as an
unprivileged user and includes a health check. Avoid passing the quoted development `.env` file
to Docker's `--env-file` option because Docker preserves those quote characters.

Images produced from the default branch and version tags are published to
`ghcr.io/arthur-c/mcp-idosport`. Version tags use the form `v1.2.3`.

## Configuration reference

| Variable | Default | Purpose |
| --- | --- | --- |
| `IDO_USERNAME` | required | iDO Sport login or email |
| `IDO_PASSWORD` | required | iDO Sport password |
| `IDO_CACHE_TTL_SECONDS` | `30` | Read cache lifetime, from 0 to 300 seconds |
| `MCP_TRANSPORT` | `stdio` | `stdio` or `streamable-http` |
| `MCP_HOST` | `127.0.0.1` | HTTP bind address |
| `MCP_PORT` | `8000` | HTTP port |
| `MCP_PATH` | `/mcp` | MCP HTTP path |
| `MCP_ALLOWED_HOSTS` | local hosts | Accepted HTTP host headers |
| `MCP_ALLOWED_ORIGINS` | empty | Accepted browser origins, comma-separated |
| `LOG_LEVEL` | `INFO` | Python logging level |

## Security notes

- Authentication follows only HTTPS form actions on `www.idosport.app`; credentials are never
  posted to a different origin.
- Inputs, response sizes, date spans, result counts, and HTTP request bodies are bounded.
- Tool errors contain actionable messages but do not expose credentials or upstream response
  bodies.
- HTTP mode enables MCP DNS-rebinding protection. Keep the host allowlist narrow.

For an orchestrated deployment, inject the two credential values from a secret provider rather
than mounting a repository `.env` file.
