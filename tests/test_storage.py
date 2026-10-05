"""Raw byte content addressing must be reproducible, immutable and self-verifying."""
import pytest

from backend.storage import LocalContentStore, StorageError
from tests.helpers import digest


def test_content_addressed_object_roundtrip_and_idempotency(tmp_path):
    store = LocalContentStore(tmp_path / 'objects')
    content = b'Original research bytes.\x00\xff\r\n'
    first = store.put(content, 'application/octet-stream')
    path = store.root / first.key
    inode = path.stat().st_ino
    second = store.put(content, 'application/octet-stream')
    assert first == second
    assert first.sha256 == digest(content)
    assert first.size == len(content)
    assert path.stat().st_ino == inode
    assert store.get(first.sha256) == content
    assert store.verify(first.sha256) is True
    changed = store.put(content + b'New revision')
    assert changed.sha256 != first.sha256
    assert store.get(first.sha256) == content


def test_tampered_raw_object_is_rejected_and_not_overwritten(tmp_path):
    store = LocalContentStore(tmp_path)
    saved = store.put(b'Original')
    path = store.root / saved.key
    path.chmod(0o600)
    path.write_bytes(b'Tampered')
    with pytest.raises(StorageError, match='digest mismatch'):
        store.get(saved.sha256)
    assert store.verify(saved.sha256) is False
    with pytest.raises(StorageError, match='digest mismatch'):
        store.put(b'Original')
    assert path.read_bytes() == b'Tampered'


@pytest.mark.parametrize('identifier', ['../../etc/passwd', '', 'a' * 63, 'g' * 64, 'A' * 64])
def test_content_store_rejects_non_digest_paths(tmp_path, identifier):
    with pytest.raises(StorageError, match='SHA-256'):
        LocalContentStore(tmp_path).get(identifier)


def test_raw_object_symlink_escape_is_rejected(tmp_path):
    root = tmp_path / 'store'
    store = LocalContentStore(root)
    saved = store.put(b'Research')
    outside = tmp_path / 'outside'
    outside.write_bytes(b'Research')
    path = root / saved.key
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(StorageError, match='escapes'):
        store.get(saved.sha256)
