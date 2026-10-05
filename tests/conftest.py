"""API tests use a real, isolated file-backed database and local immutable objects."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from backend import config, db, domain


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    # The adapter may also be invoked outside the app factory, so isolate its
    # shared settings explicitly as well as passing the app's settings override.
    settings = config.Settings(
        database_url=f'sqlite:///{tmp_path / "workspace.db"}',
        blob_dir=tmp_path / 'blobs',
        demo_mode=False,
        api_tokens={'owner-a': 'workspace-a', 'owner-b': 'workspace-b'},
        inline_worker=True,
    )
    engine = db.make_engine(settings.database_url)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(config, 'settings', settings)
    monkeypatch.setattr(domain, 'settings', settings)
    monkeypatch.setattr(db, 'SessionLocal', factory)
    monkeypatch.setattr(db, 'engine', engine)
    monkeypatch.setenv('STORAGE_BACKEND', 'local')
    monkeypatch.setenv('RAW_STORAGE_DIR', str(settings.blob_dir))
    monkeypatch.delenv('DOCLING_ARTIFACTS_PATH', raising=False)
    db.init_db(engine)
    yield SimpleNamespace(settings=settings, engine=engine, session_factory=factory)
    engine.dispose()


@pytest.fixture
def client(runtime):
    from backend.api import create_app
    application = create_app(settings_override=runtime.settings, session_factory=runtime.session_factory)
    with TestClient(application, headers={'Authorization': 'Bearer owner-a'}) as test_client:
        yield test_client
