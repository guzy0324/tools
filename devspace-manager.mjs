import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import http from 'node:http';
import net from 'node:net';
import { randomBytes } from 'node:crypto';
import { spawn, execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { checkUpdate } from './devspace-update.mjs';

const args = process.argv.slice(2);
const cli = args[0] === '--cli' ? args.splice(0, 2)[1] : undefined;
const command = args.shift() ?? 'serve';
const configuredDir = process.env.DEVSPACE_CONFIG_DIR || path.join(os.homedir(), '.devspace');
const configDir = path.resolve(configuredDir === '~' ? os.homedir()
  : configuredDir.startsWith('~/') || configuredDir.startsWith('~\\')
    ? path.join(os.homedir(), configuredDir.slice(2)) : configuredDir);
const runDir = path.join(configDir, 'launcher');
const stateFile = path.join(runDir, 'state.json');
const configFile = path.join(configDir, 'config.jsonc');
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const env = { ...process.env, DEVSPACE_CONFIG_DIR: configDir };
// Reuse auth.json consistently, as the original launcher did.
delete env.DEVSPACE_OAUTH_OWNER_TOKEN;

function read(file) { try { return JSON.parse(fs.readFileSync(file, 'utf8')); } catch { return null; } }
function write(file, data) {
  const temporary = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(temporary, JSON.stringify(data, null, 2) + '\n', { mode: 0o600 });
  fs.renameSync(temporary, file);
}
function loadConfig() {
  return JSON.parse(execFileSync(process.execPath, [cli, 'config'], { env, encoding: 'utf8', windowsHide: true }));
}
function requireAuth() {
  const file = path.join(configDir, 'auth.json');
  if (!fs.existsSync(file)) {
    throw new Error(`找不到认证文件：${file}。当前配置目录：${configDir}。请核对 DEVSPACE_CONFIG_DIR；仅首次配置需要运行 devspace init。`);
  }
}
function saveConfig(config) {
  if (fs.existsSync(configFile) && !fs.existsSync(`${configFile}.before-launcher.bak`)) {
    fs.copyFileSync(configFile, `${configFile}.before-launcher.bak`);
  }
  write(configFile, config);
}
function canonical(root) {
  return fs.realpathSync.native(root);
}
function samePath(a, b) {
  const normalize = value => {
    const resolved = path.resolve(value.startsWith('~/') ? path.join(os.homedir(), value.slice(2)) : value);
    let real = resolved;
    try { real = canonical(resolved); } catch { /* Keep configured offline roots. */ }
    return process.platform === 'win32' ? real.toLowerCase() : real;
  };
  return normalize(a) === normalize(b);
}

async function request(state, action, body = {}, timeout = 90000) {
  return new Promise((resolve, reject) => {
    const req = http.request({ hostname: '127.0.0.1', port: state.controlPort, path: `/${action}`, method: 'POST',
      headers: { authorization: `Bearer ${state.token}`, 'content-type': 'application/json' } }, response => {
      let text = '';
      response.on('data', chunk => { text += chunk; });
      response.on('end', () => {
        try {
          const result = JSON.parse(text);
          if (response.statusCode !== 200) reject(new Error(result.error ?? '管理请求失败'));
          else resolve(result);
        } catch (error) { reject(error); }
      });
    });
    req.setTimeout(timeout, () => req.destroy(new Error('管理请求超时，请查看 launcher 日志。')));
    req.on('error', reject);
    req.end(JSON.stringify(body));
  });
}
async function current() {
  const state = read(stateFile);
  if (!state?.controlPort) return null;
  try { return { state, status: await request(state, 'status', {}, 2000) }; } catch { return null; }
}
function display(status) {
  console.log(`状态：${status.phase}`);
  if (status.url) console.log(`MCP 地址：${status.url}/mcp`);
  if (status.port) console.log(`Server 端口：${status.port}`);
  if (status.serverPid) console.log(`Server PID：${status.serverPid}`);
  if (status.tunnelPid) console.log(`隧道 PID：${status.tunnelPid}`);
  for (const root of status.roots ?? []) console.log(`允许目录：${root}`);
  if (status.error) console.log(`错误：${status.error}`);
  console.log(`日志目录：${runDir}`);
}

async function commandLock(operation) {
  fs.mkdirSync(runDir, { recursive: true });
  const file = path.join(runDir, 'command.lock');
  let handle;
  for (let i = 0; i < 600; i++) {
    try {
      handle = fs.openSync(file, 'wx', 0o600);
      fs.writeFileSync(handle, String(process.pid));
      break;
    } catch (error) {
      if (error.code !== 'EEXIST') throw error;
      let owner;
      try { owner = Number(fs.readFileSync(file, 'utf8')); }
      catch (readError) { if (readError.code === 'ENOENT') continue; throw readError; }
      if (owner > 0) {
        try { process.kill(owner, 0); }
        catch (probe) { if (probe.code === 'ESRCH') { try { fs.unlinkSync(file); } catch {} } }
      }
      await sleep(200);
    }
  }
  if (handle === undefined) throw new Error('其他管理命令仍在运行，请稍后重试。');
  try { return await operation(); }
  finally { fs.closeSync(handle); fs.unlinkSync(file); }
}

function startChild(executable, childArgs, name) {
  const out = fs.openSync(path.join(runDir, `${name}.log`), 'a');
  const err = fs.openSync(path.join(runDir, `${name}.error.log`), 'a');
  try {
    const child = spawn(executable, childArgs, { env, windowsHide: true, stdio: ['ignore', out, err] });
    child.on('error', error => { child.launchError = error; });
    return child;
  } finally { fs.closeSync(out); fs.closeSync(err); }
}
function alive(child) { return child && child.exitCode === null && child.signalCode === null && !child.launchError; }
async function stopChild(child) {
  if (!alive(child)) return;
  if (process.platform === 'win32') {
    // Only kill process objects created by this worker, never a PID read from disk.
    try { execFileSync('taskkill.exe', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' }); }
    catch { if (alive(child)) throw new Error(`停止子进程失败：${child.pid}`); }
  } else { child.kill('SIGTERM'); }
  for (let i = 0; i < 100 && alive(child); i++) await sleep(50);
  if (alive(child)) { child.kill('SIGKILL'); await sleep(100); }
}
async function portFree(port) {
  return new Promise(resolve => {
    const listener = net.createServer();
    listener.once('error', () => resolve(false));
    listener.listen(port, '127.0.0.1', () => listener.close(() => resolve(true)));
  });
}
async function healthy(port) {
  return new Promise(resolve => {
    const req = http.get(`http://127.0.0.1:${port}/mcp`, res => { res.resume(); resolve(true); });
    req.setTimeout(500, () => req.destroy());
    req.on('error', () => resolve(false));
  });
}

async function worker() {
  let server, tunnel, stopping = false;
  let status = { phase: 'starting', roots: [], port: null };
  const token = randomBytes(32).toString('hex');
  let queue = Promise.resolve();
  const control = http.createServer(async (req, res) => {
    const reply = (code, value) => { res.writeHead(code, { 'content-type': 'application/json' }); res.end(JSON.stringify(value)); };
    if (req.headers.authorization !== `Bearer ${token}`) return reply(403, { error: '拒绝访问' });
    if (req.url === '/status') return reply(200, { ...status,
      serverPid: alive(server) ? server.pid : null, tunnelPid: alive(tunnel) ? tunnel.pid : null });
    try {
      let body = '';
      for await (const chunk of req) { body += chunk; if (body.length > 32768) throw new Error('请求过大'); }
      const data = JSON.parse(body || '{}');
      const job = queue.then(async () => {
        if (stopping) throw new Error('正在停止。');
        if (req.url === '/stop') {
          stopping = true;
          status.phase = 'stopping';
          await stopChild(server); await stopChild(tunnel);
          status.phase = 'stopped';
          return status;
        }
        if (req.url === '/pause') {
          status.phase = 'updating';
          await stopChild(server);
          return status;
        }
        if (req.url !== '/serve') throw new Error('未知管理命令');
        if (!alive(tunnel)) throw new Error('隧道已退出，请先运行 devspace_stop，再运行 devspace_serve。');
        const root = canonical(data.root);
        const config = loadConfig();
        const roots = config.workspaces.allowedRoots;
        const added = !roots.some(value => samePath(value, root));
        if (added) {
          config.workspaces.allowedRoots = [...roots, root];
          saveConfig(config);
        }
        status.roots = config.workspaces.allowedRoots;
        if (added || !alive(server)) {
          status.phase = 'restarting';
          await stopChild(server);
          await startServer();
        }
        return { ...status, added };
      });
      queue = job.catch(() => {});
      const result = await job;
      reply(200, result);
      if (stopping) control.close(() => process.exit(0));
    } catch (error) { reply(500, { error: error.message }); }
  });
  control.requestTimeout = 10000;
  async function startServer() {
    if (!await portFree(status.port)) throw new Error(`端口 ${status.port} 被其他进程占用。`);
    server = startChild(process.execPath, [cli, 'serve'], 'server');
    for (let i = 0; i < 300; i++) {
      if (!alive(server)) throw new Error(server.launchError?.message ?? 'Server 启动失败，请查看 server.error.log');
      if (await healthy(status.port)) { status.phase = 'running'; delete status.error; return; }
      await sleep(100);
    }
    throw new Error('Server 启动超时，请查看日志。');
  }
  async function cleanup(error) {
    stopping = true;
    status.phase = 'stopping';
    if (error) fs.appendFileSync(path.join(runDir, 'manager.error.log'), `${new Date().toISOString()} ${error.stack ?? error}\n`);
    await stopChild(server); await stopChild(tunnel);
    control.close();
  }
  process.on('SIGTERM', () => cleanup().finally(() => process.exit(0)));
  process.on('SIGINT', () => cleanup().finally(() => process.exit(0)));
  // Startup is part of the same queue as directory updates and stop requests.
  queue = (async () => {
    const config = loadConfig();
    requireAuth();
    status.port = config.server.port;
    status.roots = config.workspaces.allowedRoots;
    if (!await portFree(status.port)) throw new Error(`端口 ${status.port} 已被占用，请先停止旧的前台 server。`);
    // Truncate only tunnel logs; retaining old URLs would select the previous tunnel.
    for (const name of ['tunnel.log', 'tunnel.error.log']) fs.writeFileSync(path.join(runDir, name), '');
    tunnel = startChild(process.env.DEVSPACE_CLOUDFLARED ?? 'cloudflared',
      ['tunnel', '--no-autoupdate', '--url', `http://127.0.0.1:${status.port}`], 'tunnel');
    for (let i = 0; i < 600; i++) {
      if (!alive(tunnel)) throw new Error(tunnel.launchError?.message ?? 'Cloudflare 隧道启动失败。');
      const logs = ['tunnel.log', 'tunnel.error.log'].map(name => fs.readFileSync(path.join(runDir, name), 'utf8')).join('\n');
      const url = logs.match(/https:\/\/[A-Za-z0-9.-]+\.trycloudflare\.com/)?.[0];
      if (url) { status.url = url; break; }
      await sleep(100);
    }
    if (!status.url) throw new Error('60 秒内未获取到 Cloudflare 公网地址。');
    config.server.publicBaseUrl = status.url;
    saveConfig(config);
    await startServer();
  })();
  queue.catch(() => {});
  await new Promise(resolve => control.listen(0, '127.0.0.1', resolve));
  write(stateFile, { controlPort: control.address().port, token, workerPid: process.pid });
  try { await queue; }
  catch (error) { await cleanup(error); throw error; }
  const monitor = setInterval(() => {
    if (!stopping && status.phase !== 'updating' && (!alive(tunnel) || !alive(server))) {
      status.phase = 'error';
      status.error = !alive(tunnel) ? '隧道已退出，请 devspace_stop 后重新 devspace_serve。' : 'Server 已退出，可运行 devspace_serve 重启。';
    }
  }, 1000);
  monitor.unref();
}

async function main() {
  if (!cli) throw new Error('缺少官方 CLI 路径，请通过 devspace-control.ps1 调用。');
  if (command === '__worker') return worker();
  if (!['serve', 'start', 'stop', 'status'].includes(command)) {
    const child = spawn(process.execPath, [cli, command, ...args], { stdio: 'inherit', windowsHide: true });
    child.on('error', error => { console.error(error.message); process.exitCode = 1; });
    child.on('exit', code => { process.exitCode = code ?? 1; });
    return;
  }
  if (args.length) throw new Error('用法：devspace_serve | devspace_stop | devspace_status（不需要其他参数）');
  if (command === 'status') {
    const running = await current();
    display(running?.status ?? { phase: 'stopped' });
    return;
  }
  if (command === 'serve' || command === 'start') requireAuth();
  await commandLock(async () => {
    let running = await current();
    if (command === 'stop') {
      if (!running) { console.log('DevSpace 已停止。'); return; }
      await request(running.state, 'stop');
      // Wait for control listener to close before a subsequent serve starts.
      for (let i = 0; i < 100 && await current(); i++) await sleep(50);
      console.log('DevSpace server 和 Cloudflare 隧道已停止。');
      return;
    }
    const root = canonical(process.cwd());
    await checkUpdate({ cli,
      beforeInstall: async () => { if (running) await request(running.state, 'pause'); },
      afterInstall: async () => { if (running) await request(running.state, 'serve', { root }); },
    });
    if (!running) {
      const config = loadConfig();
      requireAuth();
      if (!config.workspaces.allowedRoots.some(value => samePath(value, root))) {
        config.workspaces.allowedRoots.push(root); saveConfig(config);
      }
      const log = fs.openSync(path.join(runDir, 'manager.log'), 'a');
      const errorLog = fs.openSync(path.join(runDir, 'manager.error.log'), 'a');
      const child = spawn(process.execPath, [fileURLToPath(import.meta.url), '--cli', cli, '__worker'],
        { env, detached: true, windowsHide: true, stdio: ['ignore', log, errorLog] });
      child.on('error', error => { console.error(error.message); });
      child.unref(); fs.closeSync(log); fs.closeSync(errorLog);
      for (let i = 0; i < 100; i++) {
        running = await current();
        if (running) break;
        await sleep(100);
      }
      if (!running) throw new Error(`后台管理进程启动失败，请查看 ${runDir}`);
    }
    const result = await request(running.state, 'serve', { root });
    console.log(result.added ? '已追加当前目录并重启 server；隧道保持运行。' : '当前目录已开放，DevSpace 在后台运行。');
    display(result);
    const auth = read(path.join(configDir, 'auth.json'));
    if (auth?.ownerToken) console.log(`Owner password：${auth.ownerToken}`);
    else console.log(`未找到 Owner password，请检查 ${path.join(configDir, 'auth.json')}`);
  });
}

main().catch(error => { console.error(`错误：${error.message}`); process.exitCode = 1; });
