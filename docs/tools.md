# Tool guide

How to use each tool the server exposes, with example arguments and (shortened) results. The
generic tools work against any index the caller may read; the pack tools are generated from the
YAML files in `packs/` and are shortcuts for common questions about one data source.

- [Choosing a tool](#choosing-a-tool)
- [Common arguments](#common-arguments)
- [What results look like](#what-results-look-like)
- Generic tools: [list_data_sources](#list_data_sources) · [describe_fields](#describe_fields) ·
  [search_events](#search_events) · [top_values](#top_values) · [distinct_values](#distinct_values) ·
  [compare_periods](#compare_periods) · [esql_query](#esql_query)
- Pack tools: [windows](#windows-pack) · [fortios](#fortios-pack) · [ingress_nginx](#ingress_nginx-pack)

## Choosing a tool

| Question | Tool |
|---|---|
| What data can I query, and what's in it? | `list_data_sources`, then `describe_fields` |
| Show me the events where … | `search_events`, or a pack search tool |
| What are the most common …? | `top_values` |
| Which … occurred in period Y? (a complete list) | `distinct_values` |
| What stopped, dropped, appeared or rose since date X? | `compare_periods` |
| Anything else: several group-by fields, computed columns, time buckets | `esql_query` |

Start a session with `list_data_sources`. It lists the index to use for each data source, its key
fields, its retention and its pack tools. A pack tool is the quickest route when one matches the
question. The generic tools cover everything else.

## Common arguments

### `index`

An index, alias, data stream or pattern. Separate several targets with commas, and prefix a target
with `-` to exclude it: `ecs-fortios-*,-ecs-fortios-rs-*`. For a known data source, use the
`index` that `list_data_sources` gives, so that every retention tier is searched. Targets outside
the server's `exposed_indices` are refused, and Elasticsearch applies the caller's own privileges
on top.

### `time_range`

Every tool except `list_data_sources` and `describe_fields` needs one:

```json
{"start": "2026-09-01", "end": "2026-09-07"}
```

- `start` is inclusive. `end` is inclusive and optional; leaving it out means now.
- A date alone as `end` covers that whole day, so the example above includes all of 7 September.
- Datetimes are ISO 8601 (`2026-09-01T08:00:00Z`). A datetime without a time zone is taken as UTC.
- How long a range may be depends on the tool:

  | Tools | Longest range |
  |---|---|
  | `search_events`, pack search tools, ES\|QL without `STATS` | `ES_MCP_MAX_TIME_RANGE_DAYS` (default 90 days) |
  | `top_values`, `distinct_values`, each period of `compare_periods`, pack tools of those kinds, ES\|QL with `STATS` | `ES_MCP_MAX_AGGREGATION_RANGE_DAYS` (default 366 days) |

  Counting over a long period is cheap for Elasticsearch; returning raw events from one is not.

### `filters`

Conditions that must all hold:

```json
[
  {"field": "event.code", "value": "4624"},
  {"field": "destination.port", "op": "in", "value": [22, 3389]},
  {"field": "http.response.status_code", "op": "gte", "value": 500},
  {"field": "host.hostname", "op": "prefix", "value": "habpw"},
  {"field": "client.ip", "op": "exists"},
  {"field": "user.name", "value": "system", "negate": true}
]
```

| `op` | Matches | `value` |
|---|---|---|
| `eq` (default) | exactly this value | one value |
| `in` | any of these values | a non-empty list |
| `prefix` | values starting with this | one string |
| `gt`, `gte`, `lt`, `lte` | a range, for numbers, dates and IPs | one value |
| `exists` | events that have the field at all | none |

`"negate": true` excludes matching events instead. Exact matching only works on `keyword`,
numeric, `ip`, `boolean` and `date` fields. For `text` fields, use `query`. `describe_fields` tells
you each field's type.

### `query`

An optional full-text query in Lucene syntax, combined with the filters:

```
message:"failed password" AND NOT user.name:svc_*
```

Leading wildcards (`*admin`) are rejected because they are expensive. Use `filters` for exact
values, since that's faster and matches what you meant.

### `timestamp_field`

The field the time range applies to. Defaults to `@timestamp`. Pack tools use the pack's own
setting.

## What results look like

Each tool returns a JSON object, and some keys appear in any result:

| Key | Meaning |
|---|---|
| `truncated` + `hint` | More results existed than fit in the response (`ES_MCP_MAX_RESPONSE_CHARS`, default 100,000 characters) or the requested `size`. The hint says how to narrow the query. |
| `note` | Context for reading the result. Most often, the period reaches back further than the data source keeps every event (its `retention_days`), so older results are incomplete. |
| `warning` | Some shards failed, so the result is partial. If every shard fails, the tool returns an error instead. |

Errors from Elasticsearch come back with its reason (an unknown field, a wrong type, a bad query),
so an agent can correct the call and try again.

---

## list_data_sources

Lists what the caller may query: indices, aliases and data streams matching the exposed patterns
that the caller can read, plus a description of each known data source (pack) among them.

**Use it first**, to find index names, key fields, retention and pack tools. It takes no
arguments.

**Example result**

```json
{
  "indices": ["ecs-fortios-v1", "ecs-microsoft-windows-v1"],
  "aliases": [],
  "data_streams": ["ecs-fortios-rs-default", "ecs-microsoft-windows-rs-default"],
  "sources": [
    {
      "name": "windows",
      "title": "Microsoft Windows security events",
      "description": "Windows event log records, mostly from the Security log, ...",
      "index": "ecs-microsoft-windows-*",
      "key_fields": {"user.name": "Account logon name in lower case, ...", "event.code": "..."},
      "tools": ["windows_active_users", "windows_logon_users", "windows_user_events", "..."],
      "retention_days": 60,
      "event_type_field": "event.code"
    }
  ]
}
```

- `retention_days` appears only when the source doesn't keep everything forever. Before that
  many days ago, the source holds only a subset of its events.
- `event_type_field` is the field `compare_periods` groups by when you don't give it `group_by`.

## describe_fields

Lists the fields in an index and their types.

**Use it** before filtering or aggregating on a field you haven't used before, to check that the
field exists and whether it's `keyword` (filter exactly) or `text` (use `query`).

| Argument | Default | Notes |
|---|---|---|
| `index` | required | |
| `fields` | `*` | Comma-separated name patterns, e.g. `user.*,source.ip`. On large indices, `*` can return more fields than fit. |

**Example:** the user fields in the Windows data.

```json
{"index": "ecs-microsoft-windows-*", "fields": "user.*"}
```

```json
{
  "indices": ["ecs-microsoft-windows-v1", ".ds-ecs-microsoft-windows-rs-default-2026.09.01-000012"],
  "fields": {"user.domain": "keyword", "user.id": "keyword", "user.name": "keyword", "user.type": "keyword"}
}
```

A field mapped differently in different indices shows a list, e.g. `"source.ip": ["ip", "keyword"]`.
Exact filters still work, but aggregations on it may fail in some indices.

## search_events

Returns individual events matching a time range, filters and an optional full-text query, plus the
total number of matches.

**Use it** to look at what actually happened: the events behind a count, a timeline for a user or
host, or the full detail of one event. **Don't use it** to count or rank things. `total` gives the
number of matches, but for "how many per …" use `top_values`, `distinct_values` or `esql_query`.

| Argument | Default | Notes |
|---|---|---|
| `index`, `time_range` | required | Up to 90 days by default. |
| `filters`, `query` | none | See [Common arguments](#common-arguments). |
| `fields` | whole events | Fields to return, wildcards allowed. Asking for only the fields you need means more events fit in the response. |
| `size` | 20 | Up to 500. |
| `sort` | `desc` | `desc` for most recent first, `asc` for chronological order. |
| `timestamp_field` | `@timestamp` | |

**Example:** failed logons for one account over the last week, oldest first.

```json
{
  "index": "ecs-microsoft-windows-*",
  "time_range": {"start": "2026-09-22"},
  "filters": [{"field": "event.code", "value": "4625"}, {"field": "user.name", "value": "john.smith1"}],
  "fields": ["@timestamp", "host.hostname", "client.ip", "event.outcome"],
  "sort": "asc",
  "size": 50
}
```

```json
{
  "total": 3,
  "returned": 3,
  "events": [
    {"index": ".ds-ecs-microsoft-windows-rs-default-2026.09.15-000014", "id": "hT2x...",
     "event": {"@timestamp": "2026-09-23T07:41:12.004Z", "host": {"hostname": "habdc01.intranet.gradata.com.au"},
               "client": {"ip": "10.20.4.17"}, "event": {"outcome": "failure"}}}
  ]
}
```

**Tips**

- If `total` is much larger than `returned`, narrow the time range, add filters, or switch to an
  aggregation tool, rather than paging through events.
- An empty result for a period older than the source's `retention_days` doesn't mean nothing
  happened. The `note` says so when it applies.

## top_values

Ranks the most frequent values of a field and counts the events for each.

**Use it** for triage: busiest users, top source IPs, most common event codes, URLs or ports. It
returns the top N only. Rare values are left out and counted together in
`events_with_other_values`. For a complete list, use `distinct_values`.

| Argument | Default | Notes |
|---|---|---|
| `index`, `field`, `time_range` | required | `field` must be `keyword`, numeric, `ip`, `boolean` or `date`. The time range can be up to 366 days by default. |
| `filters`, `query` | none | |
| `size` | 10 | Up to 100. |
| `include_fields` | none | Fields to show from one example event for each value, e.g. the SID alongside a user name. |
| `timestamp_field` | `@timestamp` | |

**Example:** the ten client IPs making the most server errors this month, with their country.

```json
{
  "index": "ecs-ingress-nginx-access-*",
  "field": "source.ip",
  "time_range": {"start": "2026-09-01"},
  "filters": [{"field": "http.response.status_code", "op": "gte", "value": 500}],
  "include_fields": ["source.geo.country_iso_code"]
}
```

```json
{
  "total_events": 1843,
  "values": [
    {"value": "203.0.113.7", "count": 912, "sample": {"source.geo.country_iso_code": "AU"}},
    {"value": "198.51.100.23", "count": 140, "sample": {"source.geo.country_iso_code": "US"}}
  ],
  "events_with_other_values": 211
}
```

If `total_events` is non-zero but `values` is empty, none of the matching events have the field.
The `hint` suggests checking the name with `describe_fields`.

## distinct_values

Lists **every** value of a field over a time range, with its event count and when it was first
and last seen, plus the number of distinct values.

**Use it** for complete answers to "which …?" questions: which users logged in this year, which
hosts sent events this month, which countries clients came from. It pages through all the values
(up to 10,000) instead of keeping only the most frequent.

| Argument | Default | Notes |
|---|---|---|
| `index`, `field`, `time_range` | required | Same field types as `top_values`. The time range can be up to 366 days by default. |
| `filters`, `query` | none | |
| `order` | `last_seen` | `last_seen` (most recent first), `count` (most events first) or `value` (alphabetical). |
| `size` | 200 | Up to 1000. `distinct_values` in the result always gives the full count. |
| `include_fields` | none | Fields from one example event for each value. |
| `timestamp_field` | `@timestamp` | Also used for `first_seen` and `last_seen`. |

**Example:** every country that clients reached the web applications from in the past year,
alphabetically.

```json
{
  "index": "ecs-ingress-nginx-access-*",
  "field": "source.geo.country_iso_code",
  "time_range": {"start": "2025-09-30"},
  "filters": [{"field": "url.domain", "value": "healthcheck.apps.gradata.com.au", "negate": true}],
  "order": "value"
}
```

```json
{
  "distinct_values": 47,
  "returned": 47,
  "values": [
    {"value": "AE", "count": 12, "first_seen": "2026-02-11T03:12:40.118Z", "last_seen": "2026-06-30T22:01:09.550Z"},
    {"value": "AU", "count": 2210453, "first_seen": "2025-09-30T00:00:00.912Z", "last_seen": "2026-09-29T05:58:31.020Z"}
  ]
}
```

**Tips**

- `returned` smaller than `distinct_values`, with `truncated: true`, means the list didn't fit.
  Raise `size`, drop `include_fields`, or use filters to split the list.
- `distinct_values_complete: false` means there are more than 10,000 values and only the first
  10,000 were read. Filter further or shorten the time range.
- When the period reaches past the source's `retention_days`, the `note` says older values may
  be missing.

## compare_periods

Compares how often each value, or combination of values, occurs in a baseline period and a later
period, and reports the groups whose rate per day changed: `stopped`, `dropped`, `new` or
`increased`.

**Use it** for "what changed since date X?" questions. For example: which event types stopped
arriving, which hosts went quiet, or which firewall log types surged after a change. It pages
through every group (up to 10,000), so rare groups that stopped aren't missed, and it compares
rates per day, so the two periods can be different lengths.

| Argument | Default | Notes |
|---|---|---|
| `index`, `before`, `after` | required | Two time ranges. `before` must end before `after` starts. Each can be up to 366 days by default, and they can be far apart. |
| `group_by` | the source's `event_type_field` | 1–4 fields. With several fields, each combination of values is one group, e.g. event type × host. If left out, the tool uses the `event_type_field` of the data source the index belongs to. It returns an error if no source defines one, or if the index covers sources that use different fields. |
| `changes` | `down` | `down` (stopped or dropped), `up` (new or increased) or `both`. |
| `min_change_percent` | 50 | The smallest change in rate to report, as a percentage of the baseline rate. |
| `min_events` | 5 | Groups with fewer events than this in both periods are ignored as noise. |
| `filters`, `query` | none | Apply to both periods. |
| `size` | 50 | Up to 500 changed groups. |
| `timestamp_field` | `@timestamp` | |

A group is:

- `stopped` if it had events in `before` and none in `after`
- `new` if it had none in `before` and some in `after`
- `dropped` if its rate fell by at least `min_change_percent`
- `increased` if its rate rose by at least `min_change_percent`

Results are sorted by how far each group's event count is from what its baseline rate predicted,
so a busy group that stopped comes before a quiet one.

**Example:** which Windows event types, per host, have stopped or slowed down since 1 September,
compared with August.

```json
{
  "index": "ecs-microsoft-windows-*",
  "before": {"start": "2026-08-01", "end": "2026-08-31"},
  "after": {"start": "2026-09-01"},
  "group_by": ["event.code", "host.hostname"]
}
```

```json
{
  "group_by": ["event.code", "host.hostname"],
  "before_days": 31.0, "after_days": 28.5,
  "before_events": 1204411, "after_events": 1011930,
  "groups_compared": 312, "groups_changed": 2, "returned": 2,
  "changes": [
    {"group": {"event.code": "4624", "host.hostname": "habfs02.intranet.gradata.com.au"}, "status": "stopped",
     "before_count": 9300, "after_count": 0, "before_per_day": 300.0, "after_per_day": 0.0, "change_percent": -100},
    {"group": {"event.code": "4688", "host.hostname": "habpw01.intranet.gradata.com.au"}, "status": "dropped",
     "before_count": 3100, "after_count": 855, "before_per_day": 100.0, "after_per_day": 30.0, "change_percent": -70}
  ]
}
```

Here `habfs02` stopped logging logons completely, and process creation on `habpw01` fell by 70%.

**Tips**

- **Keep the baseline within the source's `retention_days`.** Older data holds only a subset, so
  groups compared against it look `new` or `increased` when nothing changed. The `note` warns when
  the baseline starts too early.
- A `note` saying the whole `after` period has no events points to an ingestion problem rather
  than a change in activity.
- Leave out `group_by` for a quick "which event types changed?" over one data source. Add a
  second field (host, user, rule) to find where the change happened.
- Raise `min_events` to ignore noise from rare groups. Lower `min_change_percent` to catch smaller
  shifts.

## esql_query

Runs a read-only ES|QL query, with the time range applied as a filter.

**Use it** for analysis the other tools can't express: grouping by several fields with several
statistics, computed columns (`EVAL`), time buckets (`BUCKET`), `CASE` expressions. Prefer the
other tools when they fit, because they check arguments and explain their results more.

| Argument | Default | Notes |
|---|---|---|
| `query` | required | Must start with `FROM <index>`. Don't repeat the time range in the query. End with `LIMIT`. |
| `time_range` | required | Up to 90 days, or up to 366 days if the query has a `STATS` command. `INLINE STATS` doesn't count, since it keeps every row. |
| `timestamp_field` | `@timestamp` | |

**Example:** 5xx errors per site per day for the past two weeks.

```json
{
  "query": "FROM ecs-ingress-nginx-access-* | WHERE http.response.status_code >= 500 | STATS errors = COUNT(*) BY day = BUCKET(@timestamp, 1 day), url.domain | SORT day, errors DESC | LIMIT 200",
  "time_range": {"start": "2026-09-15"}
}
```

```json
{
  "columns": ["errors", "day", "url.domain"],
  "returned": 31,
  "rows": [
    {"errors": 412, "day": "2026-09-15T00:00:00.000Z", "url.domain": "auth.gradata.com.au"},
    {"errors": 3, "day": "2026-09-15T00:00:00.000Z", "url.domain": "www.gradata.com.au"}
  ]
}
```

**Limits and tips**

- Results are capped at `ES_MCP_MAX_RESULT_SIZE` rows (default 500) and the response size budget.
  Aggregate with `STATS` rather than returning raw rows.
- The indices in `FROM` and any `LOOKUP JOIN` are checked against the exposed patterns. Subqueries
  and comments inside the `FROM` clause are rejected.
- Retention notes work as for the other tools, based on the indices in `FROM`.

---

## Pack tools

Each pack tool is a fixed query over one data source. Its index, filters and returned fields come
from the pack, and it takes a few named arguments. Every pack tool also takes:

- `time_range` (required). Search tools allow up to 90 days by default; top-values and
  distinct-values tools up to 366.
- `size`. The default comes from the pack; the maximum is 500 for search, 100 for top-values and
  1000 for distinct-values tools.

Results have the same shape as the generic tool of the same kind (`search_events`, `top_values`
or `distinct_values`), including retention notes.

### windows pack

Windows security events, `ecs-microsoft-windows-*`. Every event is kept for 60 days; after that,
only a long-term subset of user-account events. User names are lower case.

| Tool | Kind | Arguments (besides `time_range`, `size`) | Default size |
|---|---|---|---|
| `windows_active_users` | top values of `user.name` (user accounts only), with SID and domain | none | 50 |
| `windows_logon_users` | every account with a successful logon (4624), with logon count, first and last logon, SID and domain | none | 500 |
| `windows_user_events` | one account's events, most recent first | `user` (required) | 20 |
| `windows_remote_access_events` | RD Gateway and RDP connection events, chronological, all fields | `user` (required; logon name or SID) | 50 |
| `windows_failed_logons` | failed logons (4625), most recent first | `user` (optional) | 20 |
| `windows_process_executions` | processes started (4688), with command lines and parents | `user` (optional), `host` (optional; matches the start of the host name) | 20 |

**Examples**

Who logged in during the past year:

```json
windows_logon_users {"time_range": {"start": "2025-09-30"}}
```

```json
{
  "distinct_values": 214, "returned": 214,
  "values": [{"value": "john.smith1", "count": 1893, "first_seen": "2025-10-02T21:14:03.000Z",
              "last_seen": "2026-09-29T06:02:11.000Z", "sample": {"user.id": "S-1-5-21-...-1104", "user.domain": "INTRANET"}}],
  "note": "Microsoft Windows security events keeps every event for only 60 days (since 2026-07-31); before that it holds only a subset, so fewer or no events there don't mean nothing happened."
}
```

Here the note means that accounts which only logged on before 31 July may be missing from the
list.

Who was busiest in the last day:

```json
windows_active_users {"time_range": {"start": "2026-09-28T06:00:00Z"}}
```

Everything one account did yesterday:

```json
windows_user_events {"user": "john.smith1", "time_range": {"start": "2026-09-28", "end": "2026-09-28"}, "size": 100}
```

Reconstruct someone's remote sessions this week:

```json
windows_remote_access_events {"user": "john.smith1", "time_range": {"start": "2026-09-22"}}
```

Failed logons across all accounts in the last 24 hours (or pass `user` for one account):

```json
windows_failed_logons {"time_range": {"start": "2026-09-28T06:00:00Z"}, "size": 100}
```

Processes started on one server:

```json
windows_process_executions {"host": "habpw01", "time_range": {"start": "2026-09-27"}}
```

### fortios pack

FortiGate firewall logs, `ecs-fortios-*`. Every event is kept for 30 days. Local-traffic
sessions are kept for about 4 months, and VPN, system, IPS and selected forwarded traffic for
about 2 years. Log types are in `event.type_id`, e.g. `traffic-forward` or `event-vpn-tunnel-stats`.

| Tool | Kind | Arguments (besides `time_range`, `size`) | Default size |
|---|---|---|---|
| `fortios_connections_from` | events from one address, most recent first | `source_ip` (required), `destination_port` (optional, number) | 50 |
| `fortios_connections_to` | events to one address, most recent first | `destination_ip` (required), `destination_port` (optional, number) | 50 |
| `fortios_top_destinations` | top destination IPs for one source, with an example port, host name and application | `source_ip` (required) | 20 |
| `fortios_vpn_events` | VPN negotiation, tunnel and disconnect events | `source_ip` (optional; the remote peer) | 20 |
| `fortios_ips_events` | intrusion prevention detections, all fields | `source_ip` (optional) | 20 |

**Examples**

SSH attempts from one address today:

```json
fortios_connections_from {"source_ip": "192.0.2.10", "destination_port": 22, "time_range": {"start": "2026-09-29"}}
```

Who connected to a server over RDP this week:

```json
fortios_connections_to {"destination_ip": "198.51.100.20", "destination_port": 3389, "time_range": {"start": "2026-09-22"}}
```

What a workstation talked to most:

```json
fortios_top_destinations {"source_ip": "10.20.4.17", "time_range": {"start": "2026-09-28"}}
```

```json
{
  "total_events": 5120,
  "values": [{"value": "142.250.66.206", "count": 1210,
              "sample": {"destination.port": 443, "destination.domain": "www.google.com",
                         "destination.application.name": "Google.Services"}}],
  "events_with_other_values": 1733
}
```

VPN activity from one peer over the past quarter (VPN events are in the long-term tier):

```json
fortios_vpn_events {"source_ip": "203.0.113.7", "time_range": {"start": "2026-07-01"}}
```

IPS detections in the last day:

```json
fortios_ips_events {"time_range": {"start": "2026-09-28T06:00:00Z"}}
```

### ingress_nginx pack

Kubernetes ingress-nginx access logs, `ecs-ingress-nginx-access-*`. Every request is kept
indefinitely, so there are no retention notes. Requests to `healthcheck.apps.gradata.com.au` are
synthetic health checks; exclude them when counting real traffic.

| Tool | Kind | Arguments (besides `time_range`, `size`) | Default size |
|---|---|---|---|
| `nginx_top_clients` | client IPs making the most requests, with country and an example user agent | `domain` (optional) | 20 |
| `nginx_client_requests` | one client's requests, most recent first | `client_ip` (required), `domain` (optional) | 50 |
| `nginx_server_errors` | requests that failed with a 5xx status, most recent first | `domain` (optional) | 20 |
| `nginx_top_paths` | most requested URL paths | `domain` (optional), `client_ip` (optional) | 20 |

**Examples**

The heaviest clients of one site today:

```json
nginx_top_clients {"domain": "auth.gradata.com.au", "time_range": {"start": "2026-09-29"}}
```

Everything one client requested:

```json
nginx_client_requests {"client_ip": "203.0.113.7", "time_range": {"start": "2026-09-28"}}
```

Recent server errors on one site:

```json
nginx_server_errors {"domain": "auth.gradata.com.au", "time_range": {"start": "2026-09-29T00:00:00Z"}}
```

Whether one client is scanning (many different paths):

```json
nginx_top_paths {"client_ip": "203.0.113.7", "time_range": {"start": "2026-09-29"}, "size": 100}
```

## Worked example: "which users have logged in during the past year?"

1. `list_data_sources`: the `windows` source has `windows_logon_users` and `retention_days: 60`.
2. `windows_logon_users {"time_range": {"start": "2025-09-30"}}` returns every account with a
   successful logon, with first and last logon times.
3. The `note` says that before 31 July only a subset of logons is kept. So the list is complete
   for the last 60 days, and accounts that only logged on before that may be missing.

## Worked example: "which event types have stopped or slowed down since 1 September?"

1. `list_data_sources`: the `windows` source's `event_type_field` is `event.code`, and it keeps
   every event for 60 days.
2. `compare_periods {"index": "ecs-microsoft-windows-*", "before": {"start": "2026-08-01", "end": "2026-08-31"}, "after": {"start": "2026-09-01"}}`
   groups by `event.code` and lists the event types that stopped or dropped. The baseline is within
   60 days, so there's no retention note.
3. For each changed event type, run `compare_periods` again with
   `"group_by": ["event.code", "host.hostname"]` and a filter on that event code, to see which hosts
   account for the change. Then use `search_events` to look at the last events before it stopped.
