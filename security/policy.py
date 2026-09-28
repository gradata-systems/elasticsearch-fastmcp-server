import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

# Built-in accounts that must never be impersonated, whatever the configured patterns say.
RESERVED_USERS = frozenset({
    'elastic', 'kibana', 'kibana_system', 'logstash_system', 'beats_system', 'apm_system',
    'remote_monitoring_user',
})
# Printable, no whitespace or commas: rules out header tricks and multi-user values.
_USERNAME = re.compile(r'^[A-Za-z0-9._@\-]{1,256}$')


def _matches(value: str, patterns: list[str] | frozenset[str]) -> bool:
    return any(fnmatch.fnmatchcase(value, pattern) for pattern in patterns)


class AccessPolicy(BaseModel):
    """Server-side limits applied on top of each user's own Elasticsearch privileges."""

    exposed_indices: list[str] = Field(
        min_length=1,
        description="Index patterns this server exposes. A user sees the overlap of these and their ES roles.")
    impersonable_users: list[str] = Field(
        min_length=1,
        description="Usernames the server may run as. Should mirror the impersonation account's run_as.")

    @classmethod
    def load(cls, path: Path) -> 'AccessPolicy':
        with path.open(encoding='utf-8') as f:
            return cls.model_validate(yaml.safe_load(f))

    def may_impersonate(self, username: str, impersonator: str) -> bool:
        return (
            bool(_USERNAME.match(username))
            and not username.startswith('_')
            and username not in RESERVED_USERS
            and username != impersonator
            and _matches(username, self.impersonable_users)
        )


@dataclass(frozen=True)
class Caller:
    subject: str
    username: str
    index_patterns: frozenset[str]

    def may_read(self, index: str) -> bool:
        """Whether every comma-separated target is within the exposed patterns.

        This narrows what the server exposes; the user's ES roles remain the security boundary.
        """
        return all(part and _matches(part, self.index_patterns) for part in index.split(','))
