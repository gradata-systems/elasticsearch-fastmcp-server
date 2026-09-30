from datetime import datetime, timezone

import pytest
from fastmcp.exceptions import ToolError
from pydantic import ValidationError

from tools.query import Filter, TimeRange, build_query, fit_to_budget


def test_date_only_end_covers_whole_day_and_naive_is_utc():
    tr = TimeRange(start='2026-09-01', end='2026-09-02')
    assert tr.start == datetime(2026, 9, 1, tzinfo=timezone.utc)
    assert tr.to_query('@timestamp', 90) == {'range': {'@timestamp': {
        'gte': '2026-09-01T00:00:00.000+00:00', 'lte': '2026-09-02T23:59:59.999+00:00'}}}


def test_end_before_start_rejected():
    with pytest.raises(ValidationError):
        TimeRange(start='2026-09-02', end='2026-09-01')


def test_span_limit_enforced():
    with pytest.raises(ToolError, match='maximum of 7 days'):
        TimeRange(start='2026-09-01', end='2026-09-10').to_query('@timestamp', 7)


def test_open_ended_range_ends_now():
    q = TimeRange(start=datetime.now(timezone.utc).date().isoformat()).to_query('@timestamp', 1)
    assert q['range']['@timestamp']['lte'] > q['range']['@timestamp']['gte']


@pytest.mark.parametrize('kwargs', [
    {'field': 'f', 'op': 'eq'},
    {'field': 'f', 'op': 'eq', 'value': ['a']},
    {'field': 'f', 'op': 'in', 'value': 'a'},
    {'field': 'f', 'op': 'in', 'value': []},
    {'field': 'f', 'op': 'exists', 'value': 'a'},
])
def test_filter_value_must_match_op(kwargs):
    with pytest.raises(ValidationError):
        Filter(**kwargs)


def test_build_query_combines_filters_negations_and_full_text():
    q = build_query(
        TimeRange(start='2026-09-01', end='2026-09-01'), 'ts', 90,
        [Filter(field='user.name', value='alice'),
         Filter(field='event.code', op='in', value=[4624, 4625]),
         Filter(field='source.port', op='gte', value=1024),
         Filter(field='user.name', op='prefix', value='svc_', negate=True)],
        'message:"failed password"')
    b = q['bool']
    assert b['filter'][1:] == [{'term': {'user.name': 'alice'}}, {'terms': {'event.code': [4624, 4625]}},
                               {'range': {'source.port': {'gte': 1024}}}]
    assert 'ts' in b['filter'][0]['range']
    assert b['must_not'] == [{'prefix': {'user.name': 'svc_'}}]
    assert b['must'] == [{'query_string': {'query': 'message:"failed password"', 'allow_leading_wildcard': False}}]


def test_fit_to_budget():
    rows = [{'a': 'x' * 10}] * 5  # 17 chars each
    assert fit_to_budget(rows, 40) == (rows[:2], True)
    assert fit_to_budget(rows, 1000) == (rows, False)


def test_long_strings_are_shortened_to_fit():
    from tools.query import MAX_VALUE_CHARS
    long = 'x' * (MAX_VALUE_CHARS + 500)
    rows, truncated = fit_to_budget([{'message': long, 'tags': [long], 'n': 1}], MAX_VALUE_CHARS * 3)
    assert not truncated
    message = rows[0]['message']
    assert message.startswith('x' * MAX_VALUE_CHARS) and message.endswith('... [500 more characters not shown]')
    assert rows[0]['tags'] == [message] and rows[0]['n'] == 1
