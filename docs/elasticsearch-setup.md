# Elasticsearch setup

The server needs one impersonation account of its own, plus an Elasticsearch user for every
person and agent who will call it.

## 1. The impersonation account

Create a role that can only run as other users, and a native user holding it:

```
POST /_security/role/mcp_impersonator
{ "cluster": [], "indices": [], "run_as": ["*.*", "service-account-*"] }

POST /_security/user/mcp_impersonator
{ "password": "<generated>", "roles": ["mcp_impersonator"] }
```

- **Keep `run_as` narrow.** List only the name patterns of people and agents. Never use `["*"]`,
  which would allow impersonating `elastic` and other built-in accounts.
- **Use a separate account** from any Kibana proxy or other service, so its use shows up on its own.
- **It must be a native user with a password.** Run-as isn't available to API keys.
- **Mirror `run_as` in `impersonable_users`** in `access_policy.yaml` (see
  [Access control](access-control.md#impersonable_users)).

Give the server its credentials with `ES_MCP_ES_IMPERSONATOR_USERNAME` and
`ES_MCP_ES_IMPERSONATOR_PASSWORD`, or through a Kubernetes secret (see [Deployment](deployment.md)).

## 2. People

Nothing to do if users already have Elasticsearch accounts with the same names as their identity
provider usernames, for example through the same LDAP/AD source. Their existing roles decide what they can
query. If a user's username has no Elasticsearch user of the same name, their requests
are refused with `run_as_denied`.

## 3. Agents

Each automation agent signs in to the identity provider with client credentials. Its username is
set by the provider, e.g. the service account `service-account-<client-id>` on Keycloak (see
[Identity provider setup](identity-provider.md#3-agents)). Create a matching native
user:

```
POST /_security/role/es_mcp_triage_agent
{
  "indices": [
    { "names": ["ecs-microsoft-windows-*", "ecs-fortios-*"],
      "privileges": ["read", "view_index_metadata"] }
  ]
}

POST /_security/user/service-account-langgraph-triage
{ "password": "<long random value, never used>", "roles": ["es_mcp_triage_agent"] }
```

- Give each agent the narrowest role it needs. `read` covers searches, aggregations, field
  capabilities (`describe_fields`) and ES|QL. `list_data_sources` resolves index names, which
  needs `view_index_metadata`.
- The password is never used, because the server runs as the user rather than logging in as it.
  Make it long and random, and don't store it.
- **Don't disable the user.** Disabled users can't be run as.

## 4. Check it

Run a request as a user through the impersonation account:

```
curl -u mcp_impersonator:<password> -H 'es-security-runas-user: john.smith1' \
  'https://es.example.com:9200/ecs-microsoft-windows-*/_search?size=0'
```

A 200 response means the setup works. A 403 that mentions `run as` means the `run_as` list doesn't
cover the name. A 403 about indices means the user's own roles don't allow the index.
