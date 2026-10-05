"""Real pgvector tests, opt-in via EVIDENCEHARBOR_TEST_POSTGRES."""
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from backend import domain
from backend.db import Base
from backend.vector_index import (
    build_generation,
    create_schema,
    reciprocal_rank_fusion,
    search_generation,
    validate_vector,
)


@pytest.fixture
def vector_db(tmp_path, monkeypatch):
    url = os.getenv('EVIDENCEHARBOR_TEST_POSTGRES')
    if not url: pytest.skip('Real PostgreSQL not configured')
    schema = 'vector_' + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as c: c.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={'options': f'-csearch_path={schema},public'})
    monkeypatch.setattr(domain.settings, 'blob_dir', tmp_path/'objects')
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as connection: create_schema(connection)
        yield sessionmaker(bind=engine, expire_on_commit=False)
    finally:
        engine.dispose()
        with admin.begin() as c: c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


class FixtureEmbeddings:
    model = 'fixture-semantic-v1'
    dimensions = 3
    def embed(self, texts):
        return [[1., 0., 0.] if 'table' in t.lower() else [0., 1., 0.] for t in texts]


def test_real_vector_generation_scoping_and_version(vector_db):
    with vector_db() as session:
        p = domain.create_project(session, 'w', 'Tables')
        stored = domain.store_capture(session, 'w', p.id, 'fixture:table', 'text', 'Tables',
            b'PDF table extraction.', 'text/plain', 'PDF table extraction.')
        generation = build_generation(session, 'w', p.id, stored['representation_id'], FixtureEmbeddings())
        session.commit()
        matches = search_generation(session, 'w', p.id, generation.id, 'table', FixtureEmbeddings())
        assert matches and matches[0]['distance'] == pytest.approx(0)
        with pytest.raises(ValueError, match='not found'):
            search_generation(session, 'other', p.id, generation.id, 'table', FixtureEmbeddings())
        other = FixtureEmbeddings(); other.model = 'new-model'
        with pytest.raises(ValueError, match='must match'):
            search_generation(session, 'w', p.id, generation.id, 'table', other)


def test_rrf_and_embedding_validation():
    assert reciprocal_rank_fusion([['a','b'], ['b','c']])[0][0] == 'b'
    with pytest.raises(ValueError): validate_vector([float('nan')], 1)
    with pytest.raises(ValueError): validate_vector([0.0], 1)
    with pytest.raises(ValueError): validate_vector([1.0], 2)
