from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Server configuration, read from ES_MCP_* environment variables (or a .env file)."""

    model_config = SettingsConfigDict(env_prefix='ES_MCP_', env_file='.env', extra='ignore')

    # Elasticsearch. The broker account is a native ES user whose only job is to mint
    # short-lived, index-scoped API keys; it is never used to run searches itself.
    es_url: str
    es_ca_certs: Path | None = None
    es_broker_username: str
    es_broker_password: SecretStr
    es_request_timeout: float = 30.0
    es_windows_index: str = 'ecs-microsoft-windows-v1'
    max_result_size: int = 500

    # RBAC
    rbac_policy_file: Path = Path('rbac.yaml')
    api_key_lifetime_minutes: int = 60

    # Audit trail as JSON lines; stdout when unset (suits Kubernetes log shipping).
    audit_log_file: Path | None = None

    # Keycloak (OAuth2 authorization server)
    keycloak_realm_url: str
    keycloak_audience: str
    # Client whose client roles (resource_access.<id>.roles) are mapped to indices.
    # If unset, realm roles (realm_access.roles) are used instead.
    keycloak_roles_client_id: str | None = None
    public_base_url: str

    # HTTP listener. Leave TLS unset only when TLS is terminated in front of the server.
    host: str = '0.0.0.0'
    port: int = 8000
    tls_certfile: Path | None = None
    tls_keyfile: Path | None = None
