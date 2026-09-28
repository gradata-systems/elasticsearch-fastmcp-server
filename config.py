from pathlib import Path

from pydantic import SecretStr
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
    es_request_timeout: float = 30.0
    max_result_size: int = 500
    # Upper bound on serialized tool output, to protect the model's context window.
    max_response_chars: int = 100_000
    max_time_range_days: int = 90
    # Longer limit for tools that return counts rather than events (top values, distinct values,
    # period comparisons, ES|QL with STATS): aggregating a long period is cheap for Elasticsearch.
    max_aggregation_range_days: int = 366

    # Exposed indices and impersonable usernames
    access_policy_file: Path = Path('access_policy.yaml')
    # Source packs (*.yaml) describing data sources and their curated tools
    packs_dir: Path = Path('packs')

    # Audit trail as JSON lines; stdout when unset (suits Kubernetes log shipping).
    audit_log_file: Path | None = None

    # Keycloak (OAuth2 authorization server)
    keycloak_realm_url: str
    keycloak_audience: str
    # Token claim holding the caller's Elasticsearch username. Must not be user-editable.
    username_claim: str = 'preferred_username'
    public_base_url: str

    # HTTP listener. Leave TLS unset only when TLS is terminated in front of the server.
    host: str = '0.0.0.0'
    port: int = 8000
    tls_certfile: Path | None = None
    tls_keyfile: Path | None = None
