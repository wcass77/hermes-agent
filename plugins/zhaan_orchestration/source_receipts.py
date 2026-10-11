"""Private exact originals and verified Git catalog derivatives for router intake.

Raw email may contain credential-bearing URLs. Retain it outside the agent's
Git checkout; a redacted catalog copy does not prove exact-original preservation.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any


class SourceReceiptError(RuntimeError):
    """Safe diagnostics: never include email contents or credential-bearing URLs."""


def _read(path: Path, *, private: bool = False) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise SourceReceiptError('Source receipt file must be an owned regular file.')
        if private and info.st_mode & 0o077:
            raise SourceReceiptError('Private source receipt permissions are unsafe.')
        return handle.read()


def _directory(path: Path) -> None:
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise SourceReceiptError('Private source receipt directory is unsafe.')


def _immutable(path: Path, data: bytes) -> None:
    # Link a complete fsynced file without replacing an existing receipt.
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        try:
            os.link(temporary, path)
            fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except FileExistsError:
            if _read(path, private=True) != data:
                raise SourceReceiptError('Conflicting immutable source receipt.')
    finally:
        temporary.unlink()


def _json(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + '\n').encode()


def preserve_source(root: Path, source: Path, item: dict[str, Any]) -> dict[str, Any]:
    data = _read(source)
    record = json.loads(data)
    headers = {k.casefold(): v for k, v in record['headers'].items()}
    operation = headers.get('x-personal-os-routing-operation')
    if (headers.get('x-personal-os-routing-version') != '1'
            or not isinstance(operation, str) or not re.fullmatch('[0-9a-f]{64}', operation)):
        raise SourceReceiptError('Original source routing identity is invalid.')
    if any(record.get(key) != item[key] for key in ('inbox_id', 'message_id', 'thread_id')):
        raise SourceReceiptError('Original source differs from signed ingress identity.')
    event = item['event_id']
    if not isinstance(event, str) or not event:
        raise SourceReceiptError('Signed event identity is required.')
    digest = hashlib.sha256(data).hexdigest()
    binding = {'schema_version': 1, 'event_id': event, 'message_id': item['message_id'],
               'inbox_id': item['inbox_id'], 'thread_id': item['thread_id'],
               'operation_id': operation, 'source_sha256': digest}
    # The state parent is host-owned, not the writable logistics workspace.
    if root.parent.resolve() != root.parent:
        raise SourceReceiptError('Private source state parent is a symlink.')
    _directory(root)
    _directory(root / 'sha256')
    bucket = root / 'sha256' / digest
    _directory(bucket)
    _immutable(bucket / 'original.json', data)
    _directory(root / 'events')
    key = hashlib.sha256(event.encode()).hexdigest()
    _immutable(root / 'events' / f'{key}.json', _json(binding))
    return binding


def verify_source(root: Path, binding: dict[str, Any]) -> dict[str, Any]:
    if root.parent.resolve() != root.parent:
        raise SourceReceiptError('Private source state parent is a symlink.')
    digest = binding['source_sha256']
    if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
        raise SourceReceiptError('Invalid original source digest.')
    for path in (root, root / 'sha256', root / 'sha256' / digest, root / 'events'):
        # Validate existing directories only: verification never creates evidence.
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise SourceReceiptError('Private source receipt directory is unsafe.')
    key = hashlib.sha256(binding['event_id'].encode()).hexdigest()
    if json.loads(_read(root / 'events' / f'{key}.json', private=True)) != binding:
        raise SourceReceiptError('Original source event binding differs.')
    data = _read(root / 'sha256' / digest / 'original.json', private=True)
    if hashlib.sha256(data).hexdigest() != digest:
        raise SourceReceiptError('Private original source integrity failed.')
    return json.loads(data)


def verify_catalog(workspace: Path, root: Path, binding: dict[str, Any]) -> dict[str, str]:
    original = verify_source(root, binding)
    try:
        catalog = _read(workspace / 'documents/CATALOG.md').decode()
    except FileNotFoundError:
        raise SourceReceiptError('Original source has no verified catalog derivative.') from None
    matches = []
    for entry in re.split(r'(?m)(?=^## )', catalog):
        document = re.match(r'## ([a-z][a-z0-9-]*) — ', entry)
        digest = re.search(r'^- SHA-256: `([0-9a-f]{64})`$', entry, re.M)
        archive = re.search(r'^- Archive path: `([^`\n]+)`$', entry, re.M)
        receipt = re.search(r'^- Receipt ID: `([^`\n]+)`$', entry, re.M)
        provenance = re.search(r'^- Source: (.*)$', entry, re.M)
        if not all((document, digest, archive, receipt, provenance)):
            continue
        if binding['message_id'] not in provenance[1]:
            continue
        relative = Path(archive[1])
        expected = Path('documents/archive/sha256') / digest[1][:2] / digest[1]
        if relative.parent != expected or relative.name != 'personal-os-source-email.json':
            continue
        path = workspace / relative
        if path.resolve() != path or not path.is_file():
            raise SourceReceiptError('Catalog source archive path is unsafe or missing.')
        data = _read(path)
        if hashlib.sha256(data).hexdigest() != digest[1]:
            raise SourceReceiptError('Catalog source archive integrity failed.')
        record = json.loads(data)
        if any(record.get(k) != original.get(k) for k in ('inbox_id', 'thread_id', 'message_id', 'from')):
            continue
        headers = {k.casefold(): v for k, v in record.get('headers', {}).items()}
        if (headers.get('x-personal-os-routing-operation') != binding['operation_id']
                or headers.get('x-personal-os-routing-version') != '1'):
            continue
        matches.append({'document_id': document[1], 'receipt_id': receipt[1],
                        'archive_path': archive[1], 'derivative_sha256': digest[1]})
    if not matches:
        raise SourceReceiptError('Original source has no verified catalog derivative.')
    # Multiple real receipts are allowed; select one stable verifiable link.
    return min(matches, key=lambda entry: (entry['document_id'], entry['receipt_id']))


def complete_source(workspace: Path, root: Path, binding: dict[str, Any]) -> dict[str, Any]:
    derivative = verify_catalog(workspace, root, binding)
    result = {'source': binding, 'catalog': derivative}
    _directory(root / 'completed')
    key = hashlib.sha256(binding['event_id'].encode()).hexdigest()
    _immutable(root / 'completed' / f'{key}.json', _json(result))
    return result
