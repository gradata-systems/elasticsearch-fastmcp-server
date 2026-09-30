# Elasticsearch FastMCP server

MCP server that lets agents and chat clients query Elasticsearch for automation and security
investigations, with OAuth2 (any OpenID Connect provider) authentication, per-user Elasticsearch authorization and an
audit trail.

- **Runs as the caller.** Every query runs with the caller's own Elasticsearch roles, through
  run-as. The server adds no access of its own and can only narrow what callers reach.
- **Works with any index.** Generic tools search, rank, list and compare events whatever the
  schema. Source packs (YAML) add context and shortcut tools for known data sources, with no code
  changes.
- **Guardrails for agents.** Time range limits, response size budgets, retention notes and clear
  errors that let an agent correct its own queries.
- **Audited.** Every tool call and Elasticsearch request is logged with the identity behind it.
- **One deployment per cluster.** An agent can use several side by side. Each deployment names
  its cluster and can prefix its tools, and agents ask the user when it isn't clear which cluster
  is meant.

## Tools

| Tool | Purpose |
|---|---|
| `list_data_sources` | What the caller can query, and the known data sources among it |
| `describe_fields` | Field names and types |
| `search_events` | Events matching a time range, filters and a full-text query |
| `top_values` | Most frequent values of a field |
| `distinct_values` | Every value of a field, with counts and first/last seen |
| `compare_periods` | What stopped, dropped, appeared or rose between two periods |
| `esql_query` | Read-only ES\|QL |
| `validate_source_pack` | Check a pack written with `create_source_pack`, without loading it |

Source packs describe your data sources and add curated tools for them. None are loaded by
default; `packs/` holds examples for Windows security events, FortiGate firewall logs and
ingress-nginx access logs. See the [tool guide](docs/tools.md) for arguments and examples.

The `create_source_pack` prompt has an agent write a pack for a new data source with you,
starting from its index mapping and your use cases (see [Source packs](docs/source-packs.md#generating-a-pack-with-an-agent)).

## Quick start

```
cp .env.example .env   # fill in
uv run python main.py  # MCP endpoint at /mcp
```

This needs an impersonation account in Elasticsearch and clients registered with an OpenID
Connect provider (see below). For Kubernetes, use the Helm chart in `charts/es-mcp`.

## Documentation

| Guide | Covers |
|---|---|
| [Tool guide](docs/tools.md) | Every tool, with guidance, arguments and examples |
| [Access control](docs/access-control.md) | How requests are authorized, and `access_policy.yaml` |
| [Elasticsearch setup](docs/elasticsearch-setup.md) | The impersonation account, and users for agents |
| [Identity provider setup](docs/identity-provider.md) | What tokens must carry, chat clients, agents and troubleshooting |
| [Keycloak setup](docs/keycloak-setup.md) | Ready-made Keycloak client scope and clients |
| [Configuration](docs/configuration.md) | Every setting, with defaults and chart values |
| [Source packs](docs/source-packs.md) | Describing a data source and its tools in YAML |
| [Audit log](docs/audit-log.md) | Audit events and fields |
| [Deployment](docs/deployment.md) | Running locally, in a container, and on Kubernetes with Helm |
| [Development](docs/development.md) | Code layout, tests, CI and releases |
