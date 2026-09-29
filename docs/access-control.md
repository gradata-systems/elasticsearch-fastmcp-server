# Access control

The server adds no permissions of its own. Every Elasticsearch request runs as the person or
agent who called the tool, with their own Elasticsearch roles, and the server can only narrow
what they reach.

## How a request is authorized

1. **Authentication.** The client signs in with the OpenID Connect provider and sends the access
   token with each MCP request. The server checks the token's signature against the provider's
   JWKS, and checks that its issuer is `ES_MCP_OIDC_ISSUER` and its audience is
   `ES_MCP_OIDC_AUDIENCE` (e.g. `es-mcp`).
   No particular OAuth scope is required.
2. **Identity.** The token's username claim (`preferred_username` by default,
   `ES_MCP_USERNAME_CLAIM`) must name an Elasticsearch user of the same name: a person such as
   `john.smith1`, or an agent's name, such as `service-account-<client-id>` on Keycloak.
3. **Impersonation check.** The username must match a pattern in `impersonable_users` in
   `access_policy.yaml`. Some names are always refused, whatever the patterns say:
   - built-in accounts: `elastic`, `kibana`, `kibana_system`, `logstash_system`, `beats_system`,
     `apm_system`, `remote_monitoring_user`
   - names starting with `_`
   - the impersonation account itself
   - malformed names: anything other than letters, digits and `._@-`, or longer than 256 characters
4. **Index check.** Every index the tool would read must match `exposed_indices`. For a
   multi-target expression such as `a-*,-a-debug-*`, each included target is checked. An
   expression can't start with an exclusion.
5. **Run-as.** The server logs in to Elasticsearch as its impersonation account and sends
   `es-security-runas-user: <username>`. Elasticsearch then applies **that user's own roles**,
   the same ones they have in Kibana. The impersonation account has no index privileges, so a
   request can never see more than the user could see directly.

Each step that refuses a request writes an `access_denied` event to the
[audit log](audit-log.md).

## access_policy.yaml

```yaml
# Indices this server exposes. Each caller sees only the overlap of these patterns and what
# their own Elasticsearch roles allow; ES enforces the latter.
exposed_indices:
  - ecs-microsoft-windows-*
  - ecs-ingress-nginx-*
  - ecs-fortios-*

# Usernames (the token's preferred_username) the server may run as. Keep in step with the
# impersonation account's run_as privilege. Built-in accounts such as 'elastic' are always refused.
impersonable_users:
  - "*.*"                 # people, e.g. john.smith1
  - "service-account-*"   # e.g. Keycloak client-credentials agents
```

Both lists are shell-style patterns (`*`, `?`, `[...]`) and both must be non-empty. Set the file's
path with `ES_MCP_ACCESS_POLICY_FILE`. In Kubernetes, the chart's `accessPolicy` value replaces it
(see [Deployment](deployment.md#changing-the-policy-or-packs)).

### `exposed_indices`

This keeps data that users can see in Kibana, but that isn't meant for MCP, out of reach of
agents. It **narrows scope; it isn't the security boundary**. The user's Elasticsearch roles
are. Things to know:

- `list_data_sources` shows only the indices, aliases and data streams that match these patterns
  *and* that the caller can read.
- Source packs whose index isn't covered are skipped at startup, with a warning in the log.
- For ES|QL, the indices in `FROM` and in every `LOOKUP JOIN` are checked. Queries whose index
  list can't be parsed with confidence are refused rather than allowed: subqueries or comments
  in the `FROM` clause, or a query that doesn't start with `FROM`.

### `impersonable_users`

This should match the `run_as` privilege of the impersonation account (see
[Elasticsearch setup](elasticsearch-setup.md)). Elasticsearch enforces `run_as` too, so keeping
the two lists in step means the server refuses a disallowed name itself, with a clear audit
event, rather than passing it to Elasticsearch.

## Examples

| Caller | Request | Outcome |
|---|---|---|
| `john.smith1`, Kibana role reads `ecs-microsoft-windows-*` | `search_events` on `ecs-microsoft-windows-*` | runs as `john.smith1` |
| `john.smith1` | `search_events` on `hr-payroll` | refused by the server: `index_not_exposed` |
| `john.smith1`, no role on `ecs-fortios-*` | `top_values` on `ecs-fortios-*` | refused by Elasticsearch: `elasticsearch_403` |
| token with `preferred_username: elastic` | any | refused: `username_not_impersonable` |
| `service-account-triage`, no matching ES user | any | refused: `run_as_denied` |

## Licence

On a Basic licence only index-level control is possible; document- and field-level security need
Platinum. If some users may see only part of an index, give them their own index or alias and
expose that.
