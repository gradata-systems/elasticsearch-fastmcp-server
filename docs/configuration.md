# Configuration

The server reads `ES_MCP_*` environment variables, or a `.env` file in its working directory
(`.env.example` is a starting point). In Kubernetes, the Helm chart sets them from its values
(see [Deployment](deployment.md)). Anything the chart doesn't cover can go in `extraEnv`.

## Settings

### Elasticsearch

| Variable | Chart value | Default | Meaning |
|---|---|---|---|
| `ES_MCP_ES_URL` | `elasticsearch.url` | required | Elasticsearch HTTPS URL, e.g. `https://prod-es-data-http.elastic.svc.prod:9200`. |
| `ES_MCP_ES_CA_CERTS` | `elasticsearch.ca.secretName` / `configMapName` / `key` | system CAs | CA file that signed Elasticsearch's certificate. Certificates are always verified. |
| `ES_MCP_ES_IMPERSONATOR_USERNAME` | `elasticsearch.impersonator.username` | required (chart: `mcp_impersonator`) | The run-as account (see [Elasticsearch setup](elasticsearch-setup.md)). |
| `ES_MCP_ES_IMPERSONATOR_PASSWORD` | `elasticsearch.impersonator.existingSecret` (or `.password`) | required | Its password. In Kubernetes, prefer an existing secret. |
| `ES_MCP_ES_REQUEST_TIMEOUT` | `elasticsearch.requestTimeout` | `30` | Seconds before an Elasticsearch request is abandoned. |

### Keycloak and identity

| Variable | Chart value | Default | Meaning |
|---|---|---|---|
| `ES_MCP_KEYCLOAK_REALM_URL` | `keycloak.realmUrl` | required | Realm URL, e.g. `https://keycloak.example.com/realms/security`. Must equal the tokens' `iss` exactly. |
| `ES_MCP_KEYCLOAK_AUDIENCE` | `keycloak.audience` | required (chart: `es-mcp`) | Audience tokens must carry. |
| `ES_MCP_USERNAME_CLAIM` | `keycloak.usernameClaim` | `preferred_username` | Token claim holding the Elasticsearch username. Must not be something users can edit. |
| `ES_MCP_PUBLIC_BASE_URL` | `publicBaseUrl` | required | The URL clients connect to, e.g. `https://es-mcp.example.com`. Used in the OAuth metadata the server publishes, so it must match what clients use. |

### Limits

| Variable | Chart value | Default | Meaning |
|---|---|---|---|
| `ES_MCP_MAX_TIME_RANGE_DAYS` | `limits.maxTimeRangeDays` | `90` | Longest time range for tools that return events: `search_events`, pack search tools and ES\|QL without `STATS`. |
| `ES_MCP_MAX_AGGREGATION_RANGE_DAYS` | `limits.maxAggregationRangeDays` | `366` | Longest time range for tools that return counts: `top_values`, `distinct_values`, each period of `compare_periods`, pack tools of those kinds and ES\|QL with `STATS`. |
| `ES_MCP_MAX_RESULT_SIZE` | `limits.maxResultSize` | `500` | Most events a search returns, and most rows an ES\|QL query returns. |
| `ES_MCP_MAX_RESPONSE_CHARS` | `limits.maxResponseChars` | `100000` | Most characters of JSON a tool returns, to protect the model's context window. Longer results are cut off, with `truncated: true` and a hint. |

The limits apply to every caller. Raise them with care: a larger response budget costs the model
context, and a longer event search range costs Elasticsearch.

### Policy, packs and audit

| Variable | Chart value | Default | Meaning |
|---|---|---|---|
| `ES_MCP_ACCESS_POLICY_FILE` | `accessPolicy` | `access_policy.yaml` | Exposed indices and impersonable users (see [Access control](access-control.md)). |
| `ES_MCP_PACKS_DIR` | `packs` | `packs` | Directory of source pack YAML files (see [Source packs](source-packs.md)). |
| `ES_MCP_AUDIT_LOG_FILE` | `extraEnv` | stdout | Where audit events go, as JSON lines (see [Audit log](audit-log.md)). |

### HTTP listener

| Variable | Chart value | Default | Meaning |
|---|---|---|---|
| `ES_MCP_HOST` | set by the chart | `0.0.0.0` | Address to listen on. |
| `ES_MCP_PORT` | `containerPort` | `8000` | Port to listen on. |
| `ES_MCP_TLS_CERTFILE`, `ES_MCP_TLS_KEYFILE` | `tls.*` | none | Serve HTTPS directly. Without them the server speaks plain HTTP and logs a warning; only run like that behind a TLS-terminating proxy. |
| `FASTMCP_STATELESS_HTTP` | `statelessHttp` | `false` | Handle each request on its own, with no in-memory MCP session. Needed with several replicas unless clients are pinned to one. |

## Example `.env`

```
ES_MCP_ES_URL=https://prod-es-data-http.elastic.svc.prod:9200
ES_MCP_ES_CA_CERTS=/etc/es-mcp/es-ca.crt
ES_MCP_ES_IMPERSONATOR_USERNAME=mcp_impersonator
ES_MCP_ES_IMPERSONATOR_PASSWORD=change-me

ES_MCP_KEYCLOAK_REALM_URL=https://keycloak.example.com/realms/security
ES_MCP_KEYCLOAK_AUDIENCE=es-mcp
ES_MCP_PUBLIC_BASE_URL=https://es-mcp.example.com

ES_MCP_TLS_CERTFILE=/etc/es-mcp/tls.crt
ES_MCP_TLS_KEYFILE=/etc/es-mcp/tls.key
ES_MCP_AUDIT_LOG_FILE=/var/log/es-mcp/audit.jsonl

# Allow a year for event searches too, e.g. on a small cluster
# ES_MCP_MAX_TIME_RANGE_DAYS=366
```

The server fails at startup if a required setting is missing, or if the access policy or a pack
file is invalid. The error names the setting or file at fault.
