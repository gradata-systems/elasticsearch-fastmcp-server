import pytest

from security.esql import UnsupportedQuery, source_indices


@pytest.mark.parametrize('query, expected', [
    ('FROM ecs-fortios-v1 | LIMIT 1', ['ecs-fortios-v1']),
    ('from a-*, b METADATA _id | STATS n = COUNT(*)', ['a-*', 'b']),
    ('FROM "quoted-idx",`tick`', ['quoted-idx', 'tick']),
    ('// note\n/* block */  FROM a | LIMIT 5', ['a']),
    ('FROM a | LOOKUP JOIN threat_intel ON source.ip | LIMIT 5', ['a', 'threat_intel']),
    ('TS metrics-* | STATS max(cpu)', ['metrics-*']),
    ('FROM remote:logs | LIMIT 1', ['remote:logs']),
])
def test_source_indices(query, expected):
    assert source_indices(query) == expected


@pytest.mark.parametrize('query', [
    'ROW a = 1',
    'SHOW INFO',
    'FROM a, (FROM b | LIMIT 1) | LIMIT 1',
    'FROM a /* hidden */, b | LIMIT 1',
    'FROM a,, b | LIMIT 1',
    '',
])
def test_unsupported_queries_are_rejected(query):
    with pytest.raises(UnsupportedQuery):
        source_indices(query)


def test_lookup_join_inside_string_only_makes_check_stricter():
    # False positives just add an index to check; they can't hide one.
    assert source_indices('FROM a | WHERE msg == "LOOKUP JOIN x" | LIMIT 1') == ['a', 'x']
