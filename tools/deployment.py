"""How a deployment presents itself to agents: tool name prefix, cluster identity and server instructions.

One deployment serves one Elasticsearch cluster. An agent may use several deployments at once, one
per cluster, so each can prefix its tool names (for clients that don't keep tools from different
servers apart) and says which cluster it queries.
"""
import re
from typing import Any, Iterable

from fastmcp import FastMCP
from fastmcp.prompts import Prompt
from fastmcp.resources import ResourceTemplate
from fastmcp.tools import Tool

from config import Settings
from prompts import source_pack
from sources.packs import SourcePack, SourceTool
from tools import export, generic, pack_validation

# Longest tool name many model APIs accept (^[a-zA-Z0-9_-]{1,64}$).
MAX_NAME_LENGTH = 64


def qualify(text: str, names: Iterable[str], prefix: str) -> str:
    """`text` with every whole-word mention of one of `names` prefixed."""
    names = sorted(names, key=len, reverse=True)
    if not prefix or not text or not names:
        return text
    pattern = re.compile(r'(?<![\w-])(' + '|'.join(re.escape(n) for n in names) + r')(?![\w-])')
    return pattern.sub(lambda m: prefix + m.group(1), text)


def _qualify_descriptions(schema: Any, names: list[str], prefix: str) -> Any:
    if isinstance(schema, dict):
        return {key: qualify(value, names, prefix) if key == 'description' and isinstance(value, str)
                else _qualify_descriptions(value, names, prefix) for key, value in schema.items()}
    if isinstance(schema, list):
        return [_qualify_descriptions(item, names, prefix) for item in schema]
    return schema


def qualified(tool: Tool, names: list[str], settings: Settings) -> Tool:
    """`tool` renamed with the prefix, its mentions of other tools prefixed, and the cluster named."""
    prefix = settings.tool_prefix
    name = prefix + tool.name
    if len(name) > MAX_NAME_LENGTH:
        raise ValueError(f"tool name '{name}' is longer than {MAX_NAME_LENGTH} characters; "
                         f"use a shorter tool prefix or tool name")
    description = qualify(tool.description or '', names, prefix)
    if settings.cluster_name:
        description += f"\n\nCluster: {settings.cluster_name}."
    parameters = _qualify_descriptions(tool.parameters, names, prefix)
    # Show the model the size the server will actually return, not just the tool's own maximum.
    if size := parameters.get('properties', {}).get('size'):
        for key in ('maximum', 'default'):
            if isinstance(size.get(key), int):
                size[key] = min(size[key], settings.max_result_size)
    return tool.model_copy(update={'name': name, 'description': description, 'parameters': parameters})


def server_instructions(settings: Settings, packs: list[SourcePack]) -> str:
    """MCP server instructions: what this deployment queries and how to start."""
    prefix, name = settings.tool_prefix, settings.cluster_name
    if name:
        about = f"This server queries the Elasticsearch cluster '{name}'"
        about += f": {settings.cluster_description.strip()}" if settings.cluster_description.strip() else '.'
        about = about if about.endswith('.') else about + '.'
    else:
        about = "This server queries an Elasticsearch cluster."
    parts = [about + " Every query runs with the caller's own Elasticsearch permissions."]
    if packs:
        parts.append("Source packs (known data sources) on this cluster: "
                     + '; '.join(f"{p.title} ({p.index})" for p in packs)
                     + f". {prefix}list_data_sources shows which of them the caller can read.")
    parts.append(f"Start with {prefix}list_data_sources to see what the caller can query, and the source packs "
                 f"with their key fields and tools.")
    parts.append(f"Results come as tables of 'columns' and 'rows'. Take numbers from what the tools return (total, "
                 f"count, summary, distinct_values) and never by counting rows yourself; for questions about how "
                 f"many, use {prefix}top_values, {prefix}distinct_values or {prefix}esql_query with STATS rather than "
                 f"a large search. Show the user only as many rows as they need, and say when a result was "
                 f"truncated.")
    parts.append(f"When a question relates two data sources, such as which users seen in one have logged on "
                 f"according to another, plan it as two steps: first get the set of values from the source that "
                 f"defines them, then look those values up in the other. {prefix}match_values does both in one "
                 f"call. Don't query only the second source and compare by eye, and don't copy long lists of "
                 f"values from one result into the next call; to follow up a few values, pass them in an 'in' "
                 f"filter.")
    parts.append(f"When the user wants the events themselves, to download or keep, rather than an answer, use "
                 f"{prefix}export_events: it gives them a link to a CSV or NDJSON file without the events "
                 f"passing through you.")
    if name:
        parts.append(
            "You may also be connected to other deployments of this server for other clusters, offering the same "
            f"tools under other names. Before querying, work out which cluster the user means from the source "
            f"packs each deployment offers the caller: the 'sources' in its {prefix}list_data_sources result. "
            "Don't choose a cluster by indices, aliases or data streams that no source pack describes. If source "
            "packs on more than one cluster could hold what the user is asking about, or none clearly does, ask "
            "the user which cluster they mean rather than guessing or querying them all. Say which cluster each "
            "result came from.")
    return '\n\n'.join(parts)


def register(mcp: FastMCP, settings: Settings, packs: list[SourcePack]) -> None:
    """Add the generic tools, the packs' tools, the export links and the prompts to `mcp`, prefixed as
    configured."""
    functions = generic.ALL_TOOLS + export.ALL_TOOLS + pack_validation.ALL_TOOLS
    names = [fn.__name__ for fn in functions] + [spec.name for pack in packs for spec in pack.tools]
    for fn in functions:
        mcp.add_tool(qualified(Tool.from_function(fn), names, settings))
    for pack in packs:
        for spec in pack.tools:
            mcp.add_tool(qualified(SourceTool.build(pack, spec), names, settings))
    mcp.add_template(ResourceTemplate.from_function(
        export.read_export, export.URI_TEMPLATE, name=settings.tool_prefix + 'export', mime_type='text/csv',
        description=qualify(export.read_export.__doc__, names, settings.tool_prefix)))
    for fn in source_pack.ALL_PROMPTS:
        prompt = Prompt.from_function(fn)
        mcp.add_prompt(prompt.model_copy(update={'name': settings.tool_prefix + prompt.name}))
