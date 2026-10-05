import { test } from 'node:test';
import assert from 'node:assert/strict';
import { renderToStaticMarkup } from 'react-dom/server';
import { SafeText, Status, Empty, HighlightedQuote } from '../components/ui';
import { ProposalForm, ResearchForm, IngestForm } from '../components/forms';
import { isPending } from '../lib/api';
const noop = () => {};
test('source script, HTML tags, event handlers and URLs remain escaped text', () => {
  const source =
    '<script>globalThis.PWNED=true</script>\n\n<img src=x onerror="alert(1)">\n\n[jump](javascript:alert(1))';
  const html = renderToStaticMarkup(<SafeText text={source} />);
  assert.ok(!html.includes('<script>'));
  assert.ok(!html.includes('<img'));
  assert.ok(!html.includes('<a '));
  assert.ok(html.includes('&lt;script&gt;'));
  assert.ok(html.includes('onerror=&quot;'));
});
test('safe structural headings do not become arbitrary markup', () => {
  const html = renderToStaticMarkup(
    <SafeText text={'# <script>unsafe</script>\n\nA real paragraph.'} />,
  );
  assert.ok(html.includes('<h3>&lt;script&gt;unsafe&lt;/script&gt;</h3>'));
  assert.ok(html.includes('<p>A real paragraph.</p>'));
});
test('pending status is labeled honestly and completion is distinct', () => {
  assert.match(renderToStaticMarkup(<Status value="pending" />), /排队中/);
  assert.match(renderToStaticMarkup(<Status value="succeeded" />), /已完成/);
  assert.match(renderToStaticMarkup(<Status value="failed" />), /失败/);
  assert.match(renderToStaticMarkup(<Status value="retrying" />), /重试中/);
  assert.equal(isPending('retrying'), true);
});
test('manual proposal refuses to render submission without real evidence', () => {
  const html = renderToStaticMarkup(
    <ProposalForm projectId="test" evidence={[]} onClose={noop} onCreated={noop} />,
  );
  assert.match(html, /先保存一条证据/);
  assert.ok(!html.includes('保存为提案'));
});
test('research defaults to local and includes bounded controls', () => {
  const html = renderToStaticMarkup(
    <ResearchForm projectId="test" onClose={noop} onCreated={noop} />,
  );
  assert.match(html, /value="local" selected/);
  for (const name of [
    'max_searches',
    'max_documents',
    'max_tool_calls',
    'max_duration_seconds',
    'max_output_tokens',
  ])
    assert.ok(html.includes(`name="${name}"`));
  assert.match(html, /不调用外部大模型/);
});
test('ingestion classifies internally by default', () => {
  const html = renderToStaticMarkup(
    <IngestForm projectId="test" onClose={noop} onCreated={noop} />,
  );
  assert.match(html, /name="classification"/);
  assert.match(html, /value="internal" selected/);
  assert.match(html, /不会执行原始网页脚本/);
});
test('empty state contains no invented project records', () => {
  const html = renderToStaticMarkup(<Empty title="还没有资料">添加真实来源后再开始研究。</Empty>);
  assert.match(html, /还没有资料/);
  assert.match(html, /添加真实来源/);
});

test('exact evidence highlight preserves emoji and supplementary CJK before the quote', () => {
  const text = '😀前文 𠀀的研究发现：e\u0301vidence 👩🏽‍💻，后文';
  const quote = '𠀀的研究发现：e\u0301vidence 👩🏽‍💻';
  const html = renderToStaticMarkup(<HighlightedQuote text={text} quote={quote} />);
  assert.equal(html, '<p>😀前文 <mark>' + quote + '</mark>，后文</p>');
});
test('evidence highlighting does not normalize combining sequences into different source text', () => {
  const decomposed = 'A source with e\u0301 and 𠮷 characters.';
  const html = renderToStaticMarkup(<HighlightedQuote text={decomposed} quote={'é'} />);
  assert.ok(!html.includes('<mark>'));
  assert.ok(html.includes('e\u0301'));
  const exact = renderToStaticMarkup(<HighlightedQuote text={decomposed} quote={'e\u0301'} />);
  assert.ok(exact.includes('<mark>e\u0301</mark>'));
});

test('stored code-point offset highlights the second identical quote after emoji, CJK and combining marks', () => {
  const text = '😀 证据 / 𠀀 e\u0301 证据，尾声';
  const html = renderToStaticMarkup(<HighlightedQuote text={text} quote="证据" startOffset={12} />);
  assert.equal(html, '<p>😀 证据 / 𠀀 e\u0301 <mark>证据</mark>，尾声</p>');
});
test('invalid explicit evidence offsets never fall back to a different matching occurrence', () => {
  const text = '😀 证据 / 𠀀 e\u0301 证据，尾声';
  for (const startOffset of [-1, 0, 3, 4, 13, 99, 1.5, Infinity, NaN]) {
    const html = renderToStaticMarkup(
      <HighlightedQuote text={text} quote="证据" startOffset={startOffset} />,
    );
    assert.ok(!html.includes('<mark>'), `Unexpected highlight at offset ${startOffset}`);
    assert.equal(html, '<p>' + text + '</p>');
  }
});
test('quote-only navigation uses the first occurrence only when no stored anchor is supplied', () => {
  const html = renderToStaticMarkup(<HighlightedQuote text="😀 证据 / 证据" quote="证据" />);
  assert.equal(html, '<p>😀 <mark>证据</mark> / 证据</p>');
});
test('an explicit zero offset can highlight a supplementary Unicode quote', () => {
  const html = renderToStaticMarkup(
    <HighlightedQuote text="😀𠀀 原文" quote="😀𠀀" startOffset={0} />,
  );
  assert.equal(html, '<p><mark>😀𠀀</mark> 原文</p>');
});

// Availability and bibliography are deliberately distinct from completed reading.
import { WorkScope, WorkDetails, MetadataReviewForm } from '../components/scholarly-leads';
import { CaptureDetails } from '../components/capture-inspector';
import Library from '../components/library';
import { archivedCaptureId, type DiscoveredWork, type Capture } from '../lib/api';
const lead: DiscoveredWork = {
  id: 'lead-1',
  project_id: 'project-1',
  title: 'Provider title',
  authors: ['Researcher'],
  year: 2025,
  abstract: '',
  access_status: 'open_access',
  content_scope: 'metadata_only',
  evidence_eligible: false,
  fulltext_ready: false,
  fulltext_read: false,
  review_revision: 0,
  review_status: 'unreviewed_provider_metadata',
  observations: [],
  metadata_reviews: [],
  aliases: [],
  readings: [],
};
test('discovery scope labels distinguish metadata, abstract and available fulltext without claiming papers read', () => {
  const metadata = renderToStaticMarkup(<WorkScope work={lead} />);
  assert.match(metadata, /仅元数据/);
  assert.match(metadata, /尚不可作为证据/);
  assert.match(metadata, /未确认通读/);
  const abstract = renderToStaticMarkup(
    <WorkScope work={{ ...lead, content_scope: 'abstract', evidence_eligible: true }} />,
  );
  assert.match(abstract, /仅摘要/);
  assert.match(abstract, /取证范围仅限留存摘要/);
  const fulltext = renderToStaticMarkup(
    <WorkScope
      work={{ ...lead, content_scope: 'fulltext', fulltext_ready: true, evidence_eligible: true }}
    />,
  );
  assert.match(fulltext, /全文已留存可用/);
  assert.match(fulltext, /未确认通读/);
  assert.ok(!fulltext.includes('已读论文'));
});
test('library keeps document count separate from scholarly leads', () => {
  const html = renderToStaticMarkup(
    <Library
      projectId="project-1"
      role="reader"
      documents={[]}
      sources={[]}
      onAdd={noop}
      onRead={noop}
    />,
  );
  assert.match(html, /可阅读资料 · 0/);
  assert.match(html, /学术线索/);
  assert.match(html, /解析完成不代表论文已通读/);
});
test('reviewed title, provider original and provenance history all remain visible as escaped text', () => {
  const hostile = '<img src=x onerror="alert(1)">';
  const work: DiscoveredWork = {
    ...lead,
    title: 'Reviewed title',
    review_revision: 1,
    provider_display: { title: 'Original title', authors: ['Original author'], year: 2024 },
    abstract: hostile,
    source_url: 'javascript:alert(1)',
    observations: [{ id: 'obs-1', payload_hash: 'abc', payload: { title: hostile } }],
    metadata_reviews: [
      {
        id: 'review-1',
        revision: 1,
        overrides_json: { title: 'Reviewed title' },
        reason: 'Checked publisher title',
        source_url: 'https://example.org/source',
        evidence_ids: [],
        actor_json: { username: 'editor', role: 'editor' },
      },
    ],
  };
  const html = renderToStaticMarkup(
    <WorkDetails work={work} role="reader" onRead={noop} onSaved={noop} onReload={noop} />,
  );
  for (const text of [
    'Reviewed title',
    'Original title',
    'Original author',
    'Checked publisher title',
    '原始来源与观察记录',
    '元数据更正历史',
  ])
    assert.ok(html.includes(text));
  assert.ok(!html.includes('<img'));
  assert.ok(!html.includes('href="javascript:'));
  assert.ok(html.includes('&lt;img'));
  assert.ok(!html.includes('保存元数据更正'));
  assert.match(html, /仅编辑与管理员/);
});
test('only editors and admins see metadata correction controls; immutable identities have no form field', () => {
  for (const role of ['editor', 'admin']) {
    const html = renderToStaticMarkup(
      <WorkDetails work={lead} role={role} onRead={noop} onSaved={noop} onReload={noop} />,
    );
    assert.match(html, /保存元数据更正/);
    for (const field of ['title', 'authors', 'year', 'venue', 'source_url', 'reason'])
      assert.ok(html.includes(`name="${field}"`));
    assert.ok(!html.includes('name="doi"'));
    assert.ok(!html.includes('name="arxiv_id"'));
  }
  const researcher = renderToStaticMarkup(
    <WorkDetails work={lead} role="researcher" onRead={noop} onSaved={noop} onReload={noop} />,
  );
  assert.ok(!researcher.includes('保存元数据更正'));
  const form = renderToStaticMarkup(
    <MetadataReviewForm work={{ ...lead, authors: [] }} onSaved={noop} onReload={noop} />,
  );
  assert.ok(!form.match(/<textarea name="authors"[^>]*required/));
});
test('failed capture inspector provides original download without a document and displays escaped error', () => {
  const capture: Capture = {
    id: 'cap-1',
    source_id: 'source-1',
    project_id: 'project-1',
    content_hash: 'a'.repeat(64),
    media_type: 'text/html',
    byte_size: 80,
    fetched_at: '2026-01-01T00:00:00Z',
    metadata_json: { content_scope: 'unspecified' },
    observations: [],
    processing_ready: false,
    representations: [],
    processing_operations: [{ id: 'op-1', status: 'failed', error: '<script>unsafe()</script>' }],
  };
  const html = renderToStaticMarkup(<CaptureDetails capture={capture} />);
  assert.match(html, /下载留存原件/);
  assert.match(html, /尚无可用解析结果/);
  assert.match(html, /失败/);
  assert.ok(!html.includes('<script>'));
  assert.equal(
    archivedCaptureId({ id: 'op-1', status: 'failed', result_json: { capture_id: 'cap-1' } }),
    'cap-1',
  );
  assert.equal(
    archivedCaptureId({ id: 'op-1', status: 'failed', result_json: { capture_id: false } }),
    null,
  );
});

test('reviewed abstract remains metadata with original provider text and visible version caveat', () => {
  const html = renderToStaticMarkup(
    <WorkDetails
      work={{
        ...lead,
        content_scope: 'abstract',
        abstract: 'Reviewed abstract <script>bad()</script>',
        abstract_reviewed: true,
        abstract_review_revision: 1,
        abstract_display_scope: 'abstract_metadata',
        abstract_evidence_eligible: false,
        abstract_sha256: 'fixture-hash',
        review_revision: 2,
        provider_display: { ...lead, abstract: 'Original degraded provider abstract' },
        abstract_review: {
          id: 'review-1',
          revision: 1,
          reason: 'Linked preprint; journal-version equivalence unverified.',
          source_url: 'https://example.org/preprint',
          evidence_ids: [],
        },
      }}
      role="reader"
      onRead={noop}
      onSaved={noop}
      onReload={noop}
    />,
  );
  assert.match(html, /Original degraded provider abstract/);
  assert.match(html, /journal-version equivalence unverified/);
  assert.match(html, /元数据，非全文证据/);
  assert.match(html, /尚不可作为证据/);
  assert.ok(!html.includes('<script>'));
  assert.ok(!html.includes('name="abstract"'));
});

test('editor abstract field has a stable label and explicit no-clearing/no-fulltext semantics', () => {
  const html = renderToStaticMarkup(
    <MetadataReviewForm
      work={{ ...lead, abstract: 'Provider abstract' }}
      onSaved={noop}
      onReload={noop}
    />,
  );
  assert.match(html, /name="abstract"/);
  assert.match(html, /aria-label="摘要（元数据）"/);
  assert.match(html, /不认证全文阅读/);
});
