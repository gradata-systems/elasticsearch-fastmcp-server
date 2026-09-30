"""Checks the fields and Lucene query a tool call names before it runs.

Elasticsearch rejects malformed requests itself, but some mistakes are valid requests that just match
nothing, or more than intended: a field that doesn't exist, an exact match on an analysed text field,
or a lowercase 'and' that Lucene searches for as a word. These checks turn them into errors that say
how to fix them.
"""
import difflib
import re
from typing import Any, Iterable

from fastmcp.exceptions import ToolError

from tools.query import Filter
from utils.elasticsearch import ElasticsearchGateway

_ESCAPED = re.compile(r'\\.')
# Quoted phrases, ranges and regular expressions hold values, not syntax.
_LUCENE_VALUES = re.compile(r'"[^"]*"|\[[^\]]*\]|\{[^}]*\}|/[^/]*/')
_TERM_START = r'(?:^|(?<=[\s(!+]))-?'
_LUCENE_FIELD = re.compile(_TERM_START + r'([\w@][\w.@-]*)\s*:')
_EXISTS = re.compile(r'\b_exists_\s*:\s*([\w@][\w.@-]*)')
_LOWERCASE_OPERATOR = re.compile(r'(?:^|(?<=[\s(]))(and|or|not)(?=[\s(])')
_COMPARISON = re.compile(_TERM_START + r'[\w@][\w.@-]*\s*(==?|!=|[<>]=?)')

_TEXT_TYPES = {'text', 'match_only_text'}
_DATE_TYPES = {'date', 'date_nanos'}
_OBJECT_TYPES = {'object', 'nested'}
# Filter operators that compare whole values, which an analysed text field doesn't hold.
_EXACT_OPS = {'eq', 'in', 'prefix'}


def _lucene_syntax(query: str) -> str:
    return _LUCENE_VALUES.sub(' ', _ESCAPED.sub('x', query))


def lucene_problems(query: str | None) -> list[str]:
    """Ways a Lucene query parses but doesn't mean what it appears to."""
    if not query:
        return []
    syntax = _lucene_syntax(query)
    problems = []
    if operator := _LOWERCASE_OPERATOR.search(syntax):
        word = operator.group(1)
        problems.append(f"Lucene searches for a lowercase '{word}' as a word; write {word.upper()} for the "
                        f"operator, or quote it to search for the word.")
    if comparison := _COMPARISON.search(syntax):
        problems.append(f"Lucene has no '{comparison.group(1)}' operator; write field:value, "
                        f"NOT field:value or a range such as field:>=10.")
    return problems


def check_lucene(query: str | None) -> None:
    if problems := lucene_problems(query):
        raise ToolError(' '.join(problems))


def lucene_fields(query: str | None) -> set[str]:
    """Fields a Lucene query names, other than wildcards and metadata fields."""
    if not query:
        return set()
    syntax = _lucene_syntax(query)
    return {f for f in _LUCENE_FIELD.findall(syntax) if not f.startswith('_')} | set(_EXISTS.findall(syntax))


def _subfields(name: str, mapped: dict[str, dict[str, Any]], aggregatable: bool = False) -> list[str]:
    depth = name.count('.') + 1
    return sorted(f for f, caps in mapped.items()
                  if f.startswith(name + '.') and f.count('.') == depth
                  and (not aggregatable or any(c.get('aggregatable') for c in caps.values())))


def _instead(name: str, alternatives: list[str], otherwise: str) -> str:
    return f"use {' or '.join(repr(a) for a in alternatives)}" if alternatives else otherwise


def _suggestions(name: str, known: list[str]) -> list[str]:
    leaf = name.rsplit('.', 1)[-1].lower()
    ranked = ([f for f in known if f.lower() == name.lower()]
              + difflib.get_close_matches(name, known, n=3, cutoff=0.7)
              + sorted(f for f in known if f.lower().rsplit('.', 1)[-1] == leaf))
    return list(dict.fromkeys(ranked))[:5]


async def field_problems(es: ElasticsearchGateway, index: str, *, exact: Iterable[str] = (),
                         aggregated: Iterable[str] = (), timestamp_field: str | None = None,
                         other: Iterable[str] = ()) -> list[str]:
    """Problems with the fields a query or pack names, checked against `index`'s mapping.

    `exact` fields are matched on whole values, so mustn't be analysed text; `aggregated` fields are
    grouped by, so must be aggregatable; the timestamp field must be a date. Wildcards and metadata
    fields aren't checked.
    """
    exact, aggregated = set(exact), set(aggregated)
    named = exact | aggregated | set(other) | {timestamp_field or ''}
    named = {n for n in named if n and not n.startswith('_') and not any(c in n for c in '*?')}
    if not named:
        return []
    body = await es.field_caps(index, sorted({pattern for n in named for pattern in (n, f'{n}.*')}))
    if 'indices' in body and not body['indices']:
        raise ToolError(f"No index matches '{index}'")
    mapped = body.get('fields', {})

    problems, known = [], None
    for name in sorted(named):
        if name not in mapped:
            if known is None:
                known = list((await es.field_caps(index, ['*'])).get('fields', {}))
            similar = _suggestions(name, known)
            problems.append(f"No field '{name}' in '{index}'."
                            + (f" Did you mean {', '.join(similar)}?" if similar else ''))
            continue
        types = set(mapped[name]) - {'unmapped'}
        if types <= _OBJECT_TYPES:
            children = _subfields(name, mapped)
            problems.append(f"'{name}' is an object, not a field; name one of its fields"
                            + (f", e.g. {', '.join(children[:5])}." if children else "."))
        elif name in exact and types <= _TEXT_TYPES:
            problems.append(f"'{name}' is a text field, so exact matches on it rarely work; "
                            + _instead(name, _subfields(name, mapped, aggregatable=True),
                                       "search it with the full-text query instead") + ".")
        elif name in aggregated and not any(c.get('aggregatable') for c in mapped[name].values()):
            problems.append(f"'{name}' can't be grouped by; "
                            + _instead(name, _subfields(name, mapped, aggregatable=True),
                                       "choose a keyword, numeric, ip, boolean or date field") + ".")
        elif name == timestamp_field and not types & _DATE_TYPES:
            problems.append(f"Timestamp field '{name}' is not a date field.")
    return problems


async def check_fields(es: ElasticsearchGateway, index: str, *, filters: Iterable[Filter] = (),
                       query: str | None = None, aggregated: Iterable[str] = (),
                       timestamp_field: str | None = None) -> None:
    """Refuse a tool call whose query or fields are wrong in a way Elasticsearch wouldn't report."""
    check_lucene(query)
    filters = list(filters)
    if problems := await field_problems(es, index, exact=[f.field for f in filters if f.op in _EXACT_OPS],
                                        aggregated=aggregated, timestamp_field=timestamp_field,
                                        other=[f.field for f in filters] + sorted(lucene_fields(query))):
        if any(p.startswith('No field') for p in problems):
            problems.append(f"Use {es.settings.tool_prefix}describe_fields to list the fields.")
        raise ToolError(' '.join(problems))


async def aggregatable_fields(es: ElasticsearchGateway, index: str, fields: Iterable[str]) -> list[str]:
    """Those of `fields` that every index in `index` can aggregate on, in order."""
    names = [f for f in dict.fromkeys(fields) if f and not f.startswith('_') and not any(c in f for c in '*?')]
    if not names:
        return []
    mapped = (await es.field_caps(index, names)).get('fields', {})
    return [f for f in names if f in mapped and all(c.get('aggregatable') for c in mapped[f].values())]
