# Source packs

A source pack is a YAML file in `packs/` (`ES_MCP_PACKS_DIR`) that describes one data source and
declares curated tools for it. **Adding a data source means adding a YAML file, not writing
code.** Everything specific to a source belongs in its pack: index names, field names, event
codes and retention. The server code stays independent of any schema.

A pack gives agents two things:

- **Context.** `list_data_sources` shows each pack's description, key fields, retention and tools,
  so an agent knows what the data is and how to query it before its first search.
- **Shortcuts.** Each tool in the pack is a fixed query, a search, top-values or distinct-values
  query with built-in filters, exposed as its own MCP tool with a few named arguments.

Included packs: `windows` (Windows security events), `fortios` (FortiGate firewall logs) and
`ingress_nginx` (ingress-nginx access logs). The [tool guide](tools.md#pack-tools) describes
their tools.

## Pack fields

```yaml
name: windows
title: Microsoft Windows security events
index: ecs-microsoft-windows-*
retention_days: 60
event_type_field: event.code
description: >
  Windows event log records, mostly from the Security log, ...
key_fields:
  user.name: Account logon name in lower case, e.g. 'john.smith1'
  event.code: "Windows event ID as a string: 4624 logon, 4625 failed logon, ..."
default_fields: ['@timestamp', event.code, user.name, host.hostname]
tools:
  - ...
```

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | Short identifier, shown in `list_data_sources` and used to tag the pack's tools. |
| `title` | yes | Human-readable name, shown to agents and used in retention notes. |
| `index` | yes | The index expression every pack tool searches. See [Index expressions](#index-expressions). |
| `description` | yes | What the data is, what it's good for, how it's split into retention tiers, and any quirks. Agents read it, so write it for them. |
| `retention_days` | no | How many days back **every** event is kept: the retention of the shortest tier. Leave it unset when nothing ages out. See [Retention](#retention). |
| `event_type_field` | no | The field that names the kind of each event, e.g. `event.code`. `compare_periods` groups by it when not given `group_by`. |
| `timestamp_field` | no | Defaults to `@timestamp`. |
| `key_fields` | no | Field name → explanation, for the fields agents will use most. Include formats and example values. |
| `default_fields` | no | Fields that search tools return when a tool doesn't set its own `fields`. Unset means whole events. |
| `tools` | no | The pack's tools. See below. |

## Tools

Each entry in `tools` becomes an MCP tool:

```yaml
tools:
  - name: windows_remote_access_events
    kind: search
    description: >
      Remote Desktop Gateway (RDG) and Remote Desktop (RDP) connection events for a user, in
      chronological order. Use to reconstruct a user's remote access sessions.
    params:
      user:
        description: Logon name (e.g. 'john.smith1') or account SID
        fields: [user.name, user.id]
    filters:
      - field: event.provider
        op: in
        value:
          - Microsoft-Windows-TerminalServices-Gateway
          - Microsoft-Windows-TerminalServices-RemoteConnectionManager
    fields: ['*']
    sort: asc
    size: 50
```

| Field | Kinds | Meaning |
|---|---|---|
| `name` | all | lower_snake_case, up to 64 characters, and unique across all packs. Prefix it with the pack name, e.g. `windows_…`. |
| `kind` | all | `search` (events), `top_values` (most frequent values) or `distinct_values` (every value, with first/last seen). |
| `description` | all | What the tool returns and when to use it. Agents choose tools by this, so say what question it answers. The server appends the pack's title and index. |
| `params` | all | Named arguments. See [Parameters](#parameters). |
| `filters` | all | Fixed conditions, in the same format as `search_events` filters (see the [tool guide](tools.md#filters)). |
| `query` | all | A fixed Lucene query, combined with the filters. |
| `size` | all | Default number of results. Callers can raise it to 500 (search), 100 (top values) or 1000 (distinct values). |
| `fields` | search | Fields to return. Defaults to the pack's `default_fields`. |
| `sort` | search | `desc` (most recent first, the default) or `asc`. |
| `field` | top_values, distinct_values | The field to rank or list. Required. |
| `include_fields` | top_values, distinct_values | Fields to show from one example event for each value. |
| `order` | distinct_values | `last_seen` (the default), `count` or `value`. |

Every generated tool also takes a required `time_range` and an optional `size`. Callers can't
change the index, filters or fields; to query differently, they use the generic tools. Pack tools
go through the same run-as, exposed-index and audit checks as the generic tools.

### Parameters

```yaml
params:
  host:
    description: Host name or its start, e.g. 'habpw01' matches 'habpw01.intranet.gradata.com.au'
    fields: [host.hostname]
    match: prefix
    required: false
```

| Field | Default | Meaning |
|---|---|---|
| `description` | required | Shown to agents as the argument's description. Give an example value. |
| `fields` | required | The fields the value is matched against. With several fields, an event matches if **any** of them matches, e.g. a user given by name or SID. |
| `type` | `string` | `string` or `integer`. |
| `match` | `eq` | `eq` for an exact match or `prefix` for starts-with. `prefix` needs a string parameter. |
| `required` | `true` | Optional parameters that the caller leaves out add no condition. |

Parameter names must be lower_snake_case and can't be `time_range` or `size`.

## Index expressions

`index` is a comma-separated list of names and wildcard patterns. A `-` in front of a pattern
excludes the indices it matches:

```yaml
index: ecs-fortios-*                       # every FortiOS tier
index: logs-app-*,-logs-app-debug-*        # everything except debug logs
index: audit-2026,audit-archive            # two named indices
```

- An exclusion must come after a wildcard pattern that it narrows.
- Remote clusters (`cluster:index`) and date math (`<logs-{now/d}>`) aren't supported.
- Every included target must be within `exposed_indices` in `access_policy.yaml`. Packs whose
  index isn't covered are skipped at startup, with a warning in the log.

## Retention

Many sources keep their data in several tiers with different retention periods. For example, a
short-lived `-rs-*` data stream holds everything for two months, while a long-term `-v1` index
keeps a subset for years, with each event stored in only one of them. To describe such a source:

1. **Point `index` at a pattern that covers every tier**, so searches reach all the data.
2. **Say in `description` how long each tier keeps what.** Agents then know that older periods
   hold only a subset, and that an empty result there doesn't mean nothing happened.
3. **Set `retention_days` to the shortest tier's retention.** `list_data_sources` shows it, and
   `search_events`, `top_values`, `distinct_values`, `compare_periods`, `esql_query` and every pack
   tool add a `note` to their results when the requested period reaches back further. This works
   whether they're called with the pack's index, a single tier, or a broader pattern such as
   `ecs-*`. When several packs match, the shortest retention wins.

For a source that keeps everything indefinitely, leave `retention_days` unset and say so in the
description.

## Generating a pack with an agent

The server provides an MCP prompt, `create_source_pack`, that has an agent write a pack with you.
Start it from any MCP client that supports prompts (in many chat clients, type `/` and pick it),
and give it:

| Argument | Required | What to give |
|---|---|---|
| `mapping` | yes | The index mapping or template as JSON: `GET <index>/_mapping` output, a mapping, or an index or component template (`GET _index_template/<name>`). |
| `use_cases` | no | The questions agents should answer with the data, one per line. If you leave it out, the agent asks for them and suggests some. |
| `index` | no | The index, alias, data stream or pattern the pack will cover. |

The agent then works through the pack with you:

1. It reads the mapping (multi-fields, aliases, dynamic templates, the timestamp field) and asks
   for any missing component templates.
2. If the index is reachable through this server, it looks at real values with `describe_fields`,
   `top_values` and a small `search_events`.
3. It agrees the name, index, retention tiers, `retention_days` and description with you.
4. It proposes key fields, and **asks you about any that are ambiguous**: fields that could mean
   the same thing, unclear formats or case conventions, codes stored as strings, `text` versus
   `keyword`, possibly empty fields, and which field is the `event_type_field`. Each question
   comes with its best guess, so you can confirm or correct it.
5. It turns each use case into a `search`, `top_values` or `distinct_values` tool. It shows them
   as a table for you to adjust, and points out use cases that a generic tool already covers.
6. It writes the YAML, checks it against the pack schema and the rules above, and revises it
   with you.

The prompt gives the agent the pack schema generated from the server's own validation, one of
the loaded packs as an example, and the tool names already in use.

**The result is only handed back.** The agent returns the pack as YAML text. Nothing is saved,
installed or loaded on the server, and the server has no way to do so. Whether the pack is used is
up to you: add it to your deployment's pack collection and follow
[Writing a new pack](#writing-a-new-pack) from step 5.

**Example**, with an index template from Elasticsearch:

```
GET _index_template/ecs-ingress-nginx-access
```

Then start `create_source_pack` with the response as `mapping`, and with `use_cases`:

```
Which clients hit one site hardest today?
Every request one client made, most recent first
Which URL paths returned server errors this week?
Which countries did clients come from this year?
```

## Writing a new pack

1. Look at the data first. `describe_fields` shows its fields and types. `top_values` on the
   likely event type field shows what kinds of events it holds.
2. Create `packs/<name>.yaml` with `name`, `title`, `index` and a `description` written for an
   agent: what the data is, what one event represents, the retention tiers, and anything
   surprising (synthetic traffic, service accounts, case conventions).
3. Add `key_fields` for the fields agents will filter and group on, with their formats and
   example values. Add `default_fields` so searches return a readable summary rather than whole
   events.
4. Add tools for the questions people ask most often. Keep each one narrow and describe it by the
   question it answers. The generic tools cover everything else.
5. Add the pack's index to `exposed_indices` if it isn't covered already.
6. Run `uv run pytest`. `tests/test_packs.py` loads every pack in `packs/` and fails on invalid
   fields, duplicate tool names or bad index expressions. Then start the server and check that
   `list_data_sources` shows the pack.

In Kubernetes, packs can also be supplied through the chart without rebuilding the image (see
[Deployment](deployment.md#changing-the-policy-or-packs)).
