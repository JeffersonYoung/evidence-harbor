"""Real SQLite/object backups, portable restore, overwrite refusal and archive validation."""
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from backend import db, domain
from backend import models as m
from scripts.backup_local import backup, restore
from tests.helpers import digest
from tests.test_domain_integrity import evidence_for, ingest_text, make_project


def minimal_database(path):
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE test_data (id INTEGER PRIMARY KEY, value TEXT NOT NULL)')
        connection.execute('INSERT INTO test_data VALUES (1, ?)', ('Preserved research',))
    return path.read_bytes()


def write_archive(path, entries, *, hashes=None):
    manifest = {'format': 'evidenceharbor-local-backup-v1', 'files': []}
    with zipfile.ZipFile(path, 'w') as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
            manifest['files'].append({'path': name, 'sha256': (hashes or {}).get(name, digest(data)), 'bytes': len(data)})
        archive.writestr('backup-manifest.json', json.dumps(manifest))


def test_real_research_backup_restores_database_raw_bytes_and_evidence_at_new_root(runtime, tmp_path):
    project_id = make_project(runtime)
    operation = ingest_text(runtime, project_id)
    evidence_id = evidence_for(runtime, project_id, operation)
    archive_path = tmp_path / 'research-backup.zip'
    original_objects = runtime.settings.blob_dir
    source_db = Path(runtime.engine.url.database)
    manifest = backup(source_db, original_objects, archive_path)
    assert manifest['format'] == 'evidenceharbor-local-backup-v1'
    assert len(manifest['files']) >= 2
    with zipfile.ZipFile(archive_path) as archive:
        for item in manifest['files']:
            assert digest(archive.read(item['path'])) == item['sha256']
    destination = tmp_path / 'restored-research'
    restored = restore(archive_path, destination)
    assert restored == manifest
    # Remove access to the old object location so this checks portable references,
    # rather than accidentally reading original objects through stored absolute paths.
    original_objects.rename(tmp_path / 'original-objects-unavailable')
    runtime.settings.blob_dir = destination / 'objects'
    engine = db.make_engine(f'sqlite:///{destination / "workspace.sqlite"}')
    try:
        with Session(engine) as session:
            assert session.get(m.Project, project_id).name == 'Research project'
            capture = session.get(m.Capture, operation['result_json']['capture_id'])
            assert digest(domain.read_blob(capture)) == capture.content_hash
            anchored = domain.validate_evidence(session, 'workspace-a', project_id, [evidence_id])
            assert anchored[0].quote == 'measurable reduction'
    finally:
        engine.dispose()


def test_backup_and_restore_refuse_existing_outputs(tmp_path):
    source = tmp_path / 'source.sqlite'
    minimal_database(source)
    objects = tmp_path / 'objects'
    objects.mkdir()
    archive = tmp_path / 'backup.zip'
    backup(source, objects, archive)
    original = archive.read_bytes()
    with pytest.raises(ValueError, match='overwrite'):
        backup(source, objects, archive)
    assert archive.read_bytes() == original
    target = tmp_path / 'existing'
    target.mkdir()
    sentinel = target / 'keep.txt'
    sentinel.write_text('User data')
    with pytest.raises(ValueError, match='must not exist'):
        restore(archive, target)
    assert sentinel.read_text() == 'User data'


def test_corrupt_archive_hash_is_rejected_before_destination_is_created(tmp_path):
    data = minimal_database(tmp_path / 'source.sqlite')
    archive = tmp_path / 'corrupt.zip'
    write_archive(archive, {'workspace.sqlite': data}, hashes={'workspace.sqlite': '0' * 64})
    target = tmp_path / 'restored'
    with pytest.raises(ValueError, match='hash'):
        restore(archive, target)
    assert not target.exists()


@pytest.mark.parametrize('unsafe', ['../outside.txt', 'objects/../../outside.txt', '/tmp/backup-escape.txt'])
def test_archive_path_traversal_is_rejected_before_writing(tmp_path, unsafe):
    data = minimal_database(tmp_path / 'source.sqlite')
    archive = tmp_path / 'unsafe.zip'
    write_archive(archive, {'workspace.sqlite': data, unsafe: b'Untrusted bytes'})
    target = tmp_path / 'restored'
    with pytest.raises(ValueError, match='Unsafe'):
        restore(archive, target)
    assert not target.exists()
    assert not (tmp_path / 'outside.txt').exists()


def test_backup_does_not_follow_object_symlinks(tmp_path):
    source = tmp_path / 'source.sqlite'
    minimal_database(source)
    objects = tmp_path / 'objects'
    objects.mkdir()
    external = tmp_path / 'private-outside-file'
    external.write_text('Must not enter backup')
    (objects / 'symlink').symlink_to(external)
    archive = tmp_path / 'backup.zip'
    manifest = backup(source, objects, archive)
    assert [item['path'] for item in manifest['files']] == ['workspace.sqlite']
    with zipfile.ZipFile(archive) as saved:
        assert all(b'Must not enter backup' not in saved.read(name) for name in saved.namelist())
