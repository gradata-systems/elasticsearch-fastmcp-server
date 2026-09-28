from security.policy import Caller, RbacPolicy, roles_from_claims

POLICY = RbacPolicy.model_validate({
    'roles': {
        'soc-analyst': {'indices': ['ecs-microsoft-windows-*', 'ecs-nginx-*']},
        'helpdesk': {'indices': ['ecs-microsoft-windows-*']},
    }
})

CLAIMS = {
    'realm_access': {'roles': ['helpdesk', 'offline_access']},
    'resource_access': {'es-mcp': {'roles': ['soc-analyst']}},
}


def test_client_roles_used_when_client_id_set():
    assert roles_from_claims(CLAIMS, 'es-mcp') == {'soc-analyst'}


def test_realm_roles_used_when_no_client_id():
    assert roles_from_claims(CLAIMS, None) == {'helpdesk', 'offline_access'}


def test_missing_claims_yield_no_roles():
    assert roles_from_claims({}, 'es-mcp') == frozenset()
    assert roles_from_claims({}, None) == frozenset()


def test_patterns_are_union_of_mapped_roles_and_unknown_roles_grant_nothing():
    assert POLICY.index_patterns_for({'soc-analyst', 'helpdesk', 'offline_access'}) == {
        'ecs-microsoft-windows-*', 'ecs-nginx-*'}
    assert POLICY.index_patterns_for({'offline_access'}) == frozenset()


def test_may_read_matches_patterns_only():
    caller = Caller('sub', frozenset({'helpdesk'}), POLICY.index_patterns_for({'helpdesk'}))
    assert caller.may_read('ecs-microsoft-windows-v1')
    assert not caller.may_read('ecs-nginx-v1')
    assert not caller.may_read('*')
    assert not caller.may_read('ecs-microsoft-windows-v1,ecs-nginx-v1')
