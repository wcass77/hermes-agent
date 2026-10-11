"""Exact private bytes and same-message catalog derivatives are both required."""
import hashlib
import json
from pathlib import Path

import pytest

from plugins.zhaan_orchestration.automated_intake import stage_source
from plugins.zhaan_orchestration.source_receipts import (
    SourceReceiptError, preserve_source, verify_source, complete_source,
)


def evidence(tmp_path, filename="personal-os-source-email.json"):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    root = state / 'automated-intake-sources'
    message = {'inbox_id': 'in', 'message_id': '<message@example.test>',
               'thread_id': 'thread', 'from': 'participant@example.test',
               'text': 'https://example.test/?token=synthetic-private',
               'attachments': [], 'headers': {
                   'X-Personal-OS-Routing-Version': '1',
                   'X-Personal-OS-Routing-Operation': 'a' * 64}}
    item = {key: message[key] for key in ('inbox_id', 'message_id', 'thread_id')}
    item['event_id'] = 'event'
    source = stage_source(message, workspace / 'inbox')
    binding = preserve_source(root, source, item)
    derivative = json.loads(source.read_text())
    derivative['text'] = 'https://example.test/?token=[redacted]'
    data = json.dumps(derivative).encode()
    digest = hashlib.sha256(data).hexdigest()
    path = workspace / 'documents/archive/sha256' / digest[:2] / digest / filename
    path.parent.mkdir(parents=True)
    path.write_bytes(data)
    catalog = workspace / 'documents/CATALOG.md'
    catalog.write_text(f'''# Documents

## doc-test — {source.name}

- SHA-256: `{digest}`
- Archive path: `{path.relative_to(workspace)}`
- Receipt ID: `receipt-test`
- Source: AgentMail message {message['message_id']}
''')
    return workspace, root, source, item, binding, path, catalog


def test_private_original_and_derivative_survive_idempotent_retry(tmp_path):
    workspace, root, source, item, binding, path, _ = evidence(tmp_path)
    result = complete_source(workspace, root, binding)
    assert result == complete_source(workspace, root, preserve_source(root, source, item))
    original = root / 'sha256' / binding['source_sha256'] / 'original.json'
    assert original.read_bytes() == source.read_bytes()
    assert original.read_bytes() != path.read_bytes()
    assert original.stat().st_mode & 0o777 == 0o600
    assert original.parent.stat().st_mode & 0o777 == 0o700
    assert result['catalog']['derivative_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert root not in workspace.parents and workspace not in root.parents


@pytest.mark.parametrize('corruption', ['original', 'derivative', 'catalog', 'identity',
                                         'private_symlink', 'archive_symlink', 'permissions',
                                         'conflict', 'receipt_missing'])
def test_completion_fails_without_untampered_matching_evidence(tmp_path, corruption):
    workspace, root, source, item, binding, path, catalog = evidence(tmp_path)
    original = root / 'sha256' / binding['source_sha256'] / 'original.json'
    if corruption == 'original':
        original.write_bytes(b'changed')
    elif corruption == 'derivative':
        path.write_bytes(b'changed')
    elif corruption == 'catalog':
        catalog.write_text('# No receipt')
    elif corruption == 'identity':
        catalog.write_text(catalog.read_text().replace(item['message_id'], '<other@example.test>'))
    elif corruption == 'private_symlink':
        data = original.read_bytes()
        original.unlink()
        target = tmp_path / 'elsewhere'; target.write_bytes(data)
        original.symlink_to(target)
    elif corruption == 'archive_symlink':
        data = path.read_bytes()
        path.unlink()
        target = tmp_path / 'elsewhere'; target.write_bytes(data)
        path.symlink_to(target)
    elif corruption == 'permissions':
        original.chmod(0o644)
    elif corruption == 'conflict':
        source.write_text(source.read_text().replace('synthetic-private', 'changed-value'))
        with pytest.raises(SourceReceiptError, match='Conflicting'):
            preserve_source(root, source, item)
        return
    else:
        next((root / 'events').glob('*.json')).unlink()
    with pytest.raises((SourceReceiptError, OSError)):
        complete_source(workspace, root, binding)
    assert not (root / 'completed').exists()


def test_descriptive_json_filename_retains_verified_provenance(tmp_path):
    workspace, root, _, _, binding, path, _ = evidence(
        tmp_path, 'school-weekly-2026-10-11-redacted.json')
    result = complete_source(workspace, root, binding)
    assert result['catalog']['archive_path'] == str(path.relative_to(workspace))
    assert result['catalog']['derivative_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize('mismatch', ['operation', 'message', 'outside_bucket', 'traversal', 'non_json'])
def test_descriptive_archive_still_requires_safe_same_source(tmp_path, mismatch):
    workspace, root, _, _, binding, path, catalog = evidence(tmp_path, 'school-redacted.json')
    if mismatch in ('operation', 'message'):
        data = json.loads(path.read_bytes())
        if mismatch == 'operation':
            data['headers']['X-Personal-OS-Routing-Operation'] = 'b' * 64
        else:
            data['message_id'] = '<other@example.test>'
        encoded = json.dumps(data).encode()
        digest = hashlib.sha256(encoded).hexdigest()
        replacement = path.parent.parent / digest / path.name
        replacement.parent.mkdir()
        replacement.write_bytes(encoded)
        catalog.write_text(catalog.read_text().replace(
            hashlib.sha256(path.read_bytes()).hexdigest(), digest))
    else:
        replacement = {'outside_bucket': 'documents/other/school-redacted.json',
                       'traversal': str(path.relative_to(workspace).parent / '..' / path.parent.name / path.name),
                       'non_json': str(path.relative_to(workspace).with_suffix('.txt'))}[mismatch]
        catalog.write_text(catalog.read_text().replace(str(path.relative_to(workspace)), replacement))
    with pytest.raises(SourceReceiptError):
        complete_source(workspace, root, binding)
    assert not (root / 'completed').exists()
