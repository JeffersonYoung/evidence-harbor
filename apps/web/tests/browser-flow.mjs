/** Real end-to-end verification. Start isolated API and web servers first; see README.md. */
import { chromium } from 'playwright';
import assert from 'node:assert/strict';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
const base = process.env.WEB_TEST_URL || 'http://127.0.0.1:3012';
const username = process.env.TEST_ADMIN_USERNAME || 'ui-admin';
const password = process.env.TEST_ACCOUNT_PASSWORD;
if (!password) throw new Error('TEST_ACCOUNT_PASSWORD is required (use an isolated test account)');
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
page.setDefaultNavigationTimeout(30000);
await context.tracing.start({ screenshots: true, snapshots: true, sources: true });
const errors = [];
const checks = [];
page.on('pageerror', (e) => errors.push(e.message));
await mkdir('test-results', { recursive: true });
async function check(name, fn) {
  await fn();
  checks.push(name);
  console.log('PASS', name);
}
async function login(user) {
  await page.goto(base);
  await page.getByLabel('账户', { exact: true }).fill(user);
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
async function request(path, body, method = 'POST') {
  const r = await context.request.fetch(base + '/api' + path, { method, data: body });
  return { response: r, data: await r.json() };
}
try {
  await check('Anonymous GET and POST require login even with API_TOKEN configured', async () => {
    for (const method of ['GET', 'POST']) {
      const r = await context.request.fetch(base + '/api/projects', {
        method,
        ...(method === 'POST' ? { data: { name: 'forbidden' } } : {}),
      });
      assert.equal(r.status(), 401);
    }
  });
  await check(
    'Login stores HttpOnly SameSite=Strict session; browser JSON contains no token',
    async () => {
      await login(username);
      const cookies = await context.cookies();
      const session = cookies.find((c) => c.name === 'evidenceharbor_session');
      assert.ok(session?.httpOnly);
      assert.equal(session.sameSite, 'Strict');
      assert.ok(!(await page.evaluate(() => document.cookie)).includes('evidenceharbor_session'));
    },
  );
  await check('Cross-origin mutation rejected', async () => {
    const r = await context.request.post(base + '/api/projects', {
      headers: { Origin: 'https://untrusted.example' },
      data: { name: 'blocked' },
    });
    assert.equal(r.status(), 403);
  });
  const projectName = '研究工作流验证 ' + Date.now();
  let projectId;
  await check('Create a project through the UI', async () => {
    await page.getByRole('button', { name: '新建研究项目', exact: true }).click();
    await page.getByLabel('项目名称', { exact: true }).fill(projectName);
    await page.getByLabel('研究目标（选填）').fill('验证资料、证据、提案与版本之间的完整闭环。');
    await page.getByRole('button', { name: '创建项目', exact: true }).click();
    await page.getByRole('heading', { name: projectName }).waitFor();
    const all = await (await context.request.get(base + '/api/projects')).json();
    projectId = all.find((p) => p.name === projectName).id;
  });
  await page.screenshot({ path: 'test-results/project-desktop.png', fullPage: true });
  const source =
    'Remote work research\n\nRemote work can improve focus and flexibility for knowledge workers. Team communication practices matter more than location.\n\nThis conclusion is limited to surveyed knowledge workers, and does not establish a causal effect.\n\n<script>window.UNSAFE_SOURCE_EXECUTED=true</script> This is source text, never executable code.';
  await check('Text ingestion shows operation progress and appears in library', async () => {
    await page.locator('.tab-row').getByRole('button', { name: '添加资料', exact: true }).click();
    await page.getByRole('tab', { name: '粘贴文本' }).click();
    await page.getByLabel('资料标题（选填）').fill('混合办公研究资料');
    await page.getByLabel('资料正文', { exact: true }).fill(source);
    await page.getByRole('button', { name: '开始导入', exact: true }).click();
    await page.locator('.activity-item .status-succeeded').first().waitFor({ timeout: 30000 });
    await tab('资料库');
    await page.getByRole('button', { name: /混合办公研究资料 已保存/ }).waitFor();
  });
  let evidenceId, reportId;
  await check('Search, source reading, safe text, and quote capture', async () => {
    await page.getByLabel('搜索项目资料').fill('Remote work');
    await page.locator('.search-result').first().waitFor();
    await page.locator('.search-result').first().click();
    await page.locator('.reader-block').first().waitFor();
    assert.equal(await page.evaluate(() => window.UNSAFE_SOURCE_EXECUTED), undefined);
    await page.getByRole('button', { name: '引用第 2 段', exact: true }).click();
    await page.getByRole('button', { name: '保存证据', exact: true }).click();
    await page.getByRole('button', { name: '证据已保存', exact: true }).waitFor();
    await page.getByRole('button', { name: '关闭对话框', exact: true }).click();
    const project = await (await context.request.get(base + '/api/projects/' + projectId)).json();
    assert.ok(project.evidence.length);
    evidenceId = project.evidence[0].id;
  });
  await page.getByLabel('清空搜索').click();
  await page.screenshot({ path: 'test-results/library-desktop.png', fullPage: true });
  await check('Manual proposal uses real saved evidence, review publishes v1', async () => {
    await tab('报告');
    await page.getByRole('button', { name: '起草报告', exact: true }).click();
    await page.getByLabel('报告标题', { exact: true }).fill('混合办公：证据综述');
    await page
      .getByLabel('正文', { exact: true })
      .fill(
        '# 核心结论\n\nRemote work can improve focus.\n\n# 限制\n\nEvidence is limited to knowledge workers.',
      );
    await page.getByLabel('核心结论', { exact: true }).fill('Remote work can improve focus.');
    await page.locator('.evidence-choices input').first().check();
    await page.getByRole('button', { name: '保存为提案', exact: true }).click();
    await page.getByRole('button', { name: '审核并发布', exact: true }).click();
    await page.getByRole('button', { name: '版本历史', exact: true }).waitFor();
    const project = await (await context.request.get(base + '/api/projects/' + projectId)).json();
    reportId = project.documents.find((d) => d.kind === 'report').id;
    assert.equal(project.documents.find((d) => d.id === reportId).version, 1);
  });
  await page.screenshot({ path: 'test-results/report-desktop.png', fullPage: true });
  await check('Quote evidence navigation highlights exact source block', async () => {
    const claim = { text: 'Remote work improves focus.', evidence_ids: [evidenceId] };
    const out = await request('/proposals', {
      project_id: projectId,
      title: '证据定位提案',
      content: '# 发现\n\nRemote work improves focus.',
      claims: [claim],
    });
    assert.equal(out.response.status(), 201);
    await page.reload();
    await page.getByRole('button', { name: /证据定位提案/ }).click();
    await page.getByRole('button', { name: '证据 1', exact: true }).click();
    await page.locator('.reader-block.highlighted mark').waitFor();
    await page.getByRole('button', { name: '关闭对话框', exact: true }).click();
  });
  await check('CAS conflict preserves stale proposal instead of overwriting report', async () => {
    const body = {
      project_id: projectId,
      content:
        '# 核心结论\n\nUpdated conclusion.\n\n# 限制\n\nEvidence is limited to knowledge workers.',
      claims: [{ text: 'Updated conclusion.', evidence_ids: [evidenceId] }],
      target_document_id: reportId,
      base_version: 1,
    };
    const a = await request('/proposals', { ...body, title: '当前版本更新' });
    const b = await request('/proposals', { ...body, title: '并发旧版本提案' });
    const pub = await request('/proposals/' + a.data.id + '/publish', { expected_version: 1 });
    assert.equal(pub.response.status(), 200);
    await page.reload();
    await page.getByRole('button', { name: /并发旧版本提案/ }).click();
    await page.getByRole('button', { name: '审核并发布', exact: true }).click();
    await page.getByText('报告已有新版本，发布已被阻止', { exact: true }).waitFor();
    const project = await (await context.request.get(base + '/api/projects/' + projectId)).json();
    assert.equal(project.documents.find((d) => d.id === reportId).version, 2);
    assert.equal(project.proposals.find((p) => p.id === b.data.id).status, 'pending');
  });
  await page.screenshot({ path: 'test-results/conflict-desktop.png', fullPage: true });
  await check('Versioned reader switches snapshots and locks latest sections', async () => {
    await page.getByRole('button', { name: '查看当前版本', exact: true }).click();
    await page.getByLabel('文档版本', { exact: true }).selectOption('1');
    await page.getByText('Remote work can improve focus.', { exact: true }).waitFor();
    await page.getByLabel('文档版本', { exact: true }).selectOption('2');
    await page.locator('.section-locks summary').click();
    await page.locator('.section-locks').getByLabel('限制', { exact: true }).check();
    await page.waitForTimeout(300);
    const r = await context.request.get(base + '/api/documents/' + reportId + '/content');
    assert.deepEqual((await r.json()).locked_sections, ['限制']);
    await page.getByRole('button', { name: '关闭对话框', exact: true }).click();
  });
  await check('Question and bounded local research produce a traceable run', async () => {
    await tab('问题');
    await page.getByLabel('新研究问题', { exact: true }).fill('Remote work flexibility');
    await page.getByRole('button', { name: '添加问题', exact: true }).click();
    await page.getByRole('button', { name: '研究这个问题' }).click();
    await page.getByRole('button', { name: '开始研究', exact: true }).click();
    await page.locator('.activity-summary .status-succeeded').first().waitFor({ timeout: 30000 });
    await page.locator('.activity-summary').first().click();
    await page.locator('.run-detail').waitFor();
  });
  await check('File upload is ingested through multipart proxy', async () => {
    await page.locator('.tab-row').getByRole('button', { name: '添加资料', exact: true }).click();
    await page.getByRole('tab', { name: '上传文件' }).click();
    await page.locator('input[type=file]').setInputFiles({
      name: 'remote-work.txt',
      mimeType: 'text/plain',
      buffer: Buffer.from(
        'Remote work and asynchronous documentation require clear communication.',
      ),
    });
    await page.getByRole('button', { name: '开始导入', exact: true }).click();
    await page.waitForTimeout(2800);
    await tab('资料库');
    await page
      .getByRole('button', { name: /remote-work.txt/ })
      .first()
      .waitFor();
  });
  await check('Project export downloads a non-empty ZIP', async () => {
    const download = page.waitForEvent('download');
    await page.getByRole('button', { name: '导出项目', exact: true }).click();
    const file = await download;
    assert.ok(file.suggestedFilename().endsWith('.zip'));
    assert.equal(await file.failure(), null);
    const bytes = await readFile(await file.path());
    assert.ok(bytes.length > 100);
    assert.equal(bytes.subarray(0, 2).toString(), 'PK');
  });
  await check('Diagnostics display real service state', async () => {
    await page.getByRole('button', { name: '系统与流水线' }).click();
    await page.getByText('服务已响应', { exact: true }).waitFor();
    assert.ok((await page.locator('.diagnostics-json').textContent()).includes('database'));
    await page.getByRole('button', { name: '完成', exact: true }).click();
  });
  await check('Mobile layout has no horizontal overflow and navigation works', async () => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.getByRole('button', { name: '打开导航', exact: true }).click();
    await page.locator('.sidebar.mobile-open').waitFor();
    await page.getByRole('button', { name: '关闭导航', exact: true }).click();
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    await page.screenshot({ path: 'test-results/library-mobile.png', fullPage: true });
  });
  await check('Logout revokes session and anonymous access remains 401', async () => {
    await page.setViewportSize({ width: 1440, height: 1060 });
    await page.getByRole('button', { name: '退出账户', exact: true }).click();
    await page.getByRole('button', { name: '登录工作空间', exact: true }).waitFor();
    assert.equal((await context.request.get(base + '/api/projects')).status(), 401);
    assert.equal(
      (
        await context.request.post(base + '/api/projects', { data: { name: 'after logout' } })
      ).status(),
      401,
    );
  });
  await check('Reader role sees actionable denial on write and can still read', async () => {
    await login(process.env.TEST_READER_USERNAME || 'ui-reader');
    await page.getByRole('button', { name: '新建研究项目', exact: true }).click();
    await page.getByLabel('项目名称', { exact: true }).fill('Forbidden reader write');
    await page.getByRole('button', { name: '创建项目', exact: true }).click();
    await page
      .getByText('当前账户没有此操作权限，请联系管理员调整角色。', { exact: true })
      .waitFor();
    await page.getByRole('button', { name: '取消', exact: true }).click();
    assert.equal((await context.request.get(base + '/api/projects')).status(), 200);
  });
  assert.deepEqual(errors, []);
  checks.push('No browser runtime errors');
  await writeFile(
    'test-results/results.json',
    JSON.stringify({ passed: checks.length, checks, errors }, null, 2),
  );
  console.log(JSON.stringify({ passed: checks.length, checks }, null, 2));
} catch (error) {
  await page.screenshot({ path: 'test-results/failure.png', fullPage: true }).catch(() => {});
  await writeFile(
    'test-results/browser-failure.json',
    JSON.stringify({ completed: checks.length, checks, errors, failure: String(error) }, null, 2),
  );
  throw error;
} finally {
  await context.tracing.stop({ path: 'test-results/browser-trace.zip' });
  await browser.close();
}
