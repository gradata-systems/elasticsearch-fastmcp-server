# Deployment

Before deploying, set up [Elasticsearch](elasticsearch-setup.md) and [Keycloak](keycloak-setup.md).
All settings are described in [Configuration](configuration.md).

- [Running locally](#running-locally)
- [Running the container](#running-the-container)
- [Deploying to Kubernetes](#deploying-to-kubernetes)
- [Connecting clients](#connecting-clients)

## Running locally

```
cp .env.example .env   # fill in
uv run python main.py
```

The MCP endpoint is `/mcp` and the health check is `/healthz`. Either terminate TLS in front of
the server, or set `ES_MCP_TLS_CERTFILE` and `ES_MCP_TLS_KEYFILE` to serve HTTPS directly. Without
TLS, the server logs a warning at startup.

## Running the container

CI publishes `ghcr.io/gradata-systems/elasticsearch-fastmcp-server`, tagged by branch (`master`),
commit (`sha-<short>`) and release version (`0.1.1`, `0.1`). The image runs as a non-root user
(UID 10001), works with a read-only root filesystem, and bakes in `access_policy.yaml` and
`packs/` as defaults.

```
docker run -d --name es-mcp --read-only --tmpfs /tmp --cap-drop ALL -p 8443:8000 \
  -v "$PWD/tls:/etc/es-mcp/tls:ro" -v "$PWD/es-ca.crt:/etc/es-mcp/es-ca.crt:ro" \
  --env-file .env \
  -e ES_MCP_ES_CA_CERTS=/etc/es-mcp/es-ca.crt \
  -e ES_MCP_TLS_CERTFILE=/etc/es-mcp/tls/tls.crt -e ES_MCP_TLS_KEYFILE=/etc/es-mcp/tls/tls.key \
  ghcr.io/gradata-systems/elasticsearch-fastmcp-server:0.1.1
curl --cacert tls/tls.crt https://localhost:8443/healthz   # "ok"
```

To use your own policy or packs, mount them and point `ES_MCP_ACCESS_POLICY_FILE` or
`ES_MCP_PACKS_DIR` at them. To build the image yourself:

```
docker build -t <registry>/es-mcp:<tag> .
docker push <registry>/es-mcp:<tag>
```

## Deploying to Kubernetes

The Helm chart in `charts/es-mcp` runs the server with TLS terminated by the server itself. It
can therefore sit directly behind a `LoadBalancer` service (L4 pass-through) with no ingress
controller. Release tags also publish the chart to `oci://ghcr.io/gradata-systems/charts/es-mcp`,
so you can install from there instead of from a checkout.

### 1. Create the secrets

In the target namespace:

```
kubectl create namespace es-mcp
kubectl -n es-mcp create secret generic es-mcp-impersonator --from-literal=password='<generated>'
kubectl -n es-mcp create secret generic es-ca --from-file=ca.crt=es-ca.crt   # CA of the ES HTTPS cert
kubectl -n es-mcp create secret tls es-mcp-tls --cert=tls.crt --key=tls.key   # server certificate
```

The server certificate must cover the host name in `publicBaseUrl`. To have cert-manager issue it
instead, skip the last command and set `tls.certManager.enabled=true` with an `issuerRef`. The
certificate then goes into the secret `<fullname>-tls` (`es-mcp-tls` for a release named `es-mcp`)
and covers the host of `publicBaseUrl`, unless you set `tls.certManager.dnsNames`.

If the CA is in a ConfigMap rather than a secret, set `elasticsearch.ca.configMapName` instead of
`secretName`. Leave both empty to use the system CAs.

### 2. Write a values file

For example, `es-mcp-values.yaml`:

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

Leave the [audit log](audit-log.md) on stdout in Kubernetes and collect it with your log shipper.
The pod's filesystem doesn't survive restarts.

Rendering fails with a clear message if a required setting is missing: the URLs, the
impersonator secret or password, or a TLS source. `charts/es-mcp/values.yaml` documents every
setting.

### 3. Install, then point DNS at the load balancer

```
helm upgrade --install es-mcp charts/es-mcp -n es-mcp -f es-mcp-values.yaml
kubectl -n es-mcp get service es-mcp -o jsonpath='{.status.loadBalancer.ingress[0]}'
curl https://es-mcp.example.com/healthz     # "ok"; the MCP endpoint is /mcp
```

Or from the published chart:

```
helm upgrade --install es-mcp oci://ghcr.io/gradata-systems/charts/es-mcp --version 0.1.1 \
  -n es-mcp -f es-mcp-values.yaml
```

### Changing the policy or packs

`accessPolicy` replaces the image's `access_policy.yaml`:

```yaml
accessPolicy:
  exposed_indices: [ecs-microsoft-windows-*, ecs-fortios-*]
  impersonable_users: ["*.*", "service-account-*"]
```

`packs` replaces the built-in packs, so include every pack you want to keep:

```
helm upgrade es-mcp charts/es-mcp -n es-mcp -f es-mcp-values.yaml \
  --set-file 'packs.windows\.yaml=packs/windows.yaml' \
  --set-file 'packs.fortios\.yaml=packs/fortios.yaml'
```

Pods restart automatically when the policy, packs or a chart-managed password change.

### Things to know

- **Certificate renewal.** The server reads its certificate at startup. After the TLS secret is
  renewed, restart the server (`kubectl -n es-mcp rollout restart deployment es-mcp`), or add a
  reloader annotation such as `secret.reloader.stakater.com/reload` through
  `deploymentAnnotations`.
- **Replicas.** MCP sessions live in the memory of the replica that started them. With
  `replicaCount` above 1, set `statelessHttp: true` or `service.sessionAffinity: ClientIP`. The
  install notes warn when neither is set. `podDisruptionBudget.enabled` keeps a replica up during
  node drains.
- **Service options.** `service` accepts `type`, `port`, `nodePort`, `annotations`, `labels`,
  `loadBalancerIP`, `loadBalancerClass`, `loadBalancerSourceRanges`, `externalTrafficPolicy`,
  `internalTrafficPolicy`, `sessionAffinity(Config)`, `ipFamilyPolicy`, `ipFamilies` and
  `externalIPs`.
- **TLS elsewhere.** If TLS is terminated in front of the server, by an ingress or a
  TLS-terminating load balancer, set `tls.enabled: false`. The service then listens on port 80.
- **Probes.** `/healthz` is unauthenticated and doesn't depend on Elasticsearch or Keycloak, so an
  outage there doesn't restart the pods.
- **Hardening.** Pods run as a non-root user with a read-only root filesystem, no capabilities
  and no Kubernetes API token. The server never calls the Kubernetes API.

## Connecting clients

Clients connect to `<publicBaseUrl>/mcp` over streamable HTTP and authenticate with a Keycloak
access token.

- **Chat clients**, such as OpenWebUI: add an MCP server with that URL and OAuth authentication,
  using the client registered from `keycloak/client-chat.json`. Users sign in as themselves.
- **Agents**: get a token with client credentials (see [Keycloak setup](keycloak-setup.md#3-agents))
  and send it as `Authorization: Bearer <token>`.

A request without a valid token gets `401`. A quick check from the command line:

```
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://es-mcp.example.com/mcp   # 401
```
