"""The declarative pipeline cannot run code or publish incomplete processing results."""
from copy import deepcopy

import pytest

from backend.processing import (
    STAGE_ORDER,
    PipelineConfig,
    PipelineError,
    StageConfig,
    load_pipeline,
    process_document,
    validate_source_map,
)
from backend.storage import LocalContentStore
from tests.helpers import digest, text_pdf


def config_with(stage=None, **options):
    return {'version': 1, 'stages': [
        {'name': name, 'options': options if name == stage else {}} for name in STAGE_ORDER]}


@pytest.mark.parametrize('raw,media_type', [
    ('政策证据：风险降低 20%。\n\nCafé evidence.'.encode(), 'text/plain'),
    (b'<article><p>Saved HTML research evidence.</p></article>', 'text/html'),
    (text_pdf('PDF page one evidence.', 'PDF page two limitations.'), 'application/pdf'),
])
def test_real_pipeline_verifies_saved_bytes_maps_and_index(tmp_path, raw, media_type):
    store = LocalContentStore(tmp_path)
    result = process_document(raw, media_type, store=store)
    assert result.ready_for_publication is True
    assert result.raw_hash == digest(raw)
    assert store.get(result.raw_hash) == raw
    assert result.gates == {'raw_verified': True, 'source_map_valid': True,
                            'quality_passed': True, 'lexical_index_ready': True}
    assert validate_source_map(result.extracted_text, result.blocks) is True
    block_ids = {block['id'] for block in result.blocks}
    assert result.lexical_index
    assert all(set(ids) <= block_ids for ids in result.lexical_index.values())
    assert result.metadata['config_hash'] == result.config_hash
    assert result.metadata['parser_version'] == result.parser_version


def test_reprocessing_preserves_raw_and_versions_derived_identity(tmp_path):
    raw = ('Repeated policy evidence and limitations. ' * 20).encode()
    store = LocalContentStore(tmp_path)
    first = process_document(raw, 'text/plain', store=store)
    retry = process_document(raw, 'text/plain', store=store)
    second = process_document(raw, 'text/plain', pipeline_config=config_with('source_map', max_block_chars=100),
                              store=store)
    assert first == retry
    assert first.raw_hash == second.raw_hash
    assert first.extracted_text == second.extracted_text
    assert first.config_hash != second.config_hash
    assert len(second.blocks) > len(first.blocks)
    assert {block['id'] for block in first.blocks}.isdisjoint(block['id'] for block in second.blocks)
    assert validate_source_map(first.extracted_text, first.blocks)
    assert validate_source_map(second.extracted_text, second.blocks)
    assert store.get(first.raw_hash) == raw


@pytest.mark.parametrize('change', ['bad_text', 'bad_hash', 'negative', 'out_of_bounds', 'overlap', 'bad_page'])
def test_source_map_validation_detects_corrupt_anchors(tmp_path, change):
    result = process_document(b'First source paragraph.\n\nSecond source paragraph.', 'text/plain',
                              store=LocalContentStore(tmp_path))
    blocks = deepcopy(result.blocks)
    if change == 'bad_text':
        blocks[0]['text'] = 'Invented quote'
    elif change == 'bad_hash':
        blocks[0]['quote_hash'] = '0' * 64
    elif change == 'negative':
        blocks[0]['start_offset'] = -1
    elif change == 'out_of_bounds':
        blocks[0]['end_offset'] = len(result.extracted_text) + 1
    elif change == 'overlap':
        blocks[1] = deepcopy(blocks[0])
    else:
        blocks[0]['page'] = 0
    assert validate_source_map(result.extracted_text, blocks) is False


def test_safe_yaml_cannot_execute_python(tmp_path):
    sentinel = tmp_path / 'must-not-exist'
    yaml = f'!!python/object/apply:os.system ["touch {sentinel}"]'
    with pytest.raises(PipelineError, match='safe, valid YAML'):
        load_pipeline(yaml)
    assert not sentinel.exists()


@pytest.mark.parametrize('yaml', [
    'version: 1\nversion: 1\nstages: []',
    'version: 1\nstages: &stages [*stages]',
    'version: 1\nstages: !!python/name:builtins.eval',
    'version: 1\nstages: []\n' + '#' * 33_000,
])
def test_unsafe_yaml_features_are_rejected(yaml):
    with pytest.raises(PipelineError):
        load_pipeline(yaml)


@pytest.mark.parametrize('change', ['unknown_stage', 'missing_gate', 'reorder', 'unknown_option',
                                  'bool_as_int', 'out_of_bounds', 'unknown_top_level', 'wrong_version'])
def test_stage_registry_cannot_be_bypassed(change):
    config = config_with()
    if change == 'unknown_stage':
        config['stages'][0]['name'] = 'os.system'
    elif change == 'missing_gate':
        config['stages'].pop(3)
    elif change == 'reorder':
        config['stages'][0], config['stages'][1] = config['stages'][1], config['stages'][0]
    elif change == 'unknown_option':
        config['stages'][1]['options']['command'] = 'shell-command'
    elif change == 'bool_as_int':
        config['stages'][0]['options']['max_bytes'] = True
    elif change == 'out_of_bounds':
        config['stages'][0]['options']['max_bytes'] = 1024 * 1024 * 101
    elif change == 'unknown_top_level':
        config['executor'] = 'arbitrary'
    else:
        config['version'] = True
    with pytest.raises(PipelineError):
        load_pipeline(config)


def test_constructed_pipeline_dataclass_is_revalidated():
    injected = PipelineConfig(1, (StageConfig('exec', {'command': 'arbitrary'}),))
    with pytest.raises(PipelineError):
        load_pipeline(injected)


def test_config_hash_is_stable_and_option_sensitive():
    default = load_pipeline()
    explicit = load_pipeline(config_with())
    assert default.config_hash == explicit.config_hash
    changed = load_pipeline(config_with('source_map', max_block_chars=100))
    assert default.config_hash != changed.config_hash
    assert len(default.config_hash) == 64


@pytest.mark.parametrize('raw,config', [
    (b'', None),
    (b'Four', config_with('raw_verify', max_bytes=3)),
    (b'A', config_with('quality', min_characters=20)),
    (b'\xff\xff\xffText', None),
    (b'a b c', config_with('lexical_index', minimum_token_length=10)),
    (b'     ', None),
])
def test_incomplete_gate_fails_instead_of_claiming_publication_ready(tmp_path, raw, config):
    with pytest.raises(PipelineError):
        process_document(raw, 'text/plain', pipeline_config=config, store=LocalContentStore(tmp_path))


def test_privacy_transform_keeps_raw_immutable_and_maps_transformed_quotes_exactly(tmp_path):
    raw = b'Contact researcher@example.org or +1 415 555 0100. Policy evidence remains.'
    store = LocalContentStore(tmp_path)
    result = process_document(raw, 'text/plain', store=store,
                              pipeline_config=config_with('normalize_privacy', redact_emails=True, redact_phones=True))
    assert store.get(result.raw_hash) == raw
    assert 'researcher@example.org' not in result.extracted_text
    assert '415 555 0100' not in result.extracted_text
    assert '[EMAIL_REDACTED]' in result.extracted_text
    assert '[PHONE_REDACTED]' in result.extracted_text
    assert validate_source_map(result.extracted_text, result.blocks)
    privacy = result.metadata['privacy']
    assert privacy['complete_dlp'] is False
    assert privacy['source_map_basis'] == 'stored_transformed_representation'
    assert {change['kind'] for change in privacy['transformations']} == {'email', 'phone'}
    assert result.metadata['parser_isolation']['subprocess'] is True
    assert result.metadata['parser_isolation']['memory_mb'] > 0
