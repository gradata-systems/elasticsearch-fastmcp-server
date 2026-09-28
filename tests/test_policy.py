import pytest

from security.policy import AccessPolicy, Caller, matches_index_expression

POLICY = AccessPolicy.model_validate({
    'exposed_indices': ['ecs-microsoft-windows-*', 'ecs-ingress-nginx-*'],
    'impersonable_users': ['*.*', 'service-account-*'],
})


@pytest.mark.parametrize('username', ['john.smith1', 'anja.michel', 'service-account-langgraph-triage'])
def test_people_and_agents_are_impersonable(username):
    assert POLICY.may_impersonate(username, 'mcp_impersonator')


@pytest.mark.parametrize('username', [
    'elastic', 'kibana_system',          # built-in, never impersonated
    'svc_avw_api', 'admin',              # outside the configured patterns
    'mcp_impersonator',                  # the impersonation account itself
    '_internal.user',                    # reserved prefix
    'john.smith1,elastic',               # multiple users in one header value
    'john.smith1\r\nx-evil: 1', 'john .smith', '',  # header injection / junk
    'a' * 257 + '.b',
])
def test_unsafe_usernames_are_refused(username):
    assert not POLICY.may_impersonate(username, 'mcp_impersonator')


def test_reserved_users_refused_even_if_patterns_allow_everything():
    permissive = AccessPolicy(exposed_indices=['*'], impersonable_users=['*'])
    assert not permissive.may_impersonate('elastic', 'mcp_impersonator')


def test_may_read_matches_exposed_patterns_only():
    caller = Caller('sub', 'john.smith1', frozenset(POLICY.exposed_indices))
    assert caller.may_read('ecs-microsoft-windows-v1')
    assert caller.may_read('ecs-microsoft-windows-v1,ecs-ingress-nginx-access-v1')
    assert not caller.may_read('ecs-fortios-v1')
    assert not caller.may_read('*')
    assert not caller.may_read('ecs-microsoft-windows-v1,ww2_nomroll')
    assert not caller.may_read('remote:ecs-microsoft-windows-v1')
    assert not caller.may_read('ecs-microsoft-windows-v1,')
    assert not caller.may_read('')


def test_may_read_allows_exclusions_after_a_target():
    caller = Caller('sub', 'john.smith1', frozenset(POLICY.exposed_indices))
    assert caller.may_read('ecs-microsoft-windows-*,-ecs-microsoft-windows-rs-*')
    assert caller.may_read('ecs-microsoft-windows-*,-anything-*')   # exclusions only narrow
    assert not caller.may_read('-ecs-fortios-v1,ecs-microsoft-windows-v1')
    assert not caller.may_read('-ecs-microsoft-windows-v1')
    assert not caller.may_read('ecs-microsoft-windows-*,-')
    assert not caller.may_read('ecs-microsoft-windows-*,-ecs-microsoft-windows-rs-*,ecs-fortios-v1')


@pytest.mark.parametrize('name, expression, expected', [
    ('logs-app', 'logs-*', True),
    ('logs-app', 'audit,logs-app', True),
    ('logs-debug-1', 'logs-*,-logs-debug-*', False),
    ('logs-app', 'logs-*,-logs-debug-*', True),
    ('logs-debug-1', 'logs-*,-logs-debug-*,logs-debug-1', True),   # later items win, as in ES
    ('other', 'logs-*,-logs-debug-*', False),
])
def test_matches_index_expression(name, expression, expected):
    assert matches_index_expression(name, expression) is expected
