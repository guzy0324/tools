import fs from 'node:fs';
import path from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { createInterface } from 'node:readline/promises';

const execute = promisify(execFile);
function npmCliPath() {
  const directories = [path.dirname(process.execPath), ...(process.env.PATH ?? '').split(path.delimiter)];
  for (const directory of directories) {
    const candidate = path.join(directory, 'node_modules', 'npm', 'bin', 'npm-cli.js');
    if (fs.existsSync(candidate)) return candidate;
  }
  throw new Error('找不到 npm，请确认已安装并加入 PATH。');
}
async function npm(args, options = {}) {
  const result = await execute(process.execPath, [npmCliPath(), ...args], {
    windowsHide: true, encoding: 'utf8', maxBuffer: 4 * 1024 * 1024, ...options,
  });
  return result.stdout.trim();
}

export function newer(latest, installed) {
  const parse = value => {
    const match = /^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$/.exec(value);
    if (!match) throw new Error(`无法解析版本号：${value}`);
    return { core: match.slice(1, 4).map(Number), pre: match[4]?.split('.') };
  };
  const a = parse(latest), b = parse(installed);
  for (let i = 0; i < 3; i++) if (a.core[i] !== b.core[i]) return a.core[i] > b.core[i];
  if (!a.pre || !b.pre) return !a.pre && !!b.pre;
  for (let i = 0; i < Math.max(a.pre.length, b.pre.length); i++) {
    const x = a.pre[i], y = b.pre[i];
    if (x === y) continue;
    if (x === undefined || y === undefined) return y === undefined;
    const numericX = /^\d+$/.test(x), numericY = /^\d+$/.test(y);
    if (numericX && numericY) return Number(x) > Number(y);
    if (numericX !== numericY) return !numericX;
    return x > y;
  }
  return false;
}

export async function checkUpdate({ cli, beforeInstall = async () => {}, afterInstall = async () => {},
  runNpm = npm, environment = process.env, log = console.log,
  confirm = async () => {
    if (!process.stdin.isTTY || !process.stdout.isTTY) return false;
    const terminal = createInterface({ input: process.stdin, output: process.stdout });
    try { return /^(y|yes)$/i.test((await terminal.question('是否更新 DevSpace？[y/N]: ')).trim()); }
    finally { terminal.close(); }
  },
}) {
  if (environment.DEVSPACE_UPDATE_CHECK === '0') return false;
  let latest;
  const packageName = '@waishnav/devspace';
  try {
    const installed = JSON.parse(fs.readFileSync(path.join(path.dirname(cli), '..', 'package.json'), 'utf8')).version;
    log(`正在检查 DevSpace 版本，当前：${installed}`);
    latest = JSON.parse(await runNpm(['view', packageName, 'version', '--json'], { timeout: 15000 }));
    if (!newer(latest, installed)) { log('DevSpace 已是最新版本。'); return false; }
    log(`发现 DevSpace 新版本：${installed} → ${latest}`);
  } catch (error) {
    log(`版本检查失败，继续启动：${error.message}`);
    return false;
  }
  const policy = environment.DEVSPACE_UPDATE?.toLowerCase();
  const accepted = ['y', 'yes', '1', 'true'].includes(policy)
    || (!['n', 'no', '0', 'false'].includes(policy) && await confirm());
  if (!accepted) { log('已跳过更新。可设置 DEVSPACE_UPDATE=y 自动更新。'); return false; }
  let paused = false;
  try {
    await beforeInstall();
    paused = true;
    log(`正在更新 DevSpace 至 ${latest}...`);
    // Use the selected version, and preserve the prefix owning the active CLI.
    const packageRoot = path.resolve(path.dirname(cli), '..');
    const modules = path.resolve(packageRoot, '..', '..');
    const prefix = process.platform === 'win32' ? path.dirname(modules) : path.resolve(modules, '..', '..');
    await runNpm(['install', '-g', '--prefix', prefix, `${packageName}@${latest}`], { timeout: 300000 });
    log('DevSpace 更新完成。');
    return true;
  } catch (error) {
    log(`DevSpace 更新失败：${error.message}`);
    return false;
  } finally {
    if (paused) await afterInstall();
  }
}
