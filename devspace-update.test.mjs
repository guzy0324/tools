import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { newer, checkUpdate } from './devspace-update.mjs';

test('version ordering includes numeric components and prereleases', () => {
  assert.equal(newer('1.10.0', '1.9.9'), true);
  assert.equal(newer('1.1.1', '1.1.1'), false);
  assert.equal(newer('1.1.0', '1.2.0'), false);
  assert.equal(newer('1.1.0', '1.1.0-beta.9'), true);
  assert.equal(newer('1.1.0-beta.10', '1.1.0-beta.9'), true);
  assert.equal(newer('1.1.0-beta.1', '1.1.0'), false);
});

test('update policy, npm failures, and server resume without touching the tunnel', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'devspace-update-test-'));
  const packageRoot = path.join(root, 'node_modules', '@waishnav', 'devspace');
  fs.mkdirSync(path.join(packageRoot, 'bin'), { recursive: true });
  fs.writeFileSync(path.join(packageRoot, 'package.json'), '{"version":"1.1.1"}');
  const events = [];
  const messages = [];
  const base = { cli: path.join(packageRoot, 'bin', 'devspace.js'), environment: {}, log: text => messages.push(text),
    beforeInstall: async () => events.push('pause'), afterInstall: async () => events.push('resume'),
    confirm: async () => false,
    runNpm: async args => { events.push(args[0]); return '"1.2.0"'; } };
  try {
    assert.equal(await checkUpdate(base), false);
    assert.deepEqual(events, ['view']);
    events.length = 0;
    assert.equal(await checkUpdate({ ...base, environment: { DEVSPACE_UPDATE: 'y' } }), true);
    assert.deepEqual(events, ['view', 'pause', 'install', 'resume']);
    events.length = 0;
    await checkUpdate({ ...base, environment: { DEVSPACE_UPDATE: 'y' }, runNpm: async args => {
      events.push(args[0]); if (args[0] === 'install') throw new Error('fixture install failure'); return '"1.2.0"';
    } });
    assert.deepEqual(events, ['view', 'pause', 'install', 'resume']);
    assert.ok(messages.some(text => text.includes('fixture install failure')));
    events.length = 0;
    await checkUpdate({ ...base, runNpm: async () => { throw new Error('offline'); } });
    assert.deepEqual(events, []);
    assert.ok(messages.some(text => text.includes('版本检查失败，继续启动')));
    await checkUpdate({ ...base, environment: { DEVSPACE_UPDATE_CHECK: '0' } });
    assert.deepEqual(events, []);
    await checkUpdate({ ...base, confirm: async () => true });
    assert.deepEqual(events, ['view', 'pause', 'install', 'resume']);
    events.length = 0;
    await checkUpdate({ ...base, runNpm: async args => { events.push(args[0]); return '"1.1.1"'; } });
    assert.deepEqual(events, ['view']);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});
