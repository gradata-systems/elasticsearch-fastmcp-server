import fnmatch
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class RolePolicy(BaseModel):
    indices: list[str] = Field(min_length=1, description="Index patterns this role may read.")


class RbacPolicy(BaseModel):
    """Maps Keycloak roles to the Elasticsearch index patterns they may read."""

    roles: dict[str, RolePolicy]

    @classmethod
    def load(cls, path: Path) -> 'RbacPolicy':
        with path.open(encoding='utf-8') as f:
            return cls.model_validate(yaml.safe_load(f))

    def index_patterns_for(self, roles: set[str]) -> frozenset[str]:
        return frozenset(
            pattern
            for role in roles if role in self.roles
            for pattern in self.roles[role].indices
        )


@dataclass(frozen=True)
class Caller:
    subject: str
    roles: frozenset[str]
    index_patterns: frozenset[str]

    def may_read(self, index: str) -> bool:
        """Advisory check for friendlier errors; Elasticsearch enforces the real boundary."""
        parts = index.split(',')
        return all(
            part and any(fnmatch.fnmatchcase(part, pattern) for pattern in self.index_patterns)
            for part in parts
        )


def roles_from_claims(claims: dict[str, Any], client_id: str | None) -> frozenset[str]:
    """Extract Keycloak roles from access-token claims.

    Uses client roles of `client_id` when given, otherwise realm roles. Only one source is
    read so that a realm role can't accidentally grant access meant for a client role.
    """
    if client_id:
        container = (claims.get('resource_access') or {}).get(client_id) or {}
    else:
        container = claims.get('realm_access') or {}
    roles = container.get('roles') or []
    return frozenset(r for r in roles if isinstance(r, str))
