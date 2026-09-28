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
index: ecs-microsoft-windows-*   # names/patterns, comma-separated; '-' excludes, e.g. 'a-*,-a-debug-*,b'
description: What the data is, what it's good for, and how far back each tier goes.
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

When a source is split across retention tiers (for example a short-lived `-rs-*` index and a
long-term `-v1` index, each event stored in only one), give the pack a pattern covering all of them
and say in `description` how long each tier keeps data, so the model knows that older periods hold
only a subset and an empty result there does not mean nothing happened.

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

## CI and releases

`.github/workflows/ci.yml` runs the tests, lints and renders the chart against
`charts/es-mcp/ci/*-values.yaml`, builds the image and smoke-tests it over TLS on every push and
pull request. Pushes to `master` and `v*` tags publish the image to ghcr.io. To release, bump
`version` and `appVersion` in `charts/es-mcp/Chart.yaml` (and `version` in `pyproject.toml`), then
push a matching tag, e.g. `git tag v0.2.0 && git push origin v0.2.0`; a tag that doesn't match the
chart fails the chart release.

## Deploying to Kubernetes

The Helm chart in `charts/es-mcp` runs the server with TLS terminated by the server itself, so it
can sit directly behind a `LoadBalancer` service (L4 pass-through) with no ingress controller.

**1. Get the image.** CI publishes `ghcr.io/gradata-systems/elasticsearch-fastmcp-server`,
tagged by branch (`master`), commit (`sha-<short>`) and release version (`0.1.0`, `0.1`). The image
bakes in `access_policy.yaml` and `packs/` as defaults; the chart can replace either without a
rebuild. To build your own instead:

```
docker build -t <registry>/es-mcp:<tag> .
docker push <registry>/es-mcp:<tag>      # then set image.repository and image.tag
```

Release tags also publish the chart to `oci://ghcr.io/gradata-systems/charts/es-mcp`, so you can
install from there instead of a checkout.

**2. Create the secrets** in the target namespace (see *Elasticsearch setup* for the account):

```
kubectl create namespace es-mcp
kubectl -n es-mcp create secret generic es-mcp-impersonator --from-literal=password='<generated>'
kubectl -n es-mcp create secret generic es-ca --from-file=ca.crt=es-ca.crt   # CA of the ES HTTPS cert
kubectl -n es-mcp create secret tls es-mcp-tls --cert=tls.crt --key=tls.key   # server certificate
```

The server certificate must cover the host name in `publicBaseUrl`. To have cert-manager issue it
instead, skip the last command and set `tls.certManager.enabled=true` with an `issuerRef`.

**3. Write a values file**, e.g. `es-mcp-values.yaml`:

```yaml
publicBaseUrl: https://es-mcp.example.com   # what clients connect to; used in OAuth metadata
elasticsearch:
  url: https://prod-es-data-http.elastic.svc.prod:9200
  impersonator:
    existingSecret: es-mcp-impersonator
  ca:
    secretName: es-ca
keycloak:
  realmUrl: https://keycloak.example.com/realms/security
  audience: es-mcp
tls:
  existingSecret: es-mcp-tls
  # certManager: {enabled: true, issuerRef: {name: internal-ca, kind: ClusterIssuer}}
service:
  type: LoadBalancer
  port: 443
  loadBalancerIP: 10.0.0.50                 # optional; or annotations for your LB implementation
  loadBalancerSourceRanges: [10.0.0.0/8]
  externalTrafficPolicy: Local              # preserve client addresses
  annotations: {}
```

**4. Install, then point DNS at the load balancer:**

```
helm upgrade --install es-mcp charts/es-mcp -n es-mcp -f es-mcp-values.yaml
kubectl -n es-mcp get service es-mcp -o jsonpath='{.status.loadBalancer.ingress[0]}'
curl https://es-mcp.example.com/healthz     # "ok"; the MCP endpoint is /mcp
```

Rendering fails with a clear message if a required setting (URLs, impersonator secret, TLS
source) is missing.

**Changing the policy or packs.** `accessPolicy` replaces the image's `access_policy.yaml`, and
`packs` replaces the built-in packs (include every pack you want to keep):

```
helm upgrade es-mcp charts/es-mcp -n es-mcp -f es-mcp-values.yaml \
  --set-file 'packs.windows\.yaml=packs/windows.yaml' \
  --set-file 'packs.fortios\.yaml=packs/fortios.yaml'
```

Pods restart automatically when the policy, packs or chart-managed password change.

**Things to know**

- **Certificate renewal:** the server reads its certificate at startup. After the TLS secret is
  renewed, restart it (`kubectl -n es-mcp rollout restart deployment es-mcp`) or add a reloader
  annotation through `deploymentAnnotations`.
- **Replicas:** MCP sessions live in the memory of the replica that started them. With
  `replicaCount` above 1, set `statelessHttp: true` or `service.sessionAffinity: ClientIP`.
- **Service options:** `service` accepts `type`, `port`, `nodePort`, `annotations`, `labels`,
  `loadBalancerIP`, `loadBalancerClass`, `loadBalancerSourceRanges`, `externalTrafficPolicy`,
  `internalTrafficPolicy`, `sessionAffinity(Config)`, `ipFamilyPolicy`, `ipFamilies` and
  `externalIPs`. See `charts/es-mcp/values.yaml` for all settings.
- **TLS elsewhere:** if TLS is terminated in front of the server (an ingress or TLS-terminating
  load balancer), set `tls.enabled: false`; the service then listens on port 80.
- **Probes:** `/healthz` is unauthenticated and doesn't depend on Elasticsearch or Keycloak, so an
  outage there doesn't restart the pods.
