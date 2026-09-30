"""Extracts the indices an ES|QL query reads, so they can be checked against the exposed patterns.

Deliberately conservative: anything it can't confidently parse is rejected rather than allowed.
Elasticsearch still enforces the user's own privileges either way.
"""
import re

_LEADING_COMMENTS = re.compile(r'^(?:\s+|//[^\n]*\n|/\*.*?\*/)*', re.DOTALL)
_SOURCE = re.compile(r'^(FROM|TS)\s+(.*?)(?=\s+METADATA\b|\||$)', re.IGNORECASE | re.DOTALL)
_LOOKUP_JOIN = re.compile(r'\bLOOKUP\s+JOIN\s+([^\s|]+)', re.IGNORECASE)
_STRING_LITERAL = re.compile(r'"""(?:.|\n)*?"""|"(?:[^"\\\n]|\\.)*"')
# STATS as a command of its own; INLINE STATS keeps every row, so it doesn't count.
_STATS = re.compile(r'\|\s*STATS\b', re.IGNORECASE)
# A string literal where a field name belongs: grouped by, or on the left of a comparison.
_BY = re.compile(r'\bBY\s*$', re.IGNORECASE)
_COMPARISON = re.compile(r'\s*(?:==|!=|<|>|(?:NOT\s+)?(?:R?LIKE|IN)\b|IS\b)', re.IGNORECASE)


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


def aggregates(query: str) -> bool:
    """Whether `query` has a STATS command, so it returns counts rather than events.

    Only widens the allowed time range; results are capped at the same number of rows either way.
    """
    return bool(_STATS.search(_STRING_LITERAL.sub('""', query)))


def quoted_field_names(query: str) -> list[str]:
    """String literals `query` uses where a field name belongs, e.g. `WHERE "host.name" == "a"`.

    ES|QL reads double quotes as a string, so such a query runs but compares or groups by a constant.
    """
    return [m.group() for m in _STRING_LITERAL.finditer(query)
            if _COMPARISON.match(query, m.end()) or _BY.search(query, 0, m.start())]
