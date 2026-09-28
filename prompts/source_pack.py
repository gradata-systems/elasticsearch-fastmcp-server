"""MCP prompt that guides an agent and a user through writing a source pack from an index mapping."""
import json
from string import Template
from typing import Annotated

import yaml
from fastmcp import Context
from pydantic import Field

from sources.packs import SourcePack

# A Template rather than str.format, because the text is full of literal JSON braces.
_INSTRUCTIONS = Template("""\
You are helping the user write a source pack for this Elasticsearch MCP server: a YAML file that
describes one data source and declares curated tools for it. Work with the user step by step. Ask
questions and wait for their answers rather than guessing, and keep each message short enough to
answer quickly.

The result is YAML text that you give back to the user. It isn't added to this server: you can't
install, save or load a pack here, and shouldn't try. The user decides whether and how to add it
to the pack collection their deployment uses.

A pack is read by other agents, not people. Its description, key field explanations and tool
descriptions are what those agents use to decide how to query the data, so they must be accurate
and specific. Never invent field names, values, formats or retention periods. Take them from the
mapping, from the data, or from the user.

## Step 1: Read the mapping

The user has provided the mapping below. Work out what it is:
- a mapping (`{"mappings": {"properties": ...}}` or just `{"properties": ...}`),
- `GET <index>/_mapping` output (index names, each with `mappings`),
- an index template (`index_patterns` and `template.mappings`), possibly with `composed_of`
  component templates you haven't been given, or
- a component template (`template.mappings`).

Flatten the properties into dotted field names with their types. Note:
- multi-fields, e.g. a `text` field with a `.keyword` sub-field (filter and aggregate on the keyword),
- `alias` fields, and runtime fields,
- `dynamic_templates` and `dynamic: true`, which mean fields can exist that the mapping doesn't
  list,
- the date fields, and which one is the event time (usually `@timestamp`),
- the index patterns the mapping applies to, if it says.

If component templates are referenced but missing, or the mapping is clearly partial, say so and
ask the user for the rest before going on.

Summarise what you found in a few lines (field count, the main groups of fields, the timestamp
field, the index patterns) and move on.

## Step 2: Look at the data, if you can

If this server's `list_data_sources` shows an index the mapping applies to, use the read-only
tools to ground the pack in real data. Keep each call small and the time range short.
- `describe_fields` confirms which fields exist and how they are mapped.
- `top_values` on candidate fields shows real values, their formats and case conventions, and
  whether a field is populated at all.
- `search_events` with `size` 3 shows what whole events look like.

If the index isn't available here, say so and rely on the user for example values.

## Step 3: The data source

Agree these with the user:
- `name` (lower_snake_case) and `title`.
- `index`: a pattern covering every index or data stream holding this data, including every
  retention tier. Ask whether the data is split across tiers (for example a short-lived data
  stream and a long-term index) and how long each tier keeps what.
- `retention_days`: how far back every event is kept, which is the shortest tier's retention.
  Leave it unset only if the user confirms nothing ages out.
- `timestamp_field`, if it isn't `@timestamp`.
- `description`: what the data is, what one event represents, what it is good for, the
  retention tiers, and anything surprising (synthetic or health-check traffic, service accounts,
  case conventions, fields that are often empty).

## Step 4: Key fields

Choose the fields agents will filter, group and search on most, usually 8 to 20. For each, write
an explanation that says what the field holds, its format and an example value.

Ask the user whenever a key field is ambiguous. Batch the questions, and for each one give your
best guess so the user can simply confirm or correct it. Ask in particular when:
- several fields could mean the same thing (for example two address fields or two user fields),
  and it isn't clear which one agents should use for which question,
- a field's name doesn't make clear what it holds, or whose point of view it takes,
- the format or case convention of values isn't clear from the mapping or the data,
- a value is stored as a string although it looks numeric (codes, IDs), since filters must then
  use strings,
- a field exists as both `text` and `keyword`, and it matters which one agents should use,
- a field is in the mapping but may not be populated,
- several fields could name the kind of event. Settle which one is `event_type_field`, which
  `compare_periods` groups by by default.

Also agree `default_fields`: the fields a search returns when a tool doesn't choose its own.
They should be enough to read an event at a glance.

## Step 5: Tools from the user's use cases

$use_cases_section

Turn each use case into a tool:
- `search` when the answer is a list of events ("show me", "what did X do", "timeline").
- `top_values` when the answer is the most frequent values ("top", "busiest", "most common").
- `distinct_values` when the answer is every value, with first and last seen ("which", "who",
  "list all").

For each tool, decide:
- `name` (prefixed with the pack name) and a `description` that says which question it answers,
- `params`: the arguments callers give, each with the fields it matches (any of them may match),
  `match: prefix` for starts-with matching, `type: integer` for numeric fields, and
  `required: false` if it's optional,
- fixed `filters` and `query` that define the use case,
- for search: `fields` if different from `default_fields`, `sort`, and `size`,
- for top and distinct values: `field`, `include_fields` for context, and `size`.

Some use cases don't need a pack tool, so tell the user which generic tool covers them instead:
- change over time ("what stopped or dropped since X") is covered by `compare_periods` and
  `event_type_field`,
- anything needing several group-by fields, statistics or time buckets is covered by `esql_query`.

Present the tools as a short table (name, kind, arguments, what it answers) and let the user
adjust them before you write the YAML.

## Step 6: Write and check the YAML

Write the complete pack. Before showing it, check it against the schema below and these rules:
- Tool names are lower_snake_case, at most 64 characters, and unique. They shouldn't clash with
  the existing tools listed below.
- Parameter names are lower_snake_case and aren't `time_range` or `size`.
- `prefix` matching needs a string parameter.
- `size` is at most 100 for `top_values` tools and at most 500 for the others.
- `top_values` and `distinct_values` tools need `field`.
- Every field you use exists in the mapping. Exact filters, parameters and aggregated fields are
  `keyword`, numeric, `ip`, `boolean` or `date`, never `text`.
- Filter values have the right type: strings for keyword fields, even if they look like numbers.
- In `index`, an exclusion (`-pattern`) comes after a wildcard pattern, and there are no remote
  clusters (`cluster:index`) or date math.

Show the YAML in one block and ask the user to review it. Revise it until they are happy.

## Step 7: Hand over

Give the user the final YAML in one block, as `<name>.yaml`, and stop there. Don't write it
anywhere yourself. Explain that to use it, they (or whoever manages the deployment) would:
1. add it to the deployment's pack collection: the packs directory the server reads
   (`ES_MCP_PACKS_DIR`), or the Helm chart's `packs` value,
2. make sure `exposed_indices` in `access_policy.yaml` covers the pack's `index`, or the pack is
   skipped at startup,
3. validate it, for example with `uv run pytest` in a checkout that includes it, which loads every
   pack and rejects invalid ones, and
4. redeploy or restart the server, then check that `list_data_sources` shows the pack.

## Pack schema

JSON schema of a pack, generated from the server's own validation:

```json
$schema
```
$example_section$existing_tools_section
## The mapping

$index_section```json
$mapping
```
""")

_NO_USE_CASES = """\
Ask the user what questions they want agents to answer with this data, for example "which
accounts logged in from outside the country this week" or "the busiest clients of one site".
Suggest a few based on the fields you found to get them started. Collect three to eight."""


def _pretty(mapping: str) -> str:
    try:
        return json.dumps(json.loads(mapping), indent=2)
    except ValueError:
        return mapping.strip()


def _example(packs: list[SourcePack]) -> str:
    pack = next((p for p in packs if p.tools), None)
    if pack is None:
        return ''
    body = yaml.safe_dump(pack.model_dump(mode='json', exclude_defaults=True), sort_keys=False,
                          allow_unicode=True, width=100)
    return f"\n## An existing pack, for reference\n\n```yaml\n{body}```\n"


def create_source_pack(
        mapping: Annotated[str, Field(
            description="The index mapping or index template, as JSON: a mapping, GET <index>/_mapping output, "
                        "or an index or component template.")],
        ctx: Context,
        use_cases: Annotated[str | None, Field(
            description="Optional: the questions agents should be able to answer with this data, one per line. "
                        "Left out, the agent asks for them.")] = None,
        index: Annotated[str | None, Field(
            description="Optional: the index, alias, data stream or pattern the pack will cover.")] = None,
) -> str:
    """Work with the user to write a source pack (YAML) for a data source, starting from its index mapping
    or index template and the use cases they want agents to handle."""
    packs: list[SourcePack] = ctx.lifespan_context.get('packs', [])
    if use_cases and use_cases.strip():
        use_cases_section = ("The user wants agents to answer these questions. Confirm your reading of "
                             "each one and ask about any that are unclear:\n\n" + use_cases.strip())
    else:
        use_cases_section = _NO_USE_CASES
    existing = sorted(spec.name for pack in packs for spec in pack.tools)
    existing_tools_section = (f"\n## Existing tools\n\nTool names already in use: {', '.join(existing)}.\n"
                              if existing else '')
    return _INSTRUCTIONS.substitute(
        use_cases_section=use_cases_section,
        schema=json.dumps(SourcePack.model_json_schema(), indent=2),
        example_section=_example(packs),
        existing_tools_section=existing_tools_section,
        index_section=f"The pack will cover `{index}`.\n\n" if index else '',
        mapping=_pretty(mapping),
    )


ALL_PROMPTS = [create_source_pack]
