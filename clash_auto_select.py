#!/usr/bin/env python3
"""Clash Verge Rev / Mihomo historical URL latency selector (Python 3.8+)."""
import argparse
import hashlib
import sqlite3
import sys
import json
import os
from pathlib import Path
import statistics
import tempfile
import time
from urllib.parse import quote, urlencode
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
from contextlib import closing
from datetime import datetime
from threading import Event, Thread
import webbrowser


TRAY_ICON = Path(__file__).resolve().parent / 'assets' / 'clash-dashboard.png'


def log(message):
    print(time.strftime('%H:%M:%S'), message, flush=True)


def api(path, method='GET', data=None, timeout=20):
    headers = {'Authorization': 'Bearer ' + SECRET} if SECRET else {}
    payload = None
    if data is not None:
        headers['Content-Type'] = 'application/json'
        payload = json.dumps(data).encode('utf-8')
    req = Request(ARGS.api.rstrip('/') + path, data=payload,
                  headers=headers, method=method)
    # Do not use the system proxy to reach the local controller.
    with OPENER.open(req, timeout=timeout) as response:
        raw = response.read()
    return json.loads(raw) if raw else None


def metrics(samples, jitter_weight, failure_penalty):
    good = [delay for _, delay in samples if delay > 0]
    if not good:
        return None
    rate = len(good) / len(samples)
    avg = statistics.mean(good)
    jitter = statistics.pstdev(good)
    score = avg + jitter_weight * jitter + failure_penalty * (1 - rate)
    return score, avg, rate, jitter, len(samples)


def select_node(history, delays, current):
    # Only nodes that passed this round can be selected.
    rows = [(name, metrics(history[name], ARGS.jitter_weight, ARGS.failure_penalty))
            for name in delays if delays[name] > 0]
    rows.sort(key=lambda row: (row[1][0], row[0]))
    if not rows:
        return None, rows, '本轮所有节点失败，保持当前选择'
    best, best_stats = rows[0]
    if delays.get(current, 0) <= 0:
        return best, rows, '当前节点不可用，故障切换'
    current_stats = metrics(history[current], ARGS.jitter_weight, ARGS.failure_penalty)
    if current_stats[0] - best_stats[0] > ARGS.tolerance:
        return best, rows, '历史综合评分优选'
    return current, rows, '当前节点仍在切换容差内'


def fingerprint(proxy):
    """Hash every top-level field except the display name; preserve nested names."""
    data = {k: v for k, v in proxy.items() if k != 'name'}
    raw = json.dumps(data, sort_keys=True, ensure_ascii=False,
                     separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def discover_config():
    home = Path.home()
    roots = [Path(os.environ.get('APPDATA', str(home / 'AppData/Roaming'))),
             home / 'Library/Application Support',
             Path(os.environ.get('XDG_DATA_HOME', str(home / '.local/share'))),
             Path(os.environ.get('XDG_CONFIG_HOME', str(home / '.config')))]
    candidates = [root / app / 'clash-verge.yaml' for root in roots
                  for app in ('io.github.clash-verge-rev.clash-verge-rev',
                 'io.github.clash-verge-rev.clash-verge',
                 'io.github.fqfqgo.clash-verge-for-v2free', 'clash-verge')]
    found = list(dict.fromkeys(p.resolve() for p in candidates if p.is_file()))
    if len(found) != 1:
        raise RuntimeError('找不到唯一运行配置，请通过 --config 指定 clash-verge.yaml')
    return found[0]


def read_yaml(path):
    try:
        import yaml
    except ImportError:
        raise RuntimeError('缺少 PyYAML，请运行 python -m pip install PyYAML') from None
    try:
        with path.open(encoding='utf-8-sig') as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            raise ValueError()
        return data
    except (OSError, ValueError, yaml.YAMLError):
        # YAML parse errors can contain credential-bearing source lines.
        raise RuntimeError('无法读取或解析本地 YAML（检查路径及文件格式）') from None


def install_verge_script(config_path, group, url):
    profiles_path = config_path.parent / 'profiles.yaml'
    if not profiles_path.is_file():
        return
    entry = next((item for item in (read_yaml(profiles_path).get('items') or [])
                  if isinstance(item, dict) and item.get('uid') == 'Script'
                  and item.get('type') == 'script'), None)
    if entry is None:
        return
    filename = entry.get('file')
    if not isinstance(filename, str) or not filename or Path(filename).name != filename:
        raise RuntimeError('Clash Verge 全局扩展脚本路径无效')
    path = config_path.parent / 'profiles' / filename
    if path.is_symlink():
        raise RuntimeError('Clash Verge 全局扩展脚本路径是符号链接')
    previous = path.read_bytes() if path.is_file() else b''
    original = previous[3:] if previous.startswith(b'\xef\xbb\xbf') else previous
    header = (b'// Managed by clash_auto_select.py\n'
              b'const clashAutoSelectPreviousMain = ((main) => (() => {\n')
    marker = b'\n// End of the original global script\n'
    if original.startswith(header):
        original, separator, _ = original[len(header):].rpartition(marker)
        if not separator:
            raise RuntimeError('全局扩展脚本的安装标记不完整')
    group_json, url_json = json.dumps(group), json.dumps(url)
    managed = f"""  return typeof main === "function" ? main : null;
}})())();
function main(config, ...args) {{
  if (clashAutoSelectPreviousMain) {{
    config = clashAutoSelectPreviousMain(config, ...args) ?? config;
  }}
  const name = {group_json};
  const groups = config["proxy-groups"] ??= [];
  if (!groups.some(group => group.name === name)) {{
    groups.unshift({{name, type: "select", "include-all": true, url: {url_json}}});
  }}
  for (const group of groups) {{
    if (group.type === "select" && group.name !== name) {{
      group.proxies = [name, ...(group.proxies || []).filter(proxy => proxy !== name)];
    }}
  }}
  return config;
}}
"""
    updated = header + original + marker + managed.encode('utf-8')
    if updated == previous:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        temporary.write(updated)
    temporary_path = Path(temporary.name)
    try:
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)
    log(f'已安装 Clash Verge 全局扩展脚本: {path}')


def read_nodes(config_path, core_home=None):
    config = read_yaml(config_path)
    result = {}
    ambiguous = set()
    def add(proxy):
        if not isinstance(proxy, dict) or not isinstance(proxy.get('name'), str):
            return
        name = proxy['name']
        # Only actual outbound node definitions, never group definitions.
        if not proxy.get('type') or proxy['type'] in ('direct', 'reject', 'pass'):
            return
        try:
            fp = fingerprint(proxy)
        except (ValueError, TypeError):
            ambiguous.add(name)
            return
        if name in result and result[name] != fp:
            ambiguous.add(name)
        result[name] = fp
    for proxy in config.get('proxies') or []:
        add(proxy)
    for provider in (config.get('proxy-providers') or {}).values():
        if not isinstance(provider, dict):
            continue
        override = provider.get('override') or {}
        # Do not guess effective configs for arbitrary rename/override operations.
        if set(override) - {'additional-prefix', 'additional-suffix'} or provider.get('age-secret-key'):
            continue
        if provider.get('type') == 'inline':
            nodes = provider.get('payload') or []
        else:
            if provider.get('type') not in ('http', 'file'):
                continue
            root = core_home or config_path.parent
            if provider.get('path'):
                path = root / provider['path']
                paths = [path]
            elif provider.get('url'):
                key = hashlib.md5(provider['url'].encode()).hexdigest()
                paths = [root / 'proxy_providers' / key, root / key]
            else:
                continue
            existing = [p for p in paths if p.is_file()]
            if len(existing) != 1:
                continue
            try:
                nodes = read_yaml(existing[0]).get('proxies') or []
            except RuntimeError:
                continue
        for node in nodes:
            if not isinstance(node, dict) or not isinstance(node.get('name'), str):
                continue
            proxy = dict(node)
            proxy['name'] = (override.get('additional-prefix', '') + proxy['name']
                             + override.get('additional-suffix', ''))
            add(proxy)
    for name in ambiguous:
        result.pop(name, None)
    return result


def open_database(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=15)
    db.execute('''CREATE TABLE IF NOT EXISTS speed_history (
        fingerprint TEXT NOT NULL, test_key TEXT NOT NULL,
        timestamp REAL NOT NULL, delay INTEGER NOT NULL,
        PRIMARY KEY(fingerprint, test_key, timestamp))''')
    db.execute('CREATE INDEX IF NOT EXISTS history_time ON speed_history(timestamp)')
    return db


def state_key(args):
    return json.dumps([args.url, args.expected, args.timeout], separators=(',', ':'))


def save_samples(db, samples, now):
    key = state_key(ARGS)
    with db:
        db.executemany('INSERT OR REPLACE INTO speed_history VALUES (?, ?, ?, ?)',
                       [(fp, key, now, delay) for fp, delay in samples.items()])
        db.execute('DELETE FROM speed_history WHERE timestamp < ?',
                   (now - ARGS.retention * 86400,))


def read_history(db, fingerprints, now):
    key = state_key(ARGS)
    result = {}
    for fp in set(fingerprints.values()):
        result[fp] = db.execute('SELECT timestamp, delay FROM speed_history '
            'WHERE fingerprint=? AND test_key=? AND timestamp BETWEEN ? AND ? '
            'ORDER BY timestamp', (fp, key, now - ARGS.window * 60, now)).fetchall()
    return {name: result[fp] for name, fp in fingerprints.items()}


def snapshot():
    group = api('/proxies/' + quote(ARGS.group, safe=''))
    if group.get('type', '').lower() != 'selector':
        raise RuntimeError('目标组必须为 select (Selector) 类型')
    excluded = {'DIRECT', 'REJECT', 'REJECT-DROP', 'COMPATIBLE', 'PASS', ARGS.group}
    names = set(group.get('all', [])) - excluded
    configs = read_nodes(ARGS.config, ARGS.core_home)
    # Provider proxies may not appear in /proxies on some core versions.
    live = api('/proxies').get('proxies', {})
    if any(name not in live for name in names):
        providers = api('/providers/proxies').get('providers', {})
        for provider in providers.values():
            for node in provider.get('proxies', []):
                live.setdefault(node.get('name'), node)
    names = {name for name in names if not isinstance(live.get(name, {}).get('all'), list)}
    if not names:
        raise RuntimeError('策略组没有可测速节点')
    missing = names - configs.keys()
    if missing:
        raise RuntimeError(f'{len(missing)} 个节点无法可靠匹配完整配置；跳过整轮（检查 provider 路径、加密或 override）')
    identities = {name: (configs[name], live.get(name, {}).get('id')) for name in names}
    return group, identities


def round_once(db, stop):
    group_path = '/proxies/' + quote(ARGS.group, safe='')
    group, identities = snapshot()
    fingerprints = {name: identity[0] for name, identity in identities.items()}
    query = urlencode({'url': ARGS.url, 'timeout': ARGS.timeout,
                       'expected': ARGS.expected})
    # Probe each distinct configuration only once. Keep current alias if possible.
    representatives = {}
    for name in sorted(fingerprints, key=lambda n: (n != group.get('now'), n)):
        representatives.setdefault(fingerprints[name], name)
    def probe(name):
        try:
            result = api('/proxies/' + quote(name, safe='') + '/delay?' + query,
                         timeout=ARGS.timeout / 1000 + 10)
        except HTTPError as exc:
            if exc.code in (401, 403, 404):
                raise
            if exc.code in (408, 504, 503, 502, 500):
                return 0
            raise
        except TimeoutError:
            return 0
        value = result.get('delay', 0) if isinstance(result, dict) else 0
        return value if type(value) is int and 0 < value < 65535 else 0
    with ThreadPoolExecutor(max_workers=ARGS.workers) as pool:
        delays_by_fp = dict(zip(representatives, pool.map(probe, representatives.values())))
    if stop.is_set():
        return
    latest, after = snapshot()
    if identities != after:
        raise RuntimeError('测速期间节点配置或实例发生变化；丢弃本轮数据')
    now = time.time()
    save_samples(db, delays_by_fp, now)
    delays = {name: delays_by_fp[fp] for name, fp in fingerprints.items()}
    history = read_history(db, fingerprints, now)
    current = latest.get('now')
    best, rows, reason = select_node(history, delays, current)
    log(f'成功 {sum(d > 0 for d in delays_by_fp.values())}/{len(delays_by_fp)} 个独立指纹，当前: {current}')
    for name, (score, avg, rate, jitter, count) in rows[:5]:
        log(f'  {name} [{fingerprints[name][:12]}] | 最新 {delays[name]}ms | 均值 {avg:.1f}ms | '
            f'成功率 {rate:.0%} | 波动 {jitter:.1f}ms | 评分 {score:.1f} | {count}次')
    if best and best != current:
        if ARGS.dry_run:
            log(f'[预览] {reason}: {current} -> {best}')
        else:
            final_group, final_ids = snapshot()
            if final_ids != identities or final_group.get('now') != current:
                raise RuntimeError('切换前配置或选择已变化，取消切换')
            if stop.is_set():
                return
            api(group_path, method='PUT', data={'name': best})
            log(f'{reason}: {current} -> {best}')
    else:
        log(reason)


def controller_secret(config):
    secret = os.environ.get('CLASH_SECRET')
    if secret is None:
        secret = read_yaml(config).get('secret')
    if secret is None:
        return ''
    if not isinstance(secret, str):
        raise RuntimeError('运行配置中的 secret 必须是字符串')
    return secret


def run_selection(stop):
    try:
        db = open_database(ARGS.state)
    except (OSError, sqlite3.Error) as exc:
        log(str(exc))
        return 1
    with closing(db):
        return selection_loop(db, stop)


def selection_loop(db, stop):
    log(f'运行配置: {ARGS.config} | 历史数据库: {ARGS.state}')
    log(f'启动: {ARGS.group} | {ARGS.url} | 每 {ARGS.interval}s | 历史 {ARGS.window}min')
    while not stop.is_set():
        start = time.monotonic()
        try:
            round_once(db, stop)
        except HTTPError as exc:
            log(f'API HTTP {exc.code}: {exc.reason}；跳过本轮')
            if exc.code in (401, 403):
                log('请检查外部控制器 Secret')
                return 1
            if ARGS.once:
                return 1
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, sqlite3.Error) as exc:
            log(f'本轮失败: {exc}；不主动切换，等待下轮')
            if ARGS.once:
                return 1
        if ARGS.once:
            return 0
        stop.wait(max(1, ARGS.interval - (time.monotonic() - start)))


def create_app(args):
    from flask import Flask, render_template

    app = Flask(__name__)
    test_key = state_key(args)

    @app.get('/')
    def dashboard():
        rows = []
        names = defaultdict(list)
        config_error = None
        try:
            for name, fingerprint in read_nodes(args.config, args.core_home).items():
                names[fingerprint].append(name)
        except (RuntimeError, OSError, TypeError, ValueError):
            config_error = '无法读取节点名称，表格将显示配置指纹。'

        latest = None
        db_error = None
        if args.state.is_file():
            try:
                uri = args.state.resolve().as_uri() + '?mode=ro'
                with closing(sqlite3.connect(uri, uri=True)) as db:
                    db.execute('BEGIN')
                    latest, = db.execute(
                        'SELECT MAX(timestamp) FROM speed_history WHERE test_key = ?', (test_key,)
                    ).fetchone()
                    if latest is not None:
                        samples = db.execute(
                            'SELECT fingerprint, timestamp, delay FROM speed_history '
                            'WHERE test_key = ? AND timestamp BETWEEN ? AND ? ORDER BY timestamp',
                            (test_key, latest - args.window * 60, latest),
                        ).fetchall()
                    else:
                        samples = []
            except sqlite3.Error:
                db_error = '暂时无法读取测速数据库。'
                samples = []

            histories = defaultdict(list)
            for fingerprint, timestamp, delay in samples:
                histories[fingerprint].append((timestamp, delay))
            for fingerprint, history in histories.items():
                timestamp, delay = history[-1]
                if timestamp != latest:
                    continue
                label = ' / '.join(sorted(names[fingerprint])) or fingerprint[:12]
                score, average, rate, jitter, count = (
                    metrics(history, args.jitter_weight, args.failure_penalty)
                    or (None, None, 0, None, len(history))
                )
                rows.append({
                    'label': label, 'delay': delay, 'score': score, 'average': average,
                    'rate': rate, 'jitter': jitter, 'count': count,
                })
            rows.sort(key=lambda row: (
                row['delay'] <= 0, row['score'] if row['score'] is not None else float('inf'),
                row['label'],
            ))

        return render_template(
            'clash_dashboard.html', rows=rows, args=args, config_error=config_error,
            db_error=db_error, latest=datetime.fromtimestamp(latest) if latest is not None else None,
            successful=sum(row['delay'] > 0 for row in rows),
        )

    return app


def run_with_tray(app, port, measure):
    from werkzeug.serving import make_server
    import pystray
    from PIL import Image

    url = f'http://127.0.0.1:{port}/'
    with Image.open(TRAY_ICON) as asset:
        image = asset.convert('RGBA')

    icon = pystray.Icon(
        'clash_dashboard', image, 'Clash 节点测速',
        pystray.Menu(
            pystray.MenuItem('打开看板', lambda icon, item: webbrowser.open(url), default=True),
            pystray.MenuItem('退出看板', lambda icon, item: icon.stop()),
        ),
    )
    with make_server('127.0.0.1', port, app, threaded=True) as server:
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        if measure:
            Thread(target=run_selection, args=(measure,), daemon=True).start()
        print(f'Clash 节点测速看板已启动 · {url}', flush=True)
        try:
            icon.run()
        finally:
            if measure:
                measure.set()
            server.shutdown()
            worker.join()


def run_ubuntu_tray(port):
    import fcntl
    import gi

    gi.require_version('Gtk', '3.0')
    gi.require_version('AyatanaAppIndicator3', '0.1')
    from gi.repository import AyatanaAppIndicator3, Gtk

    url = f'http://127.0.0.1:{port}/'
    lock_path = Path(os.environ['XDG_RUNTIME_DIR']) / 'clash-dashboard-tray.lock'
    with lock_path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return

        indicator = AyatanaAppIndicator3.Indicator.new_with_path(
            'clash-dashboard-tray', TRAY_ICON.stem,
            AyatanaAppIndicator3.IndicatorCategory.APPLICATION_STATUS,
            str(TRAY_ICON.parent),
        )
        indicator.set_title('Clash 节点测速')
        menu = Gtk.Menu()
        open_item = Gtk.MenuItem(label='打开看板')
        open_item.connect('activate', lambda _: Gtk.show_uri_on_window(None, url, 0))
        exit_item = Gtk.MenuItem(label='退出托盘')
        exit_item.connect('activate', lambda _: Gtk.main_quit())
        menu.append(open_item)
        menu.append(exit_item)
        menu.show_all()
        indicator.set_menu(menu)
        indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.ACTIVE)
        Gtk.main()


def main():
    global ARGS, SECRET, OPENER
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api', default='http://127.0.0.1:9097')
    parser.add_argument('--group', default='ChatGPT 自动选节点')
    parser.add_argument('--url', default='https://chatgpt.com/')
    parser.add_argument('--expected', default='200-399', help='HTTP status range; * accepts any')
    parser.add_argument('--interval', type=int, default=60, help='seconds between round starts')
    parser.add_argument('--window', type=int, default=30, help='history window in minutes')
    parser.add_argument('--timeout', type=int, default=5000, help='probe timeout in ms')
    parser.add_argument('--tolerance', type=float, default=30, help='minimum score improvement')
    parser.add_argument('--jitter-weight', type=float, default=0.5)
    parser.add_argument('--failure-penalty', type=float, default=5000)
    parser.add_argument('--state', type=Path, default=Path(__file__).resolve().with_name('clash_history.sqlite3'))
    parser.add_argument('--config', type=Path, help='path to generated clash-verge.yaml')
    parser.add_argument('--core-home', type=Path, help='Mihomo home directory for relative provider paths')
    parser.add_argument('--retention', type=int, default=7, help='history retention in days')
    parser.add_argument('--workers', type=int, default=12, help='concurrent probe limit')
    parser.add_argument('--once', action='store_true', help='run only one round')
    interface = parser.add_mutually_exclusive_group()
    interface.add_argument('--dashboard', action='store_true', help='启动 Flask 看板')
    interface.add_argument('--tray', action='store_true', help='启动 Ubuntu 桌面托盘')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true', help='测速并预览切换')
    mode.add_argument('--auto-select', action='store_true', help='随看板启动自动切换')
    parser.add_argument('--port', type=int, default=5050, help='看板端口')
    parser.add_argument('--no-tray', action='store_true', help='只启动网页服务')
    ARGS = parser.parse_args()
    if ARGS.tray:
        if ARGS.once or ARGS.dry_run or ARGS.auto_select or ARGS.no_tray:
            parser.error('--tray 用于单独启动 Ubuntu 桌面托盘')
        if not sys.platform.startswith('linux') or not os.environ.get('XDG_RUNTIME_DIR'):
            parser.error('--tray 需要 Ubuntu 图形桌面会话')
        try:
            return run_ubuntu_tray(ARGS.port)
        except (ImportError, ValueError):
            parser.error('托盘需要系统 Python 及 Ubuntu 的 GTK 和 Ayatana AppIndicator 绑定')
    if ARGS.dashboard and ARGS.once:
        parser.error('--once 用于单轮前台测试')
    if ARGS.auto_select and not ARGS.dashboard:
        parser.error('--auto-select 需与 --dashboard 一起使用')
    measuring = not ARGS.dashboard or ARGS.dry_run or ARGS.auto_select
    if min(ARGS.interval, ARGS.window, ARGS.timeout, ARGS.retention, ARGS.workers) <= 0 or ARGS.window > ARGS.retention * 1440 or min(ARGS.tolerance, ARGS.jitter_weight, ARGS.failure_penalty) < 0:
        parser.error('参数超出有效范围')
    try:
        ARGS.config = ARGS.config.expanduser().resolve() if ARGS.config else discover_config()
        if not ARGS.config.is_file():
            raise RuntimeError('指定的运行配置不存在')
        if ARGS.core_home:
            ARGS.core_home = ARGS.core_home.expanduser().resolve()
        if ARGS.state.suffix.lower() == '.json':
            raise RuntimeError('新版使用 SQLite，请指定新的 .sqlite3 历史文件')
        install_verge_script(ARGS.config, ARGS.group, ARGS.url)
        if measuring:
            SECRET = controller_secret(ARGS.config)
    except (RuntimeError, OSError, sqlite3.Error) as exc:
        log(str(exc))
        return 1
    OPENER = build_opener(ProxyHandler({}))
    if not ARGS.dashboard:
        return run_selection(Event())

    stop = Event()
    measure = stop if measuring else None
    try:
        app = create_app(ARGS)
        if sys.platform == 'win32' and not ARGS.no_tray:
            run_with_tray(app, ARGS.port, measure)
        else:
            if measure:
                Thread(target=run_selection, args=(measure,), daemon=True).start()
            app.run(host='127.0.0.1', port=ARGS.port)
    except ImportError as exc:
        parser.error(f'看板依赖未安装，请运行 python -m pip install Flask pystray Pillow（{exc.name}）')
    finally:
        stop.set()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log('已停止')
