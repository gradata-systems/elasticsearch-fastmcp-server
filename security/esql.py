"""Extracts the indices an ES|QL query reads, so they can be checked against the exposed patterns.

Deliberately conservative: anything it can't confidently parse is rejected rather than allowed.
Elasticsearch still enforces the user's own privileges either way.
"""
import re

_LEADING_COMMENTS = re.compile(r'^(?:\s+|//[^\n]*\n|/\*.*?\*/)*', re.DOTALL)
_SOURCE = re.compile(r'^(FROM|TS)\s+(.*?)(?=\s+METADATA\b|\||$)', re.IGNORECASE | re.DOTALL)
_LOOKUP_JOIN = re.compile(r'\bLOOKUP\s+JOIN\s+([^\s|]+)', re.IGNORECASE)


class UnsupportedQuery(ValueError):
    pass


def _index_names(section: str) -> list[str]:
    if any(token in section for token in ('(', '//', '/*')):
        raise UnsupportedQuery("Subqueries and comments in the FROM clause are not supported")
    names = [name.strip().strip('"`') for name in section.split(',')]
    if not all(names):
        raise UnsupportedQuery("Could not parse the index list in the FROM clause")
    return names


def source_indices(query: str) -> list[str]:
    """Indices read by `query`'s FROM/TS source command and any LOOKUP JOINs."""
    body = _LEADING_COMMENTS.sub('', query, count=1)
    source = _SOURCE.match(body)
    if not source:
        raise UnsupportedQuery("ES|QL queries must start with FROM")
    return _index_names(source.group(2)) + [m.strip('"`') for m in _LOOKUP_JOIN.findall(body)]
