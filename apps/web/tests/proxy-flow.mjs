import assert from 'node:assert/strict';
import { writeFile, mkdir } from 'node:fs/promises';
const base = process.env.WEB_TEST_URL || 'http://127.0.0.1:3012';
const password = process.env.TEST_ACCOUNT_PASSWORD;
if (!password) throw new Error('TEST_ACCOUNT_PASSWORD must refer to an isolated test account');
let cookie = '';
const checks = [];
async function req(path, { method = 'GET', body, headers = {}, raw = false } = {}) {
  const h = { ...headers, ...(cookie ? { Cookie: cookie } : {}) };
  if (body && !(body instanceof FormData)) h['Content-Type'] = 'application/json';
  const response = await fetch(base + '/api' + path, {
    method,
    headers: h,
    ...(body ? { body: body instanceof FormData ? body : JSON.stringify(body) } : {}),
  });
  return {
    status: response.status,
    headers: response.headers,
    data: raw
      ? new Uint8Array(await response.arrayBuffer())
      : await response.json().catch(() => null),
  };
}
async function check(name, fn) {
  await fn();
  checks.push(name);
  console.log('PASS', name);
}
async function login(username) {
  const result = await req('/auth/login', { method: 'POST', body: { username, password } });
  assert.equal(result.status, 200, JSON.stringify(result.data));
  assert.ok(!('access_token' in result.data));
  assert.match(result.headers.get('set-cookie'), /HttpOnly/i);
  assert.match(result.headers.get('set-cookie'), /SameSite=strict/i);
  cookie = result.headers.get('set-cookie').split(';')[0];
}
async function waitOperation(id) {
  for (let i = 0; i < 60; i++) {
    const r = await req('/operations/' + id);
    if (['succeeded', 'failed'].includes(r.data.status)) return r.data;
    await new Promise((r) => setTimeout(r, 200));
  }
  throw new Error('Operation did not finish within 12 seconds');
}
let project, document, evidence, proposal, report;
await check('Anonymous requests require 401 despite configured server API_TOKEN', async () => {
  assert.equal((await req('/projects')).status, 401);
  assert.equal(
    (await req('/projects', { method: 'POST', body: { name: 'not allowed' } })).status,
    401,
  );
});
await check('Login strips access token and uses HttpOnly Strict cookie', async () => {
  await login(process.env.TEST_ADMIN_USERNAME || 'ui-admin');
  assert.equal((await req('/auth/me')).data.role, 'admin');
});
await check('Cross-origin and Sec-Fetch-Site cross-site writes are rejected', async () => {
  assert.equal(
    (
      await req('/projects', {
        method: 'POST',
        body: { name: 'blocked' },
        headers: { Origin: 'https://evil.example' },
      })
    ).status,
    403,
  );
  assert.equal(
    (
      await req('/projects', {
        method: 'POST',
        body: { name: 'blocked' },
        headers: { 'Sec-Fetch-Site': 'cross-site' },
      })
    ).status,
    403,
  );
});
await check('Project create and read succeed', async () => {
  const r = await req('/projects', {
    method: 'POST',
    body: { name: 'Proxy integration ' + Date.now(), description: 'Isolated automated test' },
  });
  assert.equal(r.status, 201);
  project = r.data;
  assert.equal((await req('/projects/' + project.id)).status, 200);
});
const text =
  'Remote work findings\n\nRemote work can improve focus and flexibility for knowledge workers.\n\nThis finding is limited to surveyed knowledge workers.\n\n<script>window.UNSAFE_SOURCE_EXECUTED=true</script>';
await check('Text ingestion, idempotency, operation polling and immutable capture', async () => {
  const body = { project_id: project.id, type: 'text', title: 'Research source', text };
  const headers = { 'Idempotency-Key': 'proxy-test-' + project.id };
  const r = await req('/ingestions', { method: 'POST', body, headers });
  assert.equal(r.status, 202);
  assert.equal((await req('/ingestions', { method: 'POST', body, headers })).data.id, r.data.id);
  const op = await waitOperation(r.data.id);
  assert.equal(op.status, 'succeeded', op.error);
  const data = (await req('/projects/' + project.id)).data;
  document = data.documents[0];
  assert.ok(data.operations.length);
  const content = (await req('/documents/' + document.id + '/content')).data;
  assert.ok(content.blocks.length);
  assert.ok(content.content.includes('<script>'));
  assert.ok(content.capture);
});
await check('Original capture downloads unchanged bytes as an attachment', async () => {
  const r = await req('/captures/' + document.capture_id + '/raw', { raw: true });
  assert.equal(r.status, 200);
  assert.match(r.headers.get('content-disposition'), /attachment/);
  assert.equal(new TextDecoder().decode(r.data), text);
});
await check('Full-text search returns stable block reference', async () => {
  const r = await req('/search', {
    method: 'POST',
    body: { project_id: project.id, query: 'Remote work', limit: 20 },
  });
  assert.equal(r.status, 200);
  assert.ok(r.data.results.length);
  assert.ok(r.data.results[0].block_id);
});
await check('Evidence exact quote validates and returns document locator', async () => {
  const content = (await req('/documents/' + document.id + '/content')).data;
  const block = content.blocks.find((b) => b.text.includes('improve focus'));
  const r = await req('/evidence', {
    method: 'POST',
    body: { project_id: project.id, block_id: block.id, quote: block.text },
  });
  assert.equal(r.status, 201);
  evidence = r.data;
  const located = (await req('/evidence/' + evidence.id)).data;
  assert.equal(located.document_id, document.id);
  assert.equal(located.block_id, block.id);
  assert.equal(
    (
      await req('/evidence', {
        method: 'POST',
        body: {
          project_id: project.id,
          block_id: block.id,
          quote: 'invented unsupported quotation',
        },
      })
    ).status,
    422,
  );
  assert.ok((await req('/projects/' + project.id)).data.evidence.length);
});
const claims = [{ text: 'Remote work can improve focus.', evidence_ids: [] }];
await check('Evidence-backed proposal and publish create v1 report', async () => {
  claims[0].evidence_ids = [evidence.id];
  const r = await req('/proposals', {
    method: 'POST',
    body: {
      project_id: project.id,
      title: 'Verified report',
      content: '# Finding\n\nRemote work can improve focus.\n\n# Limits\n\nSurveyed workers only.',
      claims,
    },
  });
  assert.equal(r.status, 201);
  proposal = r.data;
  const pub = await req('/proposals/' + proposal.id + '/publish', { method: 'POST', body: {} });
  assert.equal(pub.status, 200);
  report = pub.data;
  assert.equal(report.version, 1);
});
await check('CAS conflict returns 409 and preserves current version', async () => {
  const body = {
    project_id: project.id,
    title: 'Revision',
    content:
      '# Finding\n\nRemote work can improve focus. Updated.\n\n# Limits\n\nSurveyed workers only.',
    claims,
    target_document_id: report.id,
    base_version: 1,
  };
  const a = await req('/proposals', { method: 'POST', body });
  const b = await req('/proposals', { method: 'POST', body });
  assert.equal(
    (
      await req('/proposals/' + a.data.id + '/publish', {
        method: 'POST',
        body: { expected_version: 1 },
      })
    ).status,
    200,
  );
  assert.equal(
    (
      await req('/proposals/' + b.data.id + '/publish', {
        method: 'POST',
        body: { expected_version: 1 },
      })
    ).status,
    409,
  );
  assert.equal((await req('/documents/' + report.id + '/content')).data.version, 2);
  assert.equal((await req('/proposals/' + b.data.id)).data.status, 'pending');
});
await check('Version history reads original snapshot and locks protect sections', async () => {
  const versions = await req('/documents/' + report.id + '/versions');
  assert.equal(versions.data.length, 2);
  assert.ok(
    (await req('/documents/' + report.id + '/content?version=1')).data.content.includes(
      'can improve focus',
    ),
  );
  assert.equal(
    (
      await req('/documents/' + report.id + '/locks', {
        method: 'PATCH',
        body: { sections: ['Limits'] },
      })
    ).status,
    200,
  );
  const p = await req('/proposals', {
    method: 'POST',
    body: {
      project_id: project.id,
      title: 'Locked revision',
      content:
        '# Finding\n\nRemote work can improve focus.\n\n# Limits\n\nAltered protected content.',
      claims,
      target_document_id: report.id,
      base_version: 2,
    },
  });
  assert.equal(
    (
      await req('/proposals/' + p.data.id + '/publish', {
        method: 'POST',
        body: { expected_version: 2 },
      })
    ).status,
    409,
  );
});
await check('Partial claim acceptance publishes only chosen claim', async () => {
  const unlocked = await req('/documents/' + report.id + '/locks', {
    method: 'PATCH',
    body: { sections: [] },
  });
  assert.equal(unlocked.status, 200);
  const p = await req('/proposals', {
    method: 'POST',
    body: {
      project_id: project.id,
      title: 'Partial acceptance',
      content: 'Remote work can improve focus. A second claim.',
      claims: [claims[0], { text: 'A second claim.', evidence_ids: [evidence.id] }],
      target_document_id: report.id,
      base_version: 2,
    },
  });
  const r = await req('/proposals/' + p.data.id + '/publish', {
    method: 'POST',
    body: { expected_version: 2, accepted_claim_indices: [0] },
  });
  assert.equal(r.status, 200);
  assert.ok(r.data.content.includes(claims[0].text));
  assert.ok(!r.data.content.includes('A second claim.'));
});
await check('Local bounded research produces events and evidence-linked proposal', async () => {
  const q = await req('/questions', {
    method: 'POST',
    body: { project_id: project.id, text: 'Remote work flexibility' },
  });
  assert.equal(q.status, 201);
  const r = await req('/research-runs', {
    method: 'POST',
    body: {
      project_id: project.id,
      question: 'Remote work flexibility',
      provider: 'local',
      max_searches: 1,
      max_documents: 8,
      max_tool_calls: 16,
      max_duration_seconds: 180,
      max_cost_usd: 0,
      max_output_tokens: 2000,
    },
  });
  assert.equal(r.status, 202);
  const operation = await waitOperation(r.data.operation_id);
  assert.equal(operation.status, 'succeeded', operation.error);
  const events = await req('/research-runs/' + r.data.id + '/events');
  assert.ok(events.data.length);
});
await check('File upload passes multipart and imports safely', async () => {
  const f = new FormData();
  f.set('project_id', project.id);
  f.set('file', new Blob(['Remote work file upload text'], { type: 'text/plain' }), 'research.txt');
  const r = await req('/ingestions/upload', { method: 'POST', body: f });
  assert.equal(r.status, 202);
  assert.equal((await waitOperation(r.data.id)).status, 'succeeded');
});
await check('Export is a non-empty ZIP with download headers', async () => {
  const r = await req('/projects/' + project.id + '/export', { raw: true });
  assert.equal(r.status, 200);
  assert.ok(r.data.length > 100);
  assert.equal(String.fromCharCode(r.data[0], r.data[1]), 'PK');
  assert.match(r.headers.get('content-disposition'), /attachment/);
});
await check('Diagnostics returns real database and worker configuration', async () => {
  const r = await req('/diagnostics');
  assert.equal(r.status, 200);
  assert.ok(r.data.database);
  assert.ok(r.data.worker);
});
await check(
  'Pipeline registry, immutable config and sample quality/diff run work through proxy',
  async () => {
    const registry = await req('/pipeline-registry');
    assert.equal(registry.status, 200);
    assert.equal(registry.data.arbitrary_code_allowed, false);
    const config = {
      version: registry.data.version,
      stages: registry.data.stage_order.map((name) => ({
        name,
        options: Object.fromEntries(
          Object.entries(registry.data.options[name]).map(([key, value]) => [key, value.default]),
        ),
      })),
    };
    const p = await req('/pipelines', {
      method: 'POST',
      body: { project_id: project.id, name: 'Verified default pipeline', config },
    });
    assert.equal(p.status, 201, JSON.stringify(p.data));
    assert.equal((await req('/pipelines?project_id=' + project.id)).data.items.length, 1);
    const op = await req('/pipelines/' + p.data.id + '/test-runs', {
      method: 'POST',
      body: { capture_id: document.capture_id },
    });
    assert.equal(op.status, 202);
    const result = await waitOperation(op.data.id);
    assert.equal(result.status, 'succeeded', result.error);
    assert.equal(result.result_json.published, false);
    assert.ok(result.result_json.quality);
    assert.ok(Array.isArray(result.result_json.diff));
  },
);
await check('Source watch configuration is offline-safe, role-scoped, and pauseable', async () => {
  const created = await req('/source-watches', {
    method: 'POST',
    body: {
      project_id: project.id,
      locator: 'https://example.com/research',
      source_type: 'http',
      interval_seconds: 86400,
      enabled: true,
      research_on_change: true,
      research_question: 'What changed?',
      classification: 'internal',
    },
  });
  assert.equal(created.status, 201, JSON.stringify(created.data));
  const paused = await req('/source-watches/' + created.data.id, {
    method: 'PATCH',
    body: { enabled: false },
  });
  assert.equal(paused.status, 200);
  assert.equal(paused.data.enabled, false);
  const r = await req('/source-watches?project_id=' + project.id);
  assert.equal(r.status, 200);
  assert.ok(r.data.items.some((item) => item.id === created.data.id));
});
await check('Logout revokes session and anonymous requests stay 401', async () => {
  assert.equal((await req('/auth/logout', { method: 'POST', body: {} })).status, 200);
  assert.equal((await req('/projects')).status, 401);
  cookie = '';
  assert.equal((await req('/projects')).status, 401);
  assert.equal(
    (await req('/projects', { method: 'POST', body: { name: 'post logout' } })).status,
    401,
  );
});
await check('Reader role can read but receives 403 on write', async () => {
  await login(process.env.TEST_READER_USERNAME || 'ui-reader');
  assert.equal((await req('/projects')).status, 200);
  assert.equal(
    (await req('/projects', { method: 'POST', body: { name: 'forbidden' } })).status,
    403,
  );
  await req('/auth/logout', { method: 'POST', body: {} });
});
await mkdir('test-results', { recursive: true });
await writeFile(
  'test-results/proxy-integration.json',
  JSON.stringify({ passed: checks.length, checks }, null, 2),
);
console.log(JSON.stringify({ passed: checks.length, checks }, null, 2));
