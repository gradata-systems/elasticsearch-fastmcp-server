# Elasticsearch FastMCP server

MCP server that lets agents and chat clients query Elasticsearch for automation and security
investigations, with OAuth2 (Keycloak) authentication, per-role index access and an audit trail.

## How access control works

1. Clients authenticate with Keycloak and send the access token to this server. The token's
   signature, issuer and audience are checked against the realm's JWKS.
2. The caller's Keycloak roles are mapped to Elasticsearch index patterns by `rbac.yaml`.
3. For each distinct set of index patterns, the server uses a native ES user (the *broker*) to
   create a short-lived API key limited to `read` + `view_index_metadata` on those patterns.
   Every search runs with that key, so **Elasticsearch enforces the index boundary**. A derived
   key can never exceed the broker's own privileges.
4. Callers whose roles map to no indices are refused before any key is created. (An API key
   created without role descriptors would inherit all of the broker's privileges.)

Only index-level control is possible on a Basic license, since document- and field-level
security need Platinum.

## Tools

Generic tools work against any index the caller may read, whatever its schema:

| tool | purpose |
|---|---|
| `list_data_sources` | indices, aliases and data streams the caller can read |
| `describe_fields` | field names and types (field caps), filterable by pattern |
| `search_events` | events in a time range with exact-match/range filters and an optional Lucene query |
| `top_values` | most frequent values of a field (terms aggregation) |
| `esql_query` | read-only ES\|QL, with the time range applied as a filter |

Guardrails: every query needs a time range of at most `ES_MCP_MAX_TIME_RANGE_DAYS` (default 90)
days. Results are capped at `ES_MCP_MAX_RESULT_SIZE` rows and `ES_MCP_MAX_RESPONSE_CHARS`
characters, with a `truncated` flag and a hint to narrow the query. Leading wildcards are
rejected in Lucene queries. ES error reasons are returned so the model can correct its query.

The `get_windows_*` tools are source-specific shortcuts for Windows ECS data.

## Elasticsearch setup

Create a role and a native user for the broker. The broker must be a native user, not an API
key, because keys created by an API key can't carry privileges. Its index privileges must cover
every pattern in `rbac.yaml`.

```
POST /_security/role/mcp_key_broker
{
  "cluster": ["manage_own_api_key"],
  "indices": [
    { "names": ["ecs-*"], "privileges": ["read", "view_index_metadata"] }
  ]
}

POST /_security/user/mcp_key_broker
{ "password": "<generated>", "roles": ["mcp_key_broker"] }
```

## Keycloak setup (26.5)

FastMCP's Keycloak integration needs Keycloak 26.6+ for MCP clients to register themselves
dynamically, so on 26.5 **register each client in Keycloak up front**:

- **`es-mcp`** (resource server): define the client roles that `rbac.yaml` maps
  (e.g. `soc-analyst`, `helpdesk`) and assign them to users, groups or service accounts.
- **Audience**: add an *Audience* mapper (included client audience `es-mcp`) to a client scope
  used by the calling clients. Tokens without `aud: es-mcp` are rejected.
- **OpenWebUI / chat clients**: confidential or public client using authorization code + PKCE.
- **LangGraph / automation agents**: one confidential client per agent with *Service accounts*
  enabled (client-credentials grant). Assign `es-mcp` client roles to the service account so
  each agent has its own identity in the audit log.

## Audit log

Every tool call and every Elasticsearch query is written as a JSON line to stdout, or to
`ES_MCP_AUDIT_LOG_FILE`, separately from application logs. On a Basic license this is the
only record linking a query to a person, because Elasticsearch's own audit log is a paid
feature. Ship it somewhere the investigated users can't modify.

| event | when | key fields |
|---|---|---|
| `tool_call` | every tool invocation | `tool`, `arguments`, `outcome`, `error`, `duration_ms` |
| `es_request` | every ES request | `api` (search, esql, field_caps, resolve_index), `index`, `request`, `outcome`, result counts, `took_ms` |
| `access_denied` | unauthenticated, index not in policy, or ES 403 | `reason`, `index`, `roles` |

Every event carries `ts`, `sub`, `username`, `client_id` and a `call_id` that links a
`tool_call` to its `es_request` events. ES requests also carry `X-Opaque-Id: mcp:<sub>`, so they
show up in ES slow logs and the tasks API.

## Running

```
cp .env.example .env   # fill in
uv run python main.py
```

Terminate TLS at your ingress, or set `ES_MCP_TLS_CERTFILE` / `ES_MCP_TLS_KEYFILE` to serve HTTPS
directly. Tests: `uv run pytest`.
