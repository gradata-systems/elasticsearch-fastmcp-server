from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from fastmcp import Client, FastMCP

from prompts import source_pack
from sources.packs import SourcePack, load_packs

REPO_PACKS = Path(__file__).parent.parent / 'packs'
MAPPING = '{"index_patterns": ["logs-app-*"], "template": {"mappings": {"properties": {"@timestamp": {"type": "date"}}}}}'


def _server(packs):
    @asynccontextmanager
    async def lifespan(server):
        yield {'packs': packs}

    mcp = FastMCP('t', lifespan=lifespan)
    for prompt in source_pack.ALL_PROMPTS:
        mcp.prompt(prompt)
    return mcp


async def _render(packs, **arguments) -> str:
    async with Client(_server(packs)) as client:
        result = await client.get_prompt('create_source_pack', arguments)
    return result.messages[0].content.text


async def test_prompt_lists_its_arguments():
    async with Client(_server([])) as client:
        prompt = next(p for p in await client.list_prompts() if p.name == 'create_source_pack')
    assert [(a.name, a.required) for a in prompt.arguments] == [
        ('mapping', True), ('use_cases', False), ('index', False)]


async def test_prompt_includes_mapping_schema_and_an_existing_pack():
    packs = load_packs(REPO_PACKS)
    text = await _render(packs, mapping=MAPPING, index='logs-app-*')

    assert '"index_patterns": [\n    "logs-app-*"' in text  # pretty-printed
    assert 'The pack will cover `logs-app-*`.' in text
    assert '"retention_days"' in text and '"event_type_field"' in text  # schema from the pack model
    # The example is a real, valid pack, and every existing tool name is listed to avoid clashes.
    example = text.split('## An existing pack, for reference\n\n```yaml\n')[1].split('```')[0]
    SourcePack.model_validate(yaml.safe_load(example))
    assert all(spec.name in text.split('## Existing tools')[1] for pack in packs for spec in pack.tools)
    assert 'Ask the user what questions they want agents to answer' in text
    assert "It isn't added to this server" in text and "Don't write it\nanywhere yourself" in text


async def test_prompt_uses_given_use_cases_and_copes_without_packs():
    text = await _render([], mapping='not json {', use_cases='Who signed in this week?\nTop error paths')
    assert 'Who signed in this week?\nTop error paths' in text
    assert 'Ask the user what questions' not in text
    assert '## An existing pack' not in text and '## Existing tools' not in text
    assert '```json\nnot json {\n```' in text


async def test_prompt_asks_for_yaml_checked_by_the_validation_tool():
    text = await _render([], mapping=MAPPING)
    assert 'Write it in YAML, never JSON' in text
    assert f'```yaml\n{source_pack.SKELETON}```' in text
    assert '`validate_source_pack`' in text
