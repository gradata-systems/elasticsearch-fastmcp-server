# Audit log

Every tool call, and every Elasticsearch request it makes, is written as one JSON object per line.
Events go to stdout by default, which suits Kubernetes log shipping, or to the file named by
`ES_MCP_AUDIT_LOG_FILE`. Either way they are kept separate from application logs.

On a Basic licence this is the **only record linking a query to a person**, because
Elasticsearch's own audit log is a paid feature. Ship it somewhere the people being investigated
can't modify it.

## Events

| Event | When | Key fields |
|---|---|---|
| `tool_call` | every tool invocation, when it finishes | `tool`, `arguments`, `outcome` (`success` or `error`), `error`, `duration_ms` |
| `es_request` | every Elasticsearch request | `api` (`search`, `esql`, `field_caps`, `resolve_index`), `es_user`, `index`, `request`, `outcome` (`success`, `partial` or `error`), `error`, `took_ms`, and result counts |
| `access_denied` | a request refused by the server or by Elasticsearch | `reason`, `es_user`, `index`, and `api` and `request` where there is one |

Result counts depend on the API: `hits_returned`, `hits_total` and `shards_failed` for searches,
`rows` for ES|QL, and `fields_returned` for field capabilities.

`access_denied` reasons:

| Reason | Meaning |
|---|---|
| `unauthenticated` | No valid access token. |
| `username_not_impersonable` | The token's username isn't in `impersonable_users`, or is a reserved or malformed name. |
| `index_not_exposed` | The index is outside `exposed_indices`. |
| `run_as_denied` | Elasticsearch refused to run as the user: no such user, a disabled user, or not covered by the impersonation account's `run_as`. |
| `elasticsearch_403` | The user's own roles don't allow the request. |

Every event also carries:

- `ts`: the time, in UTC
- `cluster`: the deployment's `ES_MCP_CLUSTER_NAME`, when set, so that trails from several
  deployments can be told apart
- `sub`, `username` and `client_id`: who called, from the access token
- `call_id`: links a `tool_call` to the `es_request` and `access_denied` events it caused

## Example

A `top_values` call and the search it made:

```json
{"ts": "2026-09-29T06:14:02.118+00:00", "event": "es_request", "call_id": "5f0c9e1d2b6a4c7e9f3a1b2c3d4e5f60", "sub": "8d2f...", "username": "john.smith1", "client_id": "openwebui", "api": "search", "es_user": "john.smith1", "index": "ecs-ingress-nginx-access-*", "request": {"size": 0, "query": {"bool": {"filter": ["..."]}}, "aggs": {"top": {"terms": {"field": "source.ip", "size": 10}}}, "track_total_hits": true}, "outcome": "success", "took_ms": 38, "hits_returned": 0, "hits_total": {"value": 1843, "relation": "eq"}, "shards_failed": 0}
{"ts": "2026-09-29T06:14:02.171+00:00", "event": "tool_call", "call_id": "5f0c9e1d2b6a4c7e9f3a1b2c3d4e5f60", "sub": "8d2f...", "username": "john.smith1", "client_id": "openwebui", "tool": "top_values", "arguments": {"index": "ecs-ingress-nginx-access-*", "field": "source.ip", "time_range": {"start": "2026-09-01"}}, "outcome": "success", "duration_ms": 61}
```

A refused request:

```json
{"ts": "2026-09-29T06:20:44.503+00:00", "event": "access_denied", "call_id": "a1b2...", "sub": "8d2f...", "username": "john.smith1", "client_id": "openwebui", "reason": "index_not_exposed", "api": "search", "es_user": "john.smith1", "index": "hr-payroll"}
```

## Finding the queries in Elasticsearch

Requests run as the user and carry `X-Opaque-Id: mcp:<username>`, so they appear under that user
in Elasticsearch's slow logs and in the tasks API:

```
GET _tasks?detailed=true&actions=*search*
```

Tasks started through this server show `"X-Opaque-Id": "mcp:john.smith1"` in their headers.

## Useful queries on the audit log

Once the audit log is indexed, for example as `es-mcp-audit`, you can answer questions about the
server's use:

- Everything one person did: filter on `username`.
- Every index a person queried: `distinct_values` on `index`, filtered on `username` and
  `event: es_request`.
- Refused requests: filter on `event: access_denied` and rank `reason` with `top_values`.
- Slow or failing tools: filter on `event: tool_call` and sort by `duration_ms`, or filter on
  `outcome: error`.
