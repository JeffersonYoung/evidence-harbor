import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { registerTools } from '../openclaw/dist/register.js';
import { callMcp } from '../openclaw/dist/bridge.js';

test('all tools delegate unchanged to the same MCP server and are opt-in', async () => {
  const registered = [];
  const calls = [];
  registerTools({ registerTool: (tool, options) => registered.push({ tool, options }) },
    async (...args) => { calls.push(args); return { content: [{ type: 'text', text: 'ok' }], structuredContent: { ok: true } }; });
  assert.equal(registered.length, JSON.parse(readFileSync(new URL('../openclaw/tool-schemas.json', import.meta.url))).length);
  assert.equal(registered.length, 15);
  assert.ok(!registered.some(({tool}) => tool.name.includes('review_discovered_work_metadata')));
  for (const { tool, options } of registered) {
    assert.equal(options.optional, true);
    const params = { project_id: 'test' };
    const result = await tool.execute('test-call', params);
    assert.equal(calls.at(-1)[0], tool.name.replace('evidenceharbor_', ''));
    assert.equal(calls.at(-1)[1], params);
    assert.deepEqual(result.details, { ok: true });
  }
});

test('backend errors are not passed off as successful tool calls', async () => {
  const registered = [];
  registerTools({ registerTool: (tool) => registered.push(tool) }, async () => ({ isError: true }));
  await assert.rejects(registered[0].execute('test', {}), /rejected/);
});

test('missing interpreter fails before a subprocess is launched', async () => {
  const saved = process.env.EVIDENCEHARBOR_PYTHON;
  delete process.env.EVIDENCEHARBOR_PYTHON;
  try { await assert.rejects(callMcp('search_library', {}), /EVIDENCEHARBOR_PYTHON/); }
  finally { if (saved !== undefined) process.env.EVIDENCEHARBOR_PYTHON = saved; }
});


test('editor correction registration needs explicit opt-in and still delegates authorization to API', () => {
  const saved = process.env.EVIDENCEHARBOR_ENABLE_EDITOR_TOOLS;
  process.env.EVIDENCEHARBOR_ENABLE_EDITOR_TOOLS = 'true';
  try {
    const registered = [];
    registerTools({ registerTool: (tool, options) => registered.push({tool, options}) });
    assert.equal(registered.length, 16);
    assert.ok(registered.find(({tool}) => tool.name === 'evidenceharbor_review_discovered_work_metadata').options.optional);
    assert.ok(!registered.some(({tool}) => /publish|schedule|shell/.test(tool.name)));
  } finally {
    if (saved === undefined) delete process.env.EVIDENCEHARBOR_ENABLE_EDITOR_TOOLS;
    else process.env.EVIDENCEHARBOR_ENABLE_EDITOR_TOOLS = saved;
  }
});
