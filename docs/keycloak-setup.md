# Keycloak setup (26.5)

Keycloak is one OpenID Connect provider the server works with. This page covers the
Keycloak-specific steps; [Identity provider setup](identity-provider.md) describes what the server
requires of any provider, and how to troubleshoot rejected tokens.

Set `ES_MCP_OIDC_ISSUER` to the realm's public URL, e.g.
`https://keycloak.example.com/realms/security`, and `ES_MCP_OIDC_AUDIENCE` to `es-mcp`. The server
finds the realm's signing keys through its discovery document. MCP clients can only register
themselves dynamically on Keycloak 26.6 or later, so on 26.5 **register each client up front**.

The `es-mcp` client scope supplies both the audience and the username, so it must be a
**default** scope on every client that calls the server.

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

Every deployment can share the `es-mcp` client scope and the chat and agent clients (see
[Several deployments](identity-provider.md#several-deployments)). To limit a token to one
deployment, give that deployment its own audience and a client scope that adds it.

## Keycloak-specific causes of "invalid token"

See [Troubleshooting](identity-provider.md#troubleshooting-invalid-token) for the log messages.
On Keycloak:

- `issuer mismatch`: Keycloak writes the URL it was reached through into `iss`. Have clients reach
  it through the realm's public URL, or set Keycloak's hostname (`KC_HOSTNAME`) so `iss` is always
  the public URL.
- `audience mismatch`: the `es-mcp` client scope isn't a default scope of the client that got the
  token. Add it (see [step 1](#1-create-the-client-scope-and-clients)).
- `signed with …, but only RS256 is accepted`: set `ES_MCP_OIDC_TOKEN_ALGORITHM` to the realm's,
  or the client's, *Access token signature algorithm*.
- `token expired`: access tokens last 5 minutes by default; the realm's *Access Token Lifespan*
  sets it.
