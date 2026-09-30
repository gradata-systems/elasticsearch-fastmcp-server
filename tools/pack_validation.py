"""Checks a source pack written with the create_source_pack prompt, without adding it to the server.

The pack is only parsed and checked; it is never saved, installed or loaded. Whoever manages the
deployment adds it to the pack collection themselves.
"""
import difflib
from typing import Annotated, Any, get_args, get_origin

import yaml
from fastmcp import Context
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field, ValidationError

from sources.packs import SourcePack
from tools import generic
from tools.checks import field_problems, lucene_fields, lucene_problems

_EXACT_OPS = {'eq', 'in', 'prefix'}


def _step(annotation: Any, part: str | int) -> Any:
    """The type found at `part` inside a value of type `annotation`, if it can be told."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        field = annotation.model_fields.get(part) if isinstance(part, str) else None
        return field.annotation if field else None
    args = [a for a in get_args(annotation) if a is not type(None)]
    if get_origin(annotation) is list and isinstance(part, int):
        return args[0]
    if get_origin(annotation) is dict and isinstance(part, str):
        return args[1]
    return _step(args[0], part) if len(args) == 1 else None


def _allowed_keys(location: tuple[str | int, ...]) -> list[str]:
    annotation: Any = SourcePack
    for part in location:
        annotation = _step(annotation, part)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return list(annotation.model_fields)
    return []


def _path(location: tuple[str | int, ...]) -> str:
    return ''.join(f'[{p}]' if isinstance(p, int) else f'.{p}' for p in location).lstrip('.') or 'pack'


def _describe(error: dict[str, Any]) -> str:
    location = tuple(error['loc'])
    message = error['msg']
    if error['type'] == 'extra_forbidden':
        allowed = _allowed_keys(location[:-1])
        similar = difflib.get_close_matches(str(location[-1]), allowed, n=2, cutoff=0.6)
        message = "unknown key" + (f"; did you mean {' or '.join(similar)}?" if similar else
                                   f"; allowed keys are {', '.join(allowed)}")
    elif error['type'] == 'dict_type' and isinstance(error.get('input'), list):
        message += " (a map of name: value, not a list)"
    elif error['type'] == 'list_type' and isinstance(error.get('input'), dict):
        message += " (a list of items, not a map)"
    return f"{_path(location)}: {message}"


def _parse(text: str) -> tuple[SourcePack | None, list[str]]:
    if text.lstrip().startswith(('{', '[')):
        return None, ["The pack is JSON; write it as YAML, the format of the pack files."]
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        return None, [f"Not valid YAML: {e}"]
    if not isinstance(data, dict):
        return None, ["The pack must be a YAML map with keys such as name, title, description and index."]
    try:
        return SourcePack.model_validate(data), []
    except ValidationError as e:
        return None, [_describe(error) for error in e.errors()]


def _field_uses(pack: SourcePack) -> dict[str, set[str]]:
    exact, aggregated, other = set(), set(), set(pack.key_fields) | set(pack.default_fields)
    if pack.event_type_field:
        aggregated.add(pack.event_type_field)
    for spec in pack.tools:
        exact |= {f.field for f in spec.filters if f.op in _EXACT_OPS}
        exact |= {field for param in spec.params.values() for field in param.fields}
        other |= {f.field for f in spec.filters} | lucene_fields(spec.query)
        other |= set(spec.fields or []) | set(spec.include_fields)
        if spec.field and spec.kind != 'search':
            aggregated.add(spec.field)
    return {'exact': exact, 'aggregated': aggregated, 'other': other}


async def validate_source_pack(
        pack: Annotated[str, Field(description="The complete source pack, as YAML text.")],
        ctx: Context,
) -> dict[str, Any]:
    """
    Check a source pack written with the create_source_pack prompt: its YAML, its structure, its tool
    names and, if this server can read the pack's index, that its fields exist and suit how they are
    used. Fix every problem and check again before showing the pack to the user. This only checks the
    pack; it doesn't save, install or load it.
    """
    parsed, problems = _parse(pack)
    notes = []
    if parsed:
        es = ctx.lifespan_context['es']
        prefix = es.settings.tool_prefix
        # A revised version of a pack this server already has may keep its tool names.
        taken = {fn.__name__ for fn in generic.ALL_TOOLS + ALL_TOOLS} | {
            spec.name for p in ctx.lifespan_context.get('packs', []) if p.name != parsed.name for spec in p.tools}
        seen = set()
        for spec in parsed.tools:
            if spec.name in taken:
                problems.append(f"tools: '{spec.name}' is already the name of a tool on this server.")
            elif spec.name in seen:
                problems.append(f"tools: '{spec.name}' is defined twice.")
            seen.add(spec.name)
            if len(prefix + spec.name) > 64:
                problems.append(f"tools: '{prefix}{spec.name}' would be longer than 64 characters.")
            problems += [f"tools '{spec.name}' query: {p}" for p in lucene_problems(spec.query)]
        try:
            problems += await field_problems(es, parsed.index, timestamp_field=parsed.timestamp_field,
                                             **_field_uses(parsed))
        except ToolError as e:
            notes.append(f"The fields weren't checked against the data, because '{parsed.index}' couldn't be "
                         f"read here ({e}). Check each one against the mapping.")
    result: dict[str, Any] = {'valid': not problems, 'problems': problems}
    if notes:
        result['notes'] = notes
    return result


ALL_TOOLS = [validate_source_pack]
