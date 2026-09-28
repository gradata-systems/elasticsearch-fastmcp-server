# Development

## Layout

| Path | What's there |
|---|---|
| `main.py` | Builds the FastMCP server: authentication, audit middleware, and tool registration. |
| `config.py` | Settings, read from `ES_MCP_*` variables (see [Configuration](configuration.md)). |
| `tools/generic.py` | The generic tools. |
| `tools/query.py` | Shared building blocks: time ranges, filters, query building and the response size budget. |
| `sources/packs.py` | Loading source packs and generating their tools (see [Source packs](source-packs.md)). |
| `prompts/` | MCP prompts, such as `create_source_pack`. |
| `security/policy.py` | `access_policy.yaml`: exposed indices and impersonable users. |
| `security/esql.py` | Extracting the indices an ES\|QL query reads, so they can be checked. |
| `security/audit.py` | The audit log. |
| `utils/elasticsearch.py` | The gateway every Elasticsearch request goes through: run-as, the index check, auditing and error handling. |
| `packs/` | The built-in source packs. |
| `keycloak/` | Keycloak client and client scope definitions. |
| `charts/es-mcp/` | The Helm chart. |

Keep the code independent of any data source or schema. Field names, index names and
source-specific behaviour belong in pack YAML, not in `tools/`, `sources/` or `security/`.

## Tests

```
uv sync
uv run pytest
```

The tests mock Elasticsearch, so they need no cluster. `tests/test_packs.py` also loads every pack
in `packs/`, so an invalid pack fails the tests.

## CI

`.github/workflows/ci.yml` runs on every push and pull request:

1. **test**: `uv run pytest`.
2. **chart**: `helm lint --strict` and `helm template` against each `charts/es-mcp/ci/*-values.yaml`,
   and a check that the chart refuses to render with no values.
3. **image**: builds the image and smoke-tests it over TLS, with a read-only filesystem and no
   capabilities. `/healthz` must return `ok`, and `/mcp` without a token must return 401.

Pushes to `master` and `v*` tags then publish the image to ghcr.io. Tags also publish the chart.

## Releasing

1. Bump `version` and `appVersion` in `charts/es-mcp/Chart.yaml`, and `version` in
   `pyproject.toml`.
2. Commit, then push a matching tag:

   ```
   git tag v0.2.0 && git push origin v0.2.0
   ```

The tag publishes the image as `0.2.0` and `0.2`, and the chart as
`oci://ghcr.io/gradata-systems/charts/es-mcp:0.2.0`. A tag that doesn't match the chart's version
fails the chart release.
