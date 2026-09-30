# Deployment

Before deploying, set up [Elasticsearch](elasticsearch-setup.md) and your
[identity provider](identity-provider.md).
All settings are described in [Configuration](configuration.md).

- [Running locally](#running-locally)
- [Running the container](#running-the-container)
- [Deploying to Kubernetes](#deploying-to-kubernetes)
- [Several clusters](#several-clusters)
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
commit (`sha-<short>`) and release version (`0.5.0`, `0.5`). The image runs as a non-root user
(UID 10001), works with a read-only root filesystem, and bakes in `access_policy.yaml` as a
default. It includes no source packs, so it offers only the generic tools until you give it some.

```
docker run -d --name es-mcp --read-only --tmpfs /tmp --cap-drop ALL -p 8443:8000 \
  -v "$PWD/tls:/etc/es-mcp/tls:ro" -v "$PWD/es-ca.crt:/etc/es-mcp/es-ca.crt:ro" \
  --env-file .env \
  -e ES_MCP_ES_CA_CERTS=/etc/es-mcp/es-ca.crt \
  -e ES_MCP_TLS_CERTFILE=/etc/es-mcp/tls/tls.crt -e ES_MCP_TLS_KEYFILE=/etc/es-mcp/tls/tls.key \
  ghcr.io/gradata-systems/elasticsearch-fastmcp-server:0.5.0
curl --cacert tls/tls.crt https://localhost:8443/healthz   # "ok"
```

To use your own policy, or to add source packs, mount them and point `ES_MCP_ACCESS_POLICY_FILE`
or `ES_MCP_PACKS_DIR` at them. To build the image yourself:

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

If the identity provider's HTTPS certificate is also from a private CA, set `oidc.ca` in the same
way. It can point at the same secret. The server fetches the provider's signing keys over HTTPS;
without the CA, that fetch fails and every token is rejected as invalid.

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
oidc:
  issuer: https://keycloak.example.com/realms/security
  audience: es-mcp
  # ca: {secretName: es-ca}                 # if the provider's certificate is from a private CA
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
To write it to a file instead, set `audit.file.enabled`. The file is on an `emptyDir` and lost with
the pod, unless you also set `audit.file.persistence.enabled`. The chart then deploys a StatefulSet
rather than a Deployment, giving each replica (`es-mcp-0`, `es-mcp-1`, ...) its own
PersistentVolumeClaim, so each keeps its file across restarts and rollouts:

```yaml
audit:
  file:
    enabled: true
    persistence:
      enabled: true
      size: 5Gi
      storageClassName: standard
```

Turning persistence on or off for an existing release replaces the workload, restarting every
pod. The claims outlive the release; delete them yourself when the audit files are no longer
needed.

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
helm upgrade --install es-mcp oci://ghcr.io/gradata-systems/charts/es-mcp --version 0.5.0 \
  -n es-mcp -f es-mcp-values.yaml
```

### Changing the policy or packs

`accessPolicy` replaces the image's `access_policy.yaml`:

```yaml
accessPolicy:
  exposed_indices: [ecs-microsoft-windows-*, ecs-fortios-*]
  impersonable_users: ["*.*", "service-account-*"]
```

Packs come from `packs`, from existing ConfigMaps listed in `packsConfigMaps`, or both. With
neither, the server has no packs. The packs in the repository's `packs/` are examples and aren't
in the image; copy any you want to start from:

```
helm upgrade es-mcp charts/es-mcp -n es-mcp -f es-mcp-values.yaml \
  --set-file 'packs.windows\.yaml=packs/windows.yaml' \
  --set-file 'packs.fortios\.yaml=packs/fortios.yaml'
```

`packsConfigMaps` suits packs managed apart from the release, for example by different teams or
by GitOps. Each ConfigMap is in the release's namespace and holds one key per pack, named
`<name>.yaml`:

```
kubectl -n es-mcp create configmap siem-packs --from-file=packs/windows.yaml --from-file=packs/fortios.yaml
kubectl -n es-mcp create configmap team-packs --from-file=app_audit.yaml
```

```yaml
packsConfigMaps: [siem-packs, team-packs]
```

All the packs are mounted together in one directory, so file names must be unique across
`packs` and every listed ConfigMap; a duplicate stops the pods from starting. Keys that don't end
in `.yaml` are ignored. The server rejects an invalid pack, or a tool name used by two packs, at
startup.

Pods restart automatically when the policy, `packs` or a chart-managed password change. They
don't notice changes to the ConfigMaps in `packsConfigMaps`, because the server reads packs only
at startup. Restart them after changing one:

```
kubectl -n es-mcp rollout restart deployment/es-mcp
```

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
- **Probes.** `/healthz` is unauthenticated and doesn't depend on Elasticsearch or the identity
  provider, so an
  outage there doesn't restart the pods.
- **Hardening.** Pods run as a non-root user with a read-only root filesystem, no capabilities
  and no Kubernetes API token. The server never calls the Kubernetes API.

## Several clusters

One deployment serves one Elasticsearch cluster. To let the same agent, for example OpenWebUI,
query several clusters, run **one deployment per cluster** and connect the agent to each of them.
Each deployment keeps its own Elasticsearch URL, impersonation account, CA, access policy, packs
and audit trail, so one cluster's settings or credentials never affect another.

Give each deployment a name, a description and a tool prefix:

```yaml
# prod-values.yaml
publicBaseUrl: https://es-mcp-prod.example.com
cluster:
  name: Production SIEM
  description: Security logs for the head office (Windows, FortiGate, ingress-nginx)
toolPrefix: prod_
elasticsearch:
  url: https://prod-es-data-http.elastic.svc.prod:9200
  # ...
```

```yaml
# dr-values.yaml
publicBaseUrl: https://es-mcp-dr.example.com
cluster:
  name: DR site
  description: Security logs for the disaster recovery site (Windows only)
toolPrefix: dr_
elasticsearch:
  url: https://dr-es-data-http.elastic.svc.dr:9200
  # ...
```

```
helm upgrade --install es-mcp-prod charts/es-mcp -n es-mcp -f prod-values.yaml
helm upgrade --install es-mcp-dr charts/es-mcp -n es-mcp -f dr-values.yaml
```

Then add both servers to the agent. What each setting does for it:

- **Tool prefix.** Without it, both deployments offer `search_events`, and clients that don't keep
  tools from different servers apart may drop or mix them up. With it, the agent sees
  `prod_search_events` and `dr_search_events`. Hints and descriptions name the same
  deployment's tools, so an investigation doesn't drift between clusters partway through.
- **Cluster name and description.** The agent sees which cluster each tool queries, in the
  server instructions, in every tool description and in `list_data_sources`.
- **Asking when unclear.** The instructions and `list_data_sources` tell the agent to choose the
  cluster **only by its source packs**: the `sources` each deployment's `list_data_sources`
  returns for that user. Indices, aliases and data streams that no pack describes don't count.
  If packs on more than one cluster could hold what the user is asking about, or no pack clearly
  does, the agent asks the user which cluster they mean. For example:
  - A question about FortiGate traffic goes to `prod_`, because only production has the
    `fortios` pack.
  - A question about Windows logons, which both clusters' packs cover, prompts the agent to ask.
  - A question about data that only exists as an unpacked index on one cluster also prompts it
    to ask. To have such data chosen automatically, give it a pack.
- **Audit.** Every audit event carries the cluster name, so trails from several deployments can
  be collected in one place and still told apart.

Every deployment can share the identity provider's audience and client registrations. Each
cluster's own Elasticsearch roles decide what a user can see there (see
[Identity provider setup](identity-provider.md#several-deployments)).

## Connecting clients

Clients connect to `<publicBaseUrl>/mcp` over streamable HTTP and authenticate with an access
token from the identity provider.

- **Chat clients**, such as OpenWebUI: add an MCP server with that URL and OAuth authentication,
  using the client registered for it (on Keycloak, from `keycloak/client-chat.json`). Users sign
  in as themselves.
- **Agents**: get a token with client credentials (see
  [Identity provider setup](identity-provider.md#3-agents)) and send it as
  `Authorization: Bearer <token>`.

A request without a valid token gets `401`. A quick check from the command line:

```
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://es-mcp.example.com/mcp   # 401
```
