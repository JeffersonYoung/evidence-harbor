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
