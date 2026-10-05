"""Feed XML is bounded discovery data; only queued normal ingestion can create evidence."""
import pytest
from sqlalchemy import func, select

from backend import connectors
from backend import models as m
from backend.scheduling import SourceWatch
from backend.security import FetchResult
from tests.test_domain_integrity import make_project

FEED_URL = 'https://feeds.example.org/rss'


def returned_xml(data, url=FEED_URL):
    return FetchResult(data, url, 'application/xml', 200, {})


def rss(*items):
    return ('<rss><channel>' + ''.join(items) + '</channel></rss>').encode()


def item(url='/paper', title='Research lead', guid='stable-item', description='Unverified snippet'):
    return f'<item><guid>{guid}</guid><link>{url}</link><title>{title}</title><description>{description}</description></item>'


def test_rss_cursor_deduplicates_same_item_but_detects_item_revision(monkeypatch):
    content = [rss(item())]
    monkeypatch.setattr(connectors, 'safe_fetch', lambda url, **kw: returned_xml(content[0], url))
    first = connectors.discover_feed(FEED_URL)
    assert len(first.candidates) == 1
    assert first.candidates[0].url == 'https://feeds.example.org/paper'
    assert first.candidates[0].title == 'Research lead'
    assert len(first.candidates[0].cursor_id) == 64
    repeated = connectors.discover_feed(FEED_URL, state=first.next_state)
    assert repeated.candidates == []
    content[0] = rss(item(description='Updated discovery snippet'))
    revised = connectors.discover_feed(FEED_URL, state=first.next_state)
    assert len(revised.candidates) == 1
    assert revised.candidates[0].cursor_id != first.candidates[0].cursor_id
    assert revised.candidates[0].item_id == first.candidates[0].item_id


def test_discovery_rejects_offsite_private_credentials_and_https_downgrade(monkeypatch):
    content = rss(item('/valid', guid='good'), item('https://other.example.org/offsite', guid='offsite'),
                  item('http://127.0.0.1/private', guid='private'), item('http://feeds.example.org/plain', guid='plain'),
                  item('https://user:password@feeds.example.org/credentials', guid='credentials'))
    monkeypatch.setattr(connectors, 'safe_fetch', lambda url, **kw: returned_xml(content, url))
    result = connectors.discover_feed(FEED_URL)
    assert [candidate.url for candidate in result.candidates] == ['https://feeds.example.org/valid']
    assert len(result.warnings) == 4


def test_atom_relative_links_resolve_and_remain_allowlisted(monkeypatch):
    content = b'''<feed xmlns="http://www.w3.org/2005/Atom" xml:base="/articles/">
    <entry><id>item-1</id><title>Saved lead</title><updated>2026-01-01</updated>
    <link rel="alternate" href="report"/><summary>Unverified text</summary></entry></feed>'''
    monkeypatch.setattr(connectors, 'safe_fetch', lambda url, **kw: returned_xml(content, url))
    result = connectors.discover_feed(FEED_URL)
    assert result.candidates[0].url == 'https://feeds.example.org/articles/report'
    assert result.candidates[0].updated == '2026-01-01'


@pytest.mark.parametrize('content', [
    b'<!DOCTYPE rss [<!ENTITY secret SYSTEM "file:///etc/passwd">]><rss><channel>&secret;</channel></rss>',
    b'<!ENTITY attack "expanded"><rss/>',
    '<rss/>'.encode('utf-16'),
    ('<rss>' + '<item>' * 40 + '</item>' * 40 + '</rss>').encode(),
    b'<rss><broken></rss>',
])
def test_hostile_or_invalid_xml_is_rejected(content, monkeypatch):
    monkeypatch.setattr(connectors, 'safe_fetch', lambda url, **kw: returned_xml(content, url))
    with pytest.raises(connectors.ConnectorError):
        connectors.discover_feed(FEED_URL)


def test_nested_sitemap_obeys_depth_fetch_and_source_item_bounds(monkeypatch):
    requested = []

    def fetch(url, **kwargs):
        requested.append(url)
        number = int(url.rsplit('/', 1)[-1])
        data = f'<sitemapindex><sitemap><loc>https://feeds.example.org/{number + 1}</loc></sitemap></sitemapindex>'.encode()
        return returned_xml(data, url)

    monkeypatch.setattr(connectors, 'safe_fetch', fetch)
    result = connectors.discover_feed('https://feeds.example.org/0', source_type='sitemap')
    assert len(requested) == result.fetch_count == 3
    assert result.truncated is True and result.warnings
    assert result.candidates == []
    xml = ('<urlset>' + ''.join(f'<url><loc>https://feeds.example.org/paper{n}</loc></url>' for n in range(4)) + '</urlset>').encode()
    monkeypatch.setattr(connectors, 'safe_fetch', lambda url, **kw: returned_xml(xml, url))
    limited = connectors.discover_feed(FEED_URL, source_type='sitemap', max_items=2)
    assert len(limited.candidates) == 2 and limited.truncated is True


def test_feed_cursor_and_enqueued_operations_commit_atomically(runtime, monkeypatch):
    project_id = make_project(runtime)
    monkeypatch.setattr(connectors, 'safe_fetch', lambda url, **kw: returned_xml(rss(item()), url))
    with runtime.session_factory() as session:
        watch = SourceWatch(workspace_id='workspace-a', project_id=project_id, locator=FEED_URL,
                            source_type='rss', classification='internal')
        session.add(watch)
        session.commit()
        watch_id = watch.id
        queued = connectors.enqueue_connector_watch(session, watch, 'rolled-back-tick')
        assert len(queued['operation_ids']) == 1
        session.rollback()
    with runtime.session_factory() as session:
        watch = session.get(SourceWatch, watch_id)
        assert watch.connector_state == {}
        assert session.scalar(select(func.count()).select_from(m.Operation)) == 0
        queued = connectors.enqueue_connector_watch(session, watch, 'committed-tick')
        session.commit()
        operation = session.get(m.Operation, queued['operation_ids'][0])
        assert operation.status == 'pending' and operation.kind == 'ingest'
        assert operation.input_json['type'] == 'url'
        assert operation.input_json['url'] == 'https://feeds.example.org/paper'
        assert operation.input_json['discovery_provenance']['connector_type'] == 'rss'
        assert 'snippet' not in operation.input_json and 'text' not in operation.input_json
        assert session.scalar(select(func.count()).select_from(m.Capture)) == 0
        assert session.scalar(select(func.count()).select_from(m.Evidence)) == 0
        retry = connectors.enqueue_connector_watch(session, watch, 'next-tick')
        session.commit()
        assert retry['operation_ids'] == []
        assert session.scalar(select(func.count()).select_from(m.Operation)) == 1
        assert session.scalar(select(func.count()).select_from(m.OperationDispatch)) == 1
