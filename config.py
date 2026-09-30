from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Server configuration, read from ES_MCP_* environment variables (or a .env file)."""

    model_config = SettingsConfigDict(env_prefix='ES_MCP_', env_file='.env', extra='ignore')

    # Elasticsearch. The impersonation account is a native ES user with only the run_as
    # privilege; every request runs as the caller's own ES user.
    es_url: str
    es_ca_certs: Path | None = None
    es_impersonator_username: str
    es_impersonator_password: SecretStr
    es_request_timeout: float = Field(default=30.0, gt=0)
    # Most rows any tool returns.
    max_result_size: int = Field(default=500, ge=1)
    # Upper bound on serialized tool output, to protect the model's context window.
    max_response_chars: int = Field(default=100_000, ge=1000)
    max_time_range_days: int = Field(default=90, ge=1)
    # Longer limit for tools that return counts rather than events (top values, distinct values,
    # period comparisons, ES|QL with STATS): aggregating a long period is cheap for Elasticsearch.
    max_aggregation_range_days: int = Field(default=366, ge=1)
    # Longest a whole tool call may take, however many Elasticsearch requests it makes.
    tool_timeout: float = Field(default=60.0, gt=0)
    # Identical calls (same caller, tool and arguments) allowed within the window; more are refused.
    max_repeated_calls: int = Field(default=3, ge=1)
    repeated_call_window_seconds: int = Field(default=300, ge=1)

    # Identity of this deployment, which serves one cluster. Agents may use several deployments at
    # once, one per cluster: the name and description tell them apart, and the prefix keeps tool names
    # distinct in clients that don't separate tools by server, e.g. 'prod_' for 'prod_search_events'.
    cluster_name: str = ''
    cluster_description: str = ''
    tool_prefix: str = Field(default='', pattern=r'^([a-z][a-z0-9_]*_)?$')

    # Exposed indices and impersonable usernames
    access_policy_file: Path = Path('access_policy.yaml')
    # Source packs (*.yaml) describing data sources and their curated tools
    packs_dir: Path = Path('packs')

    # Audit trail as JSON lines; stdout when unset (suits Kubernetes log shipping).
    audit_log_file: Path | None = None

    # OpenID Connect provider that issues access tokens. The issuer must equal the tokens' `iss`
    # exactly; the JWKS URI is found through the issuer's discovery document unless set.
    oidc_issuer: str
    oidc_audience: str
    oidc_jwks_uri: str | None = None
    # CA that signed the provider's HTTPS certificate, trusted in addition to the system CAs when
    # fetching its discovery document and signing keys. Needed when the provider uses a private CA.
    oidc_ca_certs: Path | None = None
    # Algorithm the provider signs access tokens with.
    oidc_token_algorithm: str = 'RS256'
    # Token claim holding the caller's Elasticsearch username. Must not be user-editable.
    username_claim: str = 'preferred_username'
    public_base_url: str

    # HTTP listener. Leave TLS unset only when TLS is terminated in front of the server.
    host: str = '0.0.0.0'
    port: int = 8000
    tls_certfile: Path | None = None
    tls_keyfile: Path | None = None
