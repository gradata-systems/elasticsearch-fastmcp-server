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

### Identity provider

| Variable | Chart value | Default | Meaning |
|---|---|---|---|
| `ES_MCP_OIDC_ISSUER` | `oidc.issuer` | required | OpenID Connect issuer, e.g. `https://keycloak.example.com/realms/security`. Must equal the tokens' `iss` exactly. |
| `ES_MCP_OIDC_AUDIENCE` | `oidc.audience` | required (chart: `es-mcp`) | Audience tokens must carry. |
| `ES_MCP_OIDC_JWKS_URI` | `oidc.jwksUri` | from discovery | URL of the provider's signing keys. By default taken from `<issuer>/.well-known/openid-configuration`; set it only for a provider without a discovery document there. |
| `ES_MCP_OIDC_CA_CERTS` | `oidc.ca.secretName` / `configMapName` / `key` | system CAs | CA file that signed the provider's HTTPS certificate, trusted in addition to the system CAs when the server fetches its discovery document and signing keys. Needed when the provider uses a private CA; otherwise every token is rejected. |
| `ES_MCP_OIDC_TOKEN_ALGORITHM` | `oidc.tokenAlgorithm` | `RS256` | Algorithm the provider signs access tokens with. |
| `ES_MCP_USERNAME_CLAIM` | `oidc.usernameClaim` | `preferred_username` | Token claim holding the Elasticsearch username. Must not be something users can edit. |
| `ES_MCP_PUBLIC_BASE_URL` | `publicBaseUrl` | required | The URL clients connect to, e.g. `https://es-mcp.example.com`. Used in the OAuth metadata the server publishes, so it must match what clients use. |

### Deployment identity

Each deployment serves one cluster. Set these when an agent uses several deployments at once (see
[Several clusters](deployment.md#several-clusters)):

| Variable | Chart value | Default | Meaning |
|---|---|---|---|
| `ES_MCP_CLUSTER_NAME` | `cluster.name` | none | Name of the cluster, e.g. `Production SIEM`. It becomes the MCP server's name and appears in the server instructions, at the end of every tool description ("Cluster: …"), in `list_data_sources` and in every audit event. |
| `ES_MCP_CLUSTER_DESCRIPTION` | `cluster.description` | none | A sentence on what the cluster holds, e.g. `Security logs for the head office`. Shown in the server instructions and `list_data_sources`. |
| `ES_MCP_TOOL_PREFIX` | `toolPrefix` | none | Prefix for every tool and prompt name, e.g. `prod_` for `prod_search_events`. Lower-case letters, digits and underscores, ending in `_`. Tool descriptions, hints and the `create_source_pack` prompt then refer to other tools by their prefixed names. |

When the cluster name is set, the instructions and `list_data_sources` tell agents that other
clusters may be available through other deployments. The agent chooses a cluster only by the
source packs each deployment offers the user, and asks which cluster they mean when packs on more
than one cluster could fit, or none clearly does.

The server refuses to start if a prefixed tool name would be longer than 64 characters, the
limit many model APIs place on tool names.

### Limits

| Variable | Chart value | Default | Meaning |
|---|---|---|---|
| `ES_MCP_MAX_TIME_RANGE_DAYS` | `limits.maxTimeRangeDays` | `90` | Longest time range for tools that return events: `search_events`, pack search tools and ES\|QL without `STATS`. |
| `ES_MCP_MAX_AGGREGATION_RANGE_DAYS` | `limits.maxAggregationRangeDays` | `366` | Longest time range for tools that return counts: `top_values`, `distinct_values`, each period of `compare_periods`, pack tools of those kinds and ES\|QL with `STATS`. |
| `ES_MCP_MAX_RESULT_SIZE` | `limits.maxResultSize` | `500` | Most rows any tool returns: events, values, changed groups or ES\|QL rows. Tool schemas show it as the `size` maximum, and results cut short by it say so. |
| `ES_MCP_TOOL_TIMEOUT` | `limits.toolTimeoutSeconds` | `60` | Longest a whole tool call may take, in seconds, however many Elasticsearch requests it makes. The call is then stopped with an error, and Elasticsearch cancels the search when the connection closes. |
| `ES_MCP_MAX_REPEATED_CALLS` | `limits.maxRepeatedCalls` | `3` | How many identical calls (same user, tool and arguments) may run within the window below. Further ones are refused with an error telling the model to use the earlier result. |
| `ES_MCP_REPEATED_CALL_WINDOW_SECONDS` | `limits.repeatedCallWindowSeconds` | `300` | The window for `ES_MCP_MAX_REPEATED_CALLS`. |
| `ES_MCP_MAX_RESPONSE_CHARS` | `limits.maxResponseChars` | `100000` | Most characters of JSON a tool returns, to protect the model's context window. Longer results are cut off, with `truncated: true` and a hint. |

Each Elasticsearch search also carries a server-side `timeout` of 90% of `ES_MCP_ES_REQUEST_TIMEOUT`,
so it stops shortly before the client gives up and returns what it found, marked as incomplete.
String values longer than 2,000 characters are shortened in results, and marked as such.

Repeated calls are counted in each replica's memory. With several replicas, a client whose calls
are spread across them may make up to that many identical calls per replica.

The limits apply to every caller. The server refuses to start if one is below 1 (below 1000 for
`ES_MCP_MAX_RESPONSE_CHARS`). Raise them with care: a larger response budget costs the model
context, and a longer event search range costs Elasticsearch.

For models with a small context window, such as many local models served through Ollama and
OpenWebUI, lower `ES_MCP_MAX_RESPONSE_CHARS` (for example to 20000) and `ES_MCP_MAX_RESULT_SIZE`
(for example to 100). Also check the context length configured for the model (Ollama's
`num_ctx`): results beyond it are dropped without warning, and the model then invents what it
can't see.

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

ES_MCP_OIDC_ISSUER=https://keycloak.example.com/realms/security
ES_MCP_OIDC_AUDIENCE=es-mcp
ES_MCP_PUBLIC_BASE_URL=https://es-mcp.example.com

ES_MCP_TLS_CERTFILE=/etc/es-mcp/tls.crt
ES_MCP_TLS_KEYFILE=/etc/es-mcp/tls.key
ES_MCP_AUDIT_LOG_FILE=/var/log/es-mcp/audit.jsonl

# Allow a year for event searches too, e.g. on a small cluster
# ES_MCP_MAX_TIME_RANGE_DAYS=366
```

The server fails at startup if a required setting is missing, or if the access policy or a pack
file is invalid. The error names the setting or file at fault.
