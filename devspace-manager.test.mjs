import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import net from 'node:net';
import { spawn } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';

test('background lifecycle and multi-root server restart preserve the tunnel', { timeout: 60000 }, async () => {
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'devspace-manager-test-'));
  const configDir = path.join(temporary, 'config');
  const projectA = path.join(temporary, '项目 A');
  const projectB = path.join(temporary, '项目 B');
  for (const dir of [configDir, projectA, projectB]) fs.mkdirSync(dir);
  const listener = net.createServer();
  await new Promise(resolve => listener.listen(0, '127.0.0.1', resolve));
  const port = listener.address().port;
  await new Promise(resolve => listener.close(resolve));
  const configFile = path.join(configDir, 'config.jsonc');
  fs.writeFileSync(configFile, JSON.stringify({ server: { port, publicBaseUrl: 'https://previous.example' },
    workspaces: { allowedRoots: [] }, subagents: { enabled: false } }));
  fs.writeFileSync(path.join(configDir, 'auth.json'), '{"ownerToken":"fixture-secret"}');
  const cli = path.join(temporary, 'cli.mjs');
  fs.writeFileSync(cli, `
    import fs from 'node:fs'; import path from 'node:path'; import http from 'node:http';
    const config=JSON.parse(fs.readFileSync(path.join(process.env.DEVSPACE_CONFIG_DIR,'config.jsonc')));
    if(process.argv[2]==='config') console.log(JSON.stringify(config));
    else if(process.argv[2]==='serve') {
      fs.appendFileSync(process.env.TEST_SERVER_PIDS, process.pid+'\\n');
      http.createServer((req,res)=>{res.writeHead(401);res.end('fixture')}).listen(config.server.port,'127.0.0.1');
    } else console.log('forwarded:'+process.argv.slice(2).join(' '));
  `);
  const preload = path.join(temporary, 'tunnel.mjs');
  fs.writeFileSync(preload, `
    import fs from 'node:fs'; import path from 'node:path';
    if(path.basename(process.argv[1] ?? '') === 'tunnel') {
      fs.appendFileSync(process.env.TEST_TUNNEL_PIDS, process.pid+'\\n');
      console.error('https://fixture-tunnel.trycloudflare.com');
      await new Promise(()=>setInterval(()=>{},1000));
    }
  `);
  const env = { ...process.env, DEVSPACE_UPDATE_CHECK: '0', DEVSPACE_CONFIG_DIR: configDir, DEVSPACE_CLOUDFLARED: process.execPath,
    NODE_OPTIONS: `--import=${pathToFileURL(preload).href}`, TEST_SERVER_PIDS: path.join(temporary, 'servers'),
    TEST_TUNNEL_PIDS: path.join(temporary, 'tunnels') };
  const manager = fileURLToPath(new URL('./devspace-manager.mjs', import.meta.url));
  async function run(command, cwd = projectA) {
    return new Promise((resolve, reject) => {
      const child = spawn(process.execPath, [manager, '--cli', cli, command], { cwd, env, windowsHide: true });
      let out = '', err = '';
      child.stdout.on('data', data => out += data);
      child.stderr.on('data', data => err += data);
      child.on('error', reject);
      child.on('exit', code => code === 0 ? resolve(out) : reject(new Error(`${command}: ${out}\n${err}`)));
    });
  }
  const pids = file => fs.readFileSync(file, 'utf8').trim().split('\n');
  try {
    assert.match(await run('status'), /stopped/);
    assert.match(await run('serve'), /Owner password：fixture-secret/);
    const statusOutput = await run('status');
    assert.match(statusOutput, /running/);
    assert.ok(!statusOutput.includes('fixture-secret'));
    for (const file of fs.readdirSync(path.join(configDir, 'launcher')).filter(name => name.endsWith('.log'))) {
      assert.ok(!fs.readFileSync(path.join(configDir, 'launcher', file), 'utf8').includes('fixture-secret'));
    }
    assert.equal(pids(env.TEST_SERVER_PIDS).length, 1);
    assert.match(await run('serve'), /Owner password：fixture-secret/);
    assert.equal(pids(env.TEST_SERVER_PIDS).length, 1, 'same directory must not restart');
    await Promise.all([run('serve'), run('serve')]);
    assert.equal(pids(env.TEST_SERVER_PIDS).length, 1, 'concurrent duplicate commands are serialized');
    await run('serve', projectB);
    assert.equal(pids(env.TEST_SERVER_PIDS).length, 2, 'new directory restarts server');
    assert.equal(pids(env.TEST_TUNNEL_PIDS).length, 1, 'tunnel stays alive');
    const updated = JSON.parse(fs.readFileSync(configFile));
    assert.deepEqual(updated.workspaces.allowedRoots, [fs.realpathSync.native(projectA), fs.realpathSync.native(projectB)]);
    assert.equal(updated.server.port, port);
    assert.equal(updated.server.publicBaseUrl, 'https://fixture-tunnel.trycloudflare.com');
    assert.deepEqual(updated.subagents, { enabled: false });
    assert.equal(JSON.parse(fs.readFileSync(path.join(configDir, 'auth.json'))).ownerToken, 'fixture-secret');
    assert.match(await run('doctor'), /forwarded:doctor/);
    await run('stop');
    assert.match(await run('status'), /stopped/);
    for (const pid of [...pids(env.TEST_SERVER_PIDS), ...pids(env.TEST_TUNNEL_PIDS)]) {
      assert.throws(() => process.kill(Number(pid), 0), /ESRCH/);
    }
    await run('stop');
    await run('serve', projectB);
    assert.equal(pids(env.TEST_TUNNEL_PIDS).length, 2);
    assert.equal(JSON.parse(fs.readFileSync(configFile)).workspaces.allowedRoots.length, 2);
    await run('stop');
    env.DEVSPACE_CLOUDFLARED = path.join(temporary, 'does-not-exist.exe');
    await assert.rejects(run('serve'));
    assert.match(await run('status'), /stopped/);
    fs.unlinkSync(path.join(configDir, 'auth.json'));
    await assert.rejects(run('serve'), error => {
      assert.ok(error.message.includes(path.join(configDir, 'auth.json')));
      assert.ok(error.message.includes('DEVSPACE_CONFIG_DIR'));
      return true;
    });
    const savedEnvironment = { ...env };
    try {
      env.DEVSPACE_CONFIG_DIR = '';
      env.USERPROFILE = temporary;
      env.HOME = temporary;
      const emptyConfigStatus = await run('status');
      assert.ok(emptyConfigStatus.includes(path.join(temporary, '.devspace', 'launcher')),
        'an empty config override must use the home config, not the current project');
    } finally { Object.assign(env, savedEnvironment); }
  } finally {
    await run('stop');
    fs.rmSync(temporary, { recursive: true, force: true });
  }
});
