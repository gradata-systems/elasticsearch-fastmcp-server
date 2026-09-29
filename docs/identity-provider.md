# Identity provider setup

Clients authenticate with an OpenID Connect provider, such as Keycloak, Microsoft Entra ID, Okta,
Auth0 or Authentik, and send its access token to the server. The server is an OAuth resource
server only: it verifies tokens, and never sees passwords or client secrets. Register each client
with the provider up front. For Keycloak, see the ready-made definitions in
[Keycloak setup](keycloak-setup.md).

## What the server accepts

A token is accepted only when all of these hold:

- It is a JWT signed with one of the provider's keys, using `ES_MCP_OIDC_TOKEN_ALGORITHM`
  (`RS256` by default). Providers that issue opaque access tokens aren't supported.
- It is issued by `ES_MCP_OIDC_ISSUER`. The `iss` claim must match exactly, including any trailing
  slash, so copy `issuer` from the provider's `/.well-known/openid-configuration`.
- It has the audience `ES_MCP_OIDC_AUDIENCE` (e.g. `aud: es-mcp`).
- It hasn't expired.
- Its `preferred_username` (or the claim set in `ES_MCP_USERNAME_CLAIM`) names an impersonable user
  (see [Access control](access-control.md)).

No particular OAuth scope is required.

The server finds the provider's signing keys through `<issuer>/.well-known/openid-configuration`,
on the first request rather than at startup, and refuses a discovery document whose `issuer`
differs from `ES_MCP_OIDC_ISSUER`. If the provider has no discovery document at that address, set
`ES_MCP_OIDC_JWKS_URI` to its JWKS URL instead.

The server also names the issuer as its authorization server in its OAuth protected resource
metadata (RFC 9728), so MCP clients that follow the MCP authorization spec know where to sign in.

## 1. Audience and username

Configure the provider so every token for this server carries:

- **The audience.** Usually an API or resource registered with the provider, or a scope or mapper
  that adds `aud`. On Keycloak this is the `es-mcp` client scope; on Entra ID, the application ID
  URI of an app registration for the server.
- **The username.** A claim holding the caller's Elasticsearch username. Set
  `ES_MCP_USERNAME_CLAIM` if it isn't `preferred_username`, e.g. `upn`, `email` or a custom claim.

## 2. Chat clients

Register OpenWebUI and other chat clients as clients using the authorization code flow with PKCE
(S256), with the exact callback URL the client shows when you configure the MCP server's OAuth
settings. Users sign in as themselves, so the username claim is their own account name, e.g.
`john.smith1`, and queries run with their own Elasticsearch roles.

## 3. Agents

Register each automation agent as a client using the client credentials flow. Its token's username
claim names the agent, e.g. `service-account-langgraph-triage` on Keycloak. Create the matching
Elasticsearch user described in [Elasticsearch setup](elasticsearch-setup.md#3-agents), and make
sure `impersonable_users` in `access_policy.yaml` covers the name.

The agent sends the `access_token` it gets as `Authorization: Bearer <token>` to
`https://es-mcp.example.com/mcp`.

## 4. Lock down usernames

Access is decided by the username claim. If users can change the value it comes from, they can
impersonate others. Use a claim only administrators can set, e.g. usernames taken read-only from
LDAP/AD, and never one users edit in their profile, such as a display name or, on most providers,
an unverified email address.

## Several deployments

When you run one deployment per Elasticsearch cluster (see
[Several clusters](deployment.md#several-clusters)), all of them can use the same audience and
the same client registrations. A token issued for one deployment is then accepted by the others.
That's intended: the server adds no access of its own, and each cluster applies the user's own
roles for that cluster. A user without an account or role on a cluster gets `run_as_denied` or
`elasticsearch_403` there.

If you ever need a token to work only at one deployment, give that deployment its own audience
(`ES_MCP_OIDC_AUDIENCE`).

Chat clients register each deployment as a separate MCP server. Add each deployment's callback
URL to the chat client's registration if the client shows a different one per server.

## Troubleshooting "invalid token"

Every rejected token gets the same `401 invalid_token` response. The reason is in the server's
log, as a line starting `Bearer token rejected`:

```
kubectl -n es-mcp logs deploy/es-mcp | grep "Bearer token rejected"
```

| Log message | Cause | Fix |
|---|---|---|
| `couldn't get its signing key from …: … CERTIFICATE_VERIFY_FAILED` | The provider's HTTPS certificate is from a CA the server doesn't trust. | Set `ES_MCP_OIDC_CA_CERTS` (chart: `oidc.ca`). |
| `couldn't get its signing key from …` with a connection error | The server can't reach the provider. | Allow the pod to reach the issuer URL, or the JWKS URL if set. |
| `couldn't get its signing key from …/.well-known/openid-configuration: … 404` | The provider has no discovery document under the issuer. | Set `ES_MCP_OIDC_JWKS_URI` (chart: `oidc.jwksUri`). |
| `discovery document's issuer is …, expected …` | `ES_MCP_OIDC_ISSUER` differs from the provider's own issuer, often by a trailing slash. | Copy `issuer` from the discovery document. |
| `signed with ES256, but only RS256 is accepted` | The provider signs access tokens with another algorithm. | Set `ES_MCP_OIDC_TOKEN_ALGORITHM` (chart: `oidc.tokenAlgorithm`) to match. |
| `issuer mismatch (got …, expected …)` | The token came from a different issuer, or through a different URL: some providers, Keycloak among them, write the URL they were reached through into `iss`. | Have clients reach the provider through its public URL, or fix the provider's configured hostname. |
| `audience mismatch (got …, expected …)` | The client's tokens don't carry the audience. | See [step 1](#1-audience-and-username). |
| `token expired` | The client sent an access token past its lifetime. | Make the client refresh its tokens, or lengthen the access token lifetime. |

Decode the token the client actually sends to see its header and claims: the first two parts of
the JWT are base64url JSON. Check `alg`, `iss`, `aud`, `exp` and the username claim.

If no `Bearer token rejected` line appears at all, the request didn't carry a token the server
could read. Set `FASTMCP_LOG_LEVEL=DEBUG` (chart: `extraEnv`) to see more.
