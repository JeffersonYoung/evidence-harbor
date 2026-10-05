import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile, readdir } from 'node:fs/promises';
test('proxy credentials remain server-only and require deliberate single-user mode', async () => {
  const proxy = await readFile('app/api/[...path]/route.ts', 'utf8');
  assert.match(proxy, /SINGLE_USER_MODE === 'true'/);
  assert.match(proxy, /httpOnly:\s*true/);
  assert.match(proxy, /sameSite:\s*'strict'/);
  assert.match(proxy, /Cross-origin write rejected/);
  assert.match(proxy, /Cross-site write rejected/);
  assert.match(proxy, /Authentication required/);
});
test('all reader content is React escaped text with no raw HTML injection', async () => {
  for (const file of await readdir('components')) {
    const content = await readFile('components/' + file, 'utf8');
    assert.ok(!content.includes('dangerouslySetInnerHTML'), file);
    assert.ok(!content.includes('NEXT_PUBLIC_API_TOKEN'), file);
    assert.ok(!content.includes('process.env.API_TOKEN'), file);
  }
});
test('development and normal start bind loopback by default', async () => {
  const pkg = JSON.parse(await readFile('package.json', 'utf8'));
  assert.match(pkg.scripts.dev, /127\.0\.0\.1/);
  assert.match(pkg.scripts.start, /127\.0\.0\.1/);
});
