"""Full S3-backed API flow; only the S3 protocol boundary uses an in-memory client."""
import io
import zipfile
from typing import ClassVar

import pytest

from backend import models as m
from backend import processing, storage
from tests.helpers import digest
from tests.test_api_workflow import checked, project


class ConditionalConflict(Exception):
    response: ClassVar[dict] = {'ResponseMetadata': {'HTTPStatusCode': 412}}


class MemoryS3:
    def __init__(self):
        self.objects = {}
        self.puts = []
        self.gets = []

    def put_object(self, **request):
        self.puts.append(request)
        assert request['IfNoneMatch'] == '*'
        key = (request['Bucket'], request['Key'])
        if key in self.objects:
            raise ConditionalConflict()
        self.objects[key] = request['Body']
        return {'ResponseMetadata': {'HTTPStatusCode': 200}}

    def get_object(self, **request):
        self.gets.append(request)
        return {'Body': io.BytesIO(self.objects[(request['Bucket'], request['Key'])])}


@pytest.fixture
def s3(monkeypatch):
    client = MemoryS3()
    store = storage.S3ContentStore('fixture-research-bucket', client=client, prefix='workspace-objects')
    monkeypatch.setenv('STORAGE_BACKEND', 's3')
    monkeypatch.setattr(storage, 'get_store', lambda: store)
    monkeypatch.setattr(processing, 'get_store', lambda: store)
    return client, store


def test_s3_upload_pipeline_evidence_reprocess_raw_download_and_export(client, runtime, s3):
    remote, store = s3
    proj = project(client)
    raw = b'<html><script>BAD_SCRIPT</script><article><p>S3 archive preserves real source evidence.</p></article></html>'
    request = checked(client.post('/v1/ingestions/upload', data={'project_id': proj['id']},
        files={'file': ('saved.html', raw, 'text/html')}), 202)
    completed = checked(client.get(f'/v1/operations/{request["id"]}'))
    assert completed['status'] == 'succeeded', completed['error']
    result = completed['result_json']
    document = checked(client.get(f'/v1/documents/{result["document_id"]}/content'))
    assert 'BAD_SCRIPT' not in document['content']
    assert 'S3 archive preserves real source evidence.' in document['content']
    evidence = checked(client.post('/v1/evidence', json={
        'project_id': proj['id'], 'block_id': result['block_ids'][0],
        'quote': 'S3 archive preserves real source evidence.'}), 201)
    anchor = checked(client.get(f'/v1/evidence/{evidence["id"]}'))
    with runtime.session_factory() as session:
        capture = session.get(m.Capture, result['capture_id'])
        assert capture.blob_path == f's3://{store.bucket}/{store._key(digest(raw))}'
        assert capture.metadata_json['storage_backend'] == 's3'
    raw_response = client.get(f'/v1/captures/{result["capture_id"]}/raw')
    assert raw_response.status_code == 200 and raw_response.content == raw
    assert raw_response.headers['content-type'] == 'application/octet-stream'
    assert raw_response.headers['x-content-type-options'] == 'nosniff'
    assert 'sandbox' in raw_response.headers['content-security-policy']
    reprocess = checked(client.post(f'/v1/captures/{result["capture_id"]}/reprocess'), 202)
    processed = checked(client.get(f'/v1/operations/{reprocess["id"]}'))
    assert processed['status'] == 'succeeded', processed['error']
    assert processed['result_json']['representation_id'] != result['representation_id']
    assert checked(client.get(f'/v1/evidence/{evidence["id"]}')) == anchor
    archive_response = client.get(f'/v1/projects/{proj["id"]}/export')
    assert archive_response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(archive_response.content)) as archive:
        assert archive.read(f'raw/{digest(raw)}') == raw
    assert len(remote.objects) == 1
    assert len(remote.puts) >= 3
    assert all(request['IfNoneMatch'] == '*' for request in remote.puts)
    assert all(request['Bucket'] == store.bucket for request in remote.gets)


@pytest.mark.parametrize('path_variant', ['foreign_bucket', 'foreign_prefix', 'wrong_hash_path'])
def test_s3_upload_pointer_cannot_select_unconfigured_bucket_or_prefix(runtime, s3, path_variant):
    remote, store = s3
    saved = store.put(b'Saved source evidence')
    before = len(remote.gets)
    path = f's3://{store.bucket}/{saved.key}'
    if path_variant == 'foreign_bucket':
        path = path.replace(store.bucket, 'unapproved-bucket')
    elif path_variant == 'foreign_prefix':
        path = path.replace('workspace-objects/', 'other-prefix/')
    else:
        path = path.replace('/raw/', '/raw/../')
    with pytest.raises(processing.PipelineError, match='configured bucket'):
        processing._read_upload(path, 1024, runtime.settings)
    assert len(remote.gets) == before


def test_s3_conditional_conflict_never_overwrites_corrupt_existing_object(s3):
    remote, store = s3
    raw = b'Original evidence'
    saved = store.put(raw)
    key = (store.bucket, saved.key)
    remote.objects[key] = b'Corrupt object'
    with pytest.raises(storage.StorageError, match='digest mismatch'):
        store.put(raw)
    assert remote.objects[key] == b'Corrupt object'
