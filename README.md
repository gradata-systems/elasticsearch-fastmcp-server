# Elasticsearch FastMCP server

MCP server that lets agents and chat clients query Elasticsearch for automation and security
investigations, with OAuth2 (Keycloak) authentication, per-user Elasticsearch authorization and an audit trail.

## How access control works

1. Clients authenticate with Keycloak and send the access token to this server. The token's
   signature, issuer and audience are checked against the realm's JWKS.
2. The token's `preferred_username` must be an existing Elasticsearch user of the same name,
   e.g. `john.smith1`, or `service-account-<client-id>` for agents.
3. The server logs in as an impersonation account and sends every request with
   `es-security-runas-user: <username>`, so **Elasticsearch applies that user's own roles**,
   the same ones they have in Kibana. The impersonation account has no index access of its own.
4. `access_policy.yaml` adds two server-side limits:
   - `impersonable_users`: which usernames may be run as. Built-in accounts (`elastic`,
     `kibana_system`, …), the impersonation account itself, and malformed names are always refused.
   - `exposed_indices`: which indices this server serves. Callers see the overlap of these and
     their ES roles, so data they can see in Kibana but that isn't meant for MCP stays out.
     This narrows scope; the user's ES roles remain the security boundary. For ES|QL, the
     indices in `FROM` and `LOOKUP JOIN` are checked, and queries that can't be parsed
     confidently are refused.

Only index-level control is possible on a Basic license, since document- and field-level
security need Platinum.

## Tools

Generic tools work against any index the caller may read, whatever its schema:

| tool | purpose |
|---|---|
| `list_data_sources` | exposed indices, aliases and data streams the caller can read |
| `describe_fields` | field names and types (field caps), filterable by pattern |
| `search_events` | events in a time range with exact-match/range filters and an optional Lucene query |
| `top_values` | most frequent values of a field (terms aggregation) |
| `esql_query` | read-only ES\|QL, with the time range applied as a filter |

Guardrails: every query needs a time range of at most `ES_MCP_MAX_TIME_RANGE_DAYS` (default 90)
days. Results are capped at `ES_MCP_MAX_RESULT_SIZE` rows and `ES_MCP_MAX_RESPONSE_CHARS`
characters, with a `truncated` flag and a hint to narrow the query. Leading wildcards are
rejected in Lucene queries. ES error reasons are returned so the model can correct its query.

## Source packs

Each YAML file in `packs/` (`ES_MCP_PACKS_DIR`) describes one data source and the curated tools
built on it. Adding a source is a new YAML file, not new code:

```yaml
name: windows
title: Microsoft Windows security events
index: ecs-microsoft-windows-v1
description: What the data is and what it's good for.
key_fields:                 # shown to the model by list_data_sources
  user.name: Account logon name, e.g. 'john.smith1'
default_fields: ['@timestamp', user.name, event.code]   # returned by search tools
tools:
  - name: windows_remote_access_events
    kind: search            # or top_values (with `field` and optional `include_fields`)
    description: RDG and RDP connection events for a user, chronologically.
    params:                 # become tool arguments; matched exactly (or by prefix)
      user:
        description: Logon name or SID
        fields: [user.name, user.id]   # any of these may match
    filters:                # fixed conditions, same format as search_events filters
      - {field: event.provider, op: in, value: [Microsoft-Windows-TerminalServices-Gateway]}
    sort: asc
```

Every generated tool also takes the required `time_range` and a capped `size`, and goes through
the same run-as, exposed-index and audit path as the generic tools. Packs whose index isn't in
`exposed_indices` are skipped at startup. `list_data_sources` describes each pack the caller can
read, including its key fields and tools.

Included packs: `windows` (Windows security events), `ingress_nginx` (ingress-nginx access logs)
and `fortios` (FortiGate firewall logs).

## Elasticsearch setup

Create an impersonation role and account for this server. Use a separate account from any
Kibana proxy, with a `run_as` list that only covers people and agents. Never use `["*"]`,
which would allow impersonating `elastic`. It must be a native user with a password: run-as
isn't available to API keys.

```
POST /_security/role/mcp_impersonator
{ "cluster": [], "indices": [], "run_as": ["*.*", "service-account-*"] }

POST /_security/user/mcp_impersonator
{ "password": "<generated>", "roles": ["mcp_impersonator"] }
```

Keep `impersonable_users` in `access_policy.yaml` in step with that `run_as` list.

**Agents:** create a native ES user per agent named after its Keycloak service-account username,
`service-account-<client-id>`. Give it the narrowest role it needs and a long random password
that is never used; don't disable the user, since disabled users can't be run as.

## Keycloak setup (26.5)

FastMCP's Keycloak integration needs Keycloak 26.6+ for MCP clients to register themselves
dynamically, so on 26.5 **register each client in Keycloak up front**:

- **Usernames must not be user-editable.** Access is decided by `preferred_username`; if users
  can rename themselves they can impersonate others. Keep the realm's *Edit username* off, or
  source usernames read-only from LDAP/AD.
- **Audience**: add an *Audience* mapper (included client audience `es-mcp`) to a client scope
  used by the calling clients. Tokens without `aud: es-mcp` are rejected.
- **OpenWebUI / chat clients**: confidential or public client using authorization code + PKCE.
- **LangGraph / automation agents**: one confidential client per agent with *Service accounts*
  enabled (client-credentials grant), plus the matching ES user described above.

## Audit log

Every tool call and every Elasticsearch query is written as a JSON line to stdout, or to
`ES_MCP_AUDIT_LOG_FILE`, separately from application logs. On a Basic license this is the
only record linking a query to a person, because Elasticsearch's own audit log is a paid
feature. Ship it somewhere the investigated users can't modify.

| event | when | key fields |
|---|---|---|
| `tool_call` | every tool invocation | `tool`, `arguments`, `outcome`, `error`, `duration_ms` |
| `es_request` | every ES request | `api` (search, esql, field_caps, resolve_index), `es_user`, `index`, `request`, `outcome`, result counts, `took_ms` |
| `access_denied` | unauthenticated, username not impersonable, index not exposed, run-as refused, or ES 403 | `reason`, `es_user`, `index` |

Every event carries `ts`, `sub`, `username`, `client_id` and a `call_id` that links a
`tool_call` to its `es_request` events. ES requests run as the user and carry `X-Opaque-Id: mcp:<username>`,
so they show up under that user in ES slow logs and the tasks API.

## Running

```
cp .env.example .env   # fill in
uv run python main.py
```

Terminate TLS at your ingress, or set `ES_MCP_TLS_CERTFILE` / `ES_MCP_TLS_KEYFILE` to serve HTTPS
directly. Tests: `uv run pytest`.
