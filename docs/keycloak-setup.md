# Keycloak setup (26.5)

Clients authenticate with Keycloak and send the access token to the server. FastMCP's Keycloak
integration needs Keycloak 26.6 or later for MCP clients to register themselves dynamically, so on
26.5 **register each client in Keycloak up front**.

## What the server accepts

A token is accepted only when all of these hold:

- It is issued by `ES_MCP_KEYCLOAK_REALM_URL`. The `iss` claim must match exactly, so use the
  realm's public URL, e.g. `https://keycloak.example.com/realms/security`.
- It has the audience `ES_MCP_KEYCLOAK_AUDIENCE` (`aud: es-mcp`).
- Its `preferred_username` (or the claim set in `ES_MCP_USERNAME_CLAIM`) names an impersonable user
  (see [Access control](access-control.md)).

No particular OAuth scope is required. The `es-mcp` client scope supplies both the audience and
the username, so it must be a **default** scope on every client that calls the server.

## Definitions in `keycloak/`

Importable definitions, tested against Keycloak 26.5:

| File | What it is |
|---|---|
| `client-scope-es-mcp.json` | The `es-mcp` client scope. Adds `aud: es-mcp` and the `preferred_username` claim. |
| `client-chat.json` | Template for OpenWebUI and other chat clients: authorization code + PKCE (S256). |
| `client-agent.json` | Template for one automation agent: client credentials via a service account. |

## 1. Create the client scope and clients

`kcadm.sh` ships in the Keycloak image:

```
kcadm.sh config credentials --server https://keycloak.example.com --realm master --user admin
kcadm.sh create client-scopes -r security -f keycloak/client-scope-es-mcp.json
kcadm.sh create clients -r security -f keycloak/client-chat.json    # edit clientId and redirectUris first
kcadm.sh create clients -r security -f keycloak/client-agent.json   # one per agent; edit clientId
```

The admin console's *Clients → Import client* also accepts the two client files. Client scopes
have no import button, though, and must exist before the clients that use them.

For an **existing client**, add `es-mcp` as a default client scope instead: *Clients → client →
Client scopes → Add client scope → Default*. A token without it has no `es-mcp` audience and no
username, and is rejected.

## 2. Chat clients

- Replace the placeholder in `redirectUris` with the exact callback URL the client shows when you
  configure the MCP server's OAuth settings.
- Keep the client confidential, with PKCE. Set `publicClient: true` only for clients that can't
  keep a secret.
- Users sign in as themselves, so `preferred_username` is their own account name, e.g.
  `john.smith1`, and queries run with their own Elasticsearch roles.

## 3. Agents

Each agent's service account is named `service-account-<clientId>`, e.g.
`service-account-langgraph-triage`. Create the matching Elasticsearch user described in
[Elasticsearch setup](elasticsearch-setup.md#3-agents). The agent gets a token with a plain
client-credentials request:

```
curl https://keycloak.example.com/realms/security/protocol/openid-connect/token \
  -d grant_type=client_credentials -d client_id=langgraph-triage -d client_secret=...
```

It then sends the `access_token` from the response as `Authorization: Bearer <token>` to
`https://es-mcp.example.com/mcp`.

To check a token, decode its payload (the middle part, base64url) and confirm that `iss` is the
realm URL, `aud` includes `es-mcp`, and `preferred_username` is `service-account-<clientId>`.

## 4. Lock down usernames

Access is decided by the username claim. If users can rename themselves, they can impersonate
others. Keep the realm's *Edit username* setting off (`editUsernameAllowed: false`), or take
usernames read-only from LDAP/AD.

## Several deployments

When you run one deployment per Elasticsearch cluster (see
[Several clusters](deployment.md#several-clusters)), all of them can use the same `es-mcp`
audience and client scope, and the same chat and agent clients. A token issued for one deployment
is then accepted by the others. That's intended: the server adds no access of its own, and each
cluster applies the user's own roles for that cluster. A user without an account or role on a
cluster gets `run_as_denied` or `elasticsearch_403` there.

If you ever need a token to work only at one deployment, give that deployment its own audience
(`ES_MCP_KEYCLOAK_AUDIENCE`) and a client scope that adds it.

Chat clients register each deployment as a separate MCP server. Add each deployment's callback
URL to the chat client's `redirectUris` if the client shows a different one per server.
