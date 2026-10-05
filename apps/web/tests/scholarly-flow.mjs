/** Additive real-Chromium coverage for scholarly discovery and retained failed captures. */
import { chromium } from 'playwright';
import assert from 'node:assert/strict';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
const base = process.env.WEB_TEST_URL || 'http://127.0.0.1:3012';
const password = process.env.TEST_ACCOUNT_PASSWORD;
if (!password) throw new Error('TEST_ACCOUNT_PASSWORD must refer to an isolated test account');
const browser = await chromium.launch({
  headless: true,
  ...(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {}),
  args: ['--no-sandbox'],
});
const context = await browser.newContext({
  viewport: { width: 1440, height: 1060 },
  acceptDownloads: true,
});
const page = await context.newPage();
page.setDefaultTimeout(15000);
const errors = [];
const checks = [];
page.on('pageerror', (error) => errors.push(error.message));
await mkdir('test-results', { recursive: true });
await context.tracing.start({ screenshots: true, snapshots: true, sources: true });
async function check(name, action) {
  await action();
  checks.push(name);
  console.log('PASS', name);
}
async function request(path, body, status = 200) {
  const response =
    body === undefined
      ? await context.request.get(base + '/api' + path)
      : await context.request.post(base + '/api' + path, { data: body });
  const data = await response.json();
  assert.equal(response.status(), status, JSON.stringify(data));
  return data;
}
async function login(username) {
  await page.goto(base);
  await page.getByLabel('账户', { exact: true }).fill(username);
  await page.getByLabel('密码', { exact: true }).fill(password);
  await page.getByRole('button', { name: '登录工作空间', exact: true }).click();
  await page.locator('.workspace').waitFor();
}
async function tab(name) {
  await page
    .getByRole('navigation', { name: '项目视图' })
    .getByRole('button', { name: new RegExp('^' + name) })
    .click();
}
async function close() {
  await page.getByRole('button', { name: '关闭对话框', exact: true }).click();
  await page.locator('dialog[open]').waitFor({ state: 'detached' });
}
async function operation(id) {
  for (let i = 0; i < 100; i++) {
    const result = await request('/operations/' + id);
    if (['succeeded', 'failed'].includes(result.status)) return result;
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  throw new Error('Fixture operation did not finish');
}
const projectName = 'Scholarly UI ' + Date.now();
const raw = '<!doctype html><html><head><title>Empty fixture</title></head><body></body></html>';
try {
  await login(process.env.TEST_ADMIN_USERNAME || 'ui-admin');
  const project = await request('/projects', { name: projectName }, 201);
  async function intake(items) {
    return (await request('/discovered-works/batch', { project_id: project.id, items }, 201)).items;
  }
  const [metadata] = await intake([
    {
      title: 'UI metadata lead',
      authors: ['Provider Author'],
      year: 2024,
      doi: '10.1234/ui-metadata',
    },
  ]);
  const sourceUrl = 'https://example.org/scholarly-ui-source';
  const [fulltext] = await intake([
    {
      title: 'UI fulltext lead',
      authors: ['Fulltext Author'],
      doi: '10.1234/ui-fulltext',
      source_url: sourceUrl,
    },
  ]);
  await intake([
    {
      title: 'UI abstract lead',
      authors: ['Abstract Author'],
      doi: '10.1234/ui-abstract',
      abstract:
        '<script>window.UNSAFE_SCHOLARLY_EXECUTED=true</script><img src=x onerror="alert(1)">',
      access_status: 'open_access',
    },
  ]);
  await intake(
    Array.from({ length: 102 }, (_, i) => ({
      title: `UI paged lead ${String(i).padStart(3, '0')}`,
      authors: ['Paged Author'],
      doi: `10.1234/ui-page-${i}`,
    })),
  );
  const ingested = await request(
    '/ingestions',
    {
      project_id: project.id,
      type: 'text',
      title: 'UI saved fulltext',
      text: 'Saved full text fixture with methods, findings and limitations. This controlled fixture does not certify that a human read every paragraph.',
      original_url: sourceUrl,
      content_scope: 'fulltext',
    },
    202,
  );
  const completed = await operation(ingested.id);
  assert.equal(completed.status, 'succeeded', completed.error);
  await request(
    `/discovered-works/${fulltext.id}/readings`,
    {
      document_id: completed.result_json.document_id,
      content_scope: 'fulltext',
      fulltext_reviewed: true,
      review_note: 'Inspected the saved fixture as full text, without claiming a completed read.',
    },
    201,
  );
  const upload = await context.request.post(base + '/api/ingestions/upload', {
    multipart: {
      project_id: project.id,
      file: { name: 'empty.html', mimeType: 'text/html', buffer: Buffer.from(raw) },
    },
  });
  assert.equal(upload.status(), 202);
  const failed = await operation((await upload.json()).id);
  assert.equal(failed.status, 'failed');
  assert.ok(failed.result_json.capture_id);
  await page.reload();
  await page
    .getByRole('navigation', { name: '项目导航' })
    .getByRole('button', { name: projectName, exact: true })
    .click();

  await check(
    'Library separates parsed documents and 105 leads with bounded reversible paging',
    async () => {
      await tab('资料库');
      await page.getByRole('button', { name: '可阅读资料 · 1', exact: true }).waitFor();
      await page.getByRole('button', { name: '学术线索', exact: true }).click();
      await page.getByText('共 105 条学术线索 · 每页 50 条', { exact: true }).waitFor();
      assert.equal(await page.locator('.lead-card').count(), 50);
      const first = await page.locator('.lead-title').first().textContent();
      await page.getByRole('button', { name: '下一页', exact: true }).click();
      await page.getByText('51–100 / 105', { exact: true }).waitFor();
      assert.equal(await page.locator('.lead-card').count(), 50);
      await page.getByRole('button', { name: '下一页', exact: true }).click();
      await page.getByText('101–105 / 105', { exact: true }).waitFor();
      assert.equal(await page.locator('.lead-card').count(), 5);
      assert.equal(
        await page.getByRole('button', { name: '下一页', exact: true }).isDisabled(),
        true,
      );
      await page.getByRole('button', { name: '上一页', exact: true }).click();
      await page.getByText('51–100 / 105', { exact: true }).waitFor();
      await page.getByRole('button', { name: '上一页', exact: true }).click();
      await page.getByText('1–50 / 105', { exact: true }).waitFor();
      assert.equal(await page.locator('.lead-title').first().textContent(), first);
      await page.screenshot({ path: 'test-results/scholarly-desktop.png', fullPage: true });
    },
  );
  await check(
    'Metadata, abstract and available fulltext labels never assert completed reading',
    async () => {
      for (const [title, scope] of [
        ['UI metadata lead', '仅元数据'],
        ['UI abstract lead', '仅摘要'],
        ['UI fulltext lead', '全文已留存可用'],
      ]) {
        const card = page
          .locator('.lead-card')
          .filter({ has: page.getByRole('button', { name: title, exact: true }) });
        assert.ok((await card.textContent()).includes(scope));
        assert.ok((await card.textContent()).includes('未确认通读'));
      }
      await page.getByRole('button', { name: 'UI abstract lead', exact: true }).click();
      await page.locator('summary').filter({ hasText: '供应方摘要（非全文）' }).click();
      await page.locator('.asset-history .safe-text').waitFor();
      assert.equal(await page.evaluate(() => window.UNSAFE_SCHOLARLY_EXECUTED), undefined);
      assert.equal(await page.locator('.asset-detail script, .asset-detail img').count(), 0);
      await page.keyboard.press('Escape');
      await page.locator('dialog[open]').waitFor({ state: 'detached' });
      await page.getByRole('button', { name: 'UI fulltext lead', exact: true }).click();
      await page.getByRole('button', { name: '打开留存资料', exact: true }).click();
      await page.locator('.reader-block').first().waitFor();
      assert.equal(await page.locator('dialog[open]').count(), 1);
      await close();
    },
  );
  await check(
    'Editor correction updates catalogue and preserves original metadata plus review history',
    async () => {
      await page.getByRole('button', { name: 'UI metadata lead', exact: true }).click();
      await page
        .locator('summary')
        .filter({ hasText: /^更正展示元数据$/ })
        .click();
      await page.getByLabel('标题', { exact: true }).fill('UI reviewed lead');
      await page
        .getByRole('dialog')
        .getByLabel(/^作者（每行一位）/)
        .fill('Reviewed Author');
      await page
        .getByLabel('更正依据链接', { exact: true })
        .fill('https://example.org/metadata-proof');
      await page
        .getByLabel('更正理由', { exact: true })
        .fill('Verified against the publisher record.');
      await page.getByRole('button', { name: '保存元数据更正', exact: true }).click();
      await page
        .getByText('元数据更正已保存，原始观察与历史记录已保留。', { exact: true })
        .waitFor();
      await page
        .locator('summary')
        .filter({ hasText: /^供应方原始展示信息$/ })
        .click();
      await page.locator('.asset-detail').getByText('Provider Author', { exact: true }).waitFor();
      await page
        .locator('summary')
        .filter({ hasText: /^元数据更正历史/ })
        .click();
      assert.ok(
        (await page.locator('.asset-detail').textContent()).includes(
          'Verified against the publisher record.',
        ),
      );
      await close();
      await page.getByRole('button', { name: 'UI reviewed lead', exact: true }).waitFor();
    },
  );
  await check(
    'A concurrent correction produces a real CAS conflict and requires reload before resubmission',
    async () => {
      await page.getByRole('button', { name: 'UI reviewed lead', exact: true }).click();
      await page
        .locator('summary')
        .filter({ hasText: /^更正展示元数据$/ })
        .click();
      await page.getByLabel('标题', { exact: true }).fill('Stale UI edit');
      await page
        .getByLabel('更正依据链接', { exact: true })
        .fill('https://example.org/metadata-proof');
      await page
        .getByLabel('更正理由', { exact: true })
        .fill('This edit must not overwrite the concurrent review.');
      await request(
        `/discovered-works/${metadata.id}/metadata-reviews`,
        {
          expected_revision: 1,
          changes: { title: 'Concurrent reviewed lead' },
          reason: 'Another editor verified an updated title.',
          source_url: 'https://example.org/concurrent',
        },
        201,
      );
      await page.getByRole('button', { name: '保存元数据更正', exact: true }).click();
      await page
        .getByText('元数据已被其他编辑更新。请重新加载最新版本，再核对并提交更正。', {
          exact: true,
        })
        .waitFor();
      assert.equal(
        await page.getByRole('button', { name: '保存元数据更正', exact: true }).isDisabled(),
        true,
      );
      await page.getByRole('button', { name: '重新加载最新版本', exact: true }).click();
      await page
        .locator('.asset-detail')
        .getByText('Concurrent reviewed lead', { exact: true })
        .waitFor();
      await close();
      await page.getByRole('button', { name: '刷新线索', exact: true }).click();
      await page.getByRole('button', { name: 'Concurrent reviewed lead', exact: true }).waitFor();
    },
  );
  await check(
    'Closing a pending detail request prevents a late response from reopening the inspector',
    async () => {
      let release;
      let intercepted;
      const gate = new Promise((resolve) => {
        release = resolve;
      });
      const caught = new Promise((resolve) => {
        intercepted = resolve;
      });
      const detailPath = `**/api/discovered-works/${metadata.id}`;
      await page.route(
        detailPath,
        async (route) => {
          intercepted();
          await gate;
          await route.continue();
        },
        { times: 1 },
      );
      await page.getByRole('button', { name: 'Concurrent reviewed lead', exact: true }).click();
      await caught;
      await page.getByRole('status').filter({ hasText: '读取线索与来源记录' }).waitFor();
      const response = page.waitForResponse((result) =>
        result.url().endsWith(`/api/discovered-works/${metadata.id}`),
      );
      await close();
      release();
      await response;
      assert.equal(await page.locator('dialog[open]').count(), 0);
      await page.getByRole('button', { name: 'Concurrent reviewed lead', exact: true }).waitFor();
    },
  );
  await check('List errors are actionable and retry restores real results', async () => {
    const pattern = '**/api/discovered-works?*';
    await page.route(
      pattern,
      (route) =>
        route.fulfill({
          status: 503,
          contentType: 'application/json',
          body: JSON.stringify({ detail: 'Controlled scholarly list interruption' }),
        }),
      { times: 1 },
    );
    await page.getByRole('button', { name: '刷新线索', exact: true }).click();
    await page
      .getByRole('alert')
      .getByText('Controlled scholarly list interruption', { exact: true })
      .waitFor();
    await page.getByRole('alert').getByRole('button', { name: '重试', exact: true }).click();
    await page.getByRole('button', { name: 'Concurrent reviewed lead', exact: true }).waitFor();
  });
  await check(
    'Failed operation exposes capture inspection and byte-identical raw download without a document',
    async () => {
      await tab('运行记录');
      await page
        .locator('.activity-item')
        .filter({ has: page.locator('.status-failed') })
        .locator('.activity-summary')
        .click();
      await page.getByRole('button', { name: '查看留存原件', exact: true }).click();
      await page
        .getByText('原件已归档，尚无可用解析结果；不能作为已就绪的研究文档或证据。', {
          exact: true,
        })
        .waitFor();
      const download = page.waitForEvent('download');
      await page
        .locator('dialog[open]')
        .getByRole('button', { name: '下载留存原件', exact: true })
        .click();
      const artifact = await download;
      assert.equal(await artifact.failure(), null);
      assert.equal((await readFile(await artifact.path())).toString(), raw);
      await page.screenshot({ path: 'test-results/failed-capture-desktop.png', fullPage: true });
      await close();
    },
  );
  await check(
    'Mobile catalogue and detail have no horizontal overflow, and Close restores the list',
    async () => {
      await page.setViewportSize({ width: 390, height: 844 });
      await tab('资料库');
      await page.getByRole('button', { name: '学术线索', exact: true }).click();
      await page.getByRole('button', { name: 'Concurrent reviewed lead', exact: true }).waitFor();
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
      await page.getByRole('button', { name: 'Concurrent reviewed lead', exact: true }).click();
      await page.locator('.asset-detail').waitFor();
      assert.ok(
        await page
          .locator('dialog[open]')
          .evaluate((dialog) => dialog.scrollWidth <= dialog.clientWidth + 1),
      );
      await page.screenshot({ path: 'test-results/scholarly-mobile.png', fullPage: true });
      await close();
      await page.getByRole('button', { name: 'Concurrent reviewed lead', exact: true }).waitFor();
      await page.setViewportSize({ width: 1440, height: 1060 });
    },
  );
  await check(
    'Back and Forward navigation do not reopen a dismissed scholarly inspector',
    async () => {
      await page.goto(base + '/?scholarly-navigation=1');
      await page.locator('.workspace').waitFor();
      await page
        .getByRole('navigation', { name: '项目导航' })
        .getByRole('button', { name: projectName, exact: true })
        .click();
      await tab('资料库');
      await page.getByRole('button', { name: '学术线索', exact: true }).click();
      await page.getByRole('button', { name: 'Concurrent reviewed lead', exact: true }).click();
      await page.locator('.asset-detail').waitFor();
      await close();
      await page.goBack();
      await page.locator('.workspace').waitFor();
      assert.equal(await page.locator('dialog[open]').count(), 0);
      await page.goForward();
      await page.locator('.workspace').waitFor();
      assert.equal(await page.locator('dialog[open]').count(), 0);
    },
  );
  await check(
    'Reader can inspect lead provenance and failed captures but has no correction controls',
    async () => {
      await page.getByRole('button', { name: '退出账户', exact: true }).click();
      await page.getByRole('button', { name: '登录工作空间', exact: true }).waitFor();
      await login(process.env.TEST_READER_USERNAME || 'ui-reader');
      await page
        .getByRole('navigation', { name: '项目导航' })
        .getByRole('button', { name: projectName, exact: true })
        .click();
      await tab('资料库');
      await page.getByRole('button', { name: '学术线索', exact: true }).click();
      await page.getByRole('button', { name: 'Concurrent reviewed lead', exact: true }).click();
      await page.getByText('仅编辑与管理员可更正展示元数据。', { exact: true }).waitFor();
      assert.equal(
        await page.getByRole('button', { name: '保存元数据更正', exact: true }).count(),
        0,
      );
      await close();
      await tab('运行记录');
      await page
        .locator('.activity-item')
        .filter({ has: page.locator('.status-failed') })
        .locator('.activity-summary')
        .click();
      assert.equal(
        await page.getByRole('button', { name: '重试这项操作', exact: true }).count(),
        0,
      );
      await page.getByRole('button', { name: '查看留存原件', exact: true }).click();
      await page.locator('.asset-detail').waitFor();
      await close();
    },
  );
  assert.deepEqual(errors, []);
  checks.push('No browser runtime errors');
  await writeFile(
    'test-results/scholarly-results.json',
    JSON.stringify({ passed: checks.length, checks, errors }, null, 2),
  );
  console.log(JSON.stringify({ passed: checks.length, checks }, null, 2));
} catch (error) {
  await page
    .screenshot({ path: 'test-results/scholarly-failure.png', fullPage: true })
    .catch(() => {});
  throw error;
} finally {
  await context.tracing.stop({ path: 'test-results/scholarly-trace.zip' });
  await browser.close();
}
