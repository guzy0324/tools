#!/usr/bin/env python3
"""Clash Dashboard for Clash Verge Rev / Mihomo (Python 3.8+)."""
import argparse
import hashlib
import hmac
import ipaddress
import logging
import re
import secrets
import socket
import sqlite3
import subprocess
import sys
import json
import os
from pathlib import Path
import statistics
import tempfile
import time
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
from contextlib import closing
from datetime import datetime
from email.utils import formatdate
from threading import Event, Lock, Thread
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
        return parse_yaml(path.read_bytes())
    except OSError:
        raise RuntimeError('无法读取或解析本地 YAML（检查路径及文件格式）') from None


def parse_yaml(raw):
    try:
        import yaml
    except ImportError:
        raise RuntimeError('缺少 PyYAML，请运行 python -m pip install PyYAML') from None
    try:
        data = yaml.safe_load(raw.decode('utf-8-sig'))
        if not isinstance(data, dict):
            raise ValueError()
        return data
    except (ValueError, yaml.YAMLError):
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
    header = (b'// Managed by clash_dashboard.py\n'
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


def subscription_uid():
    try:
        return read_yaml(ARGS.config.with_name('profiles.yaml')).get('current')
    except RuntimeError:
        return None


def round_once(db, stop, uid, *, select=True):
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
    if subscription_uid() != uid or identities != after:
        raise RuntimeError('测速期间节点配置或实例发生变化；丢弃本轮数据')
    now = time.time()
    save_samples(db, delays_by_fp, now)
    if not select:
        return
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
            if (subscription_uid() != uid or final_ids != identities
                    or final_group.get('now') != current):
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


class Measurement:
    def __init__(self, stop):
        self.stop = stop
        self.lock = Lock()
        self.message = ''

    def start(self):
        if self.stop.is_set() or not self.lock.acquire(blocking=False):
            return
        try:
            Thread(target=self.run_manual, daemon=True).start()
        except RuntimeError:
            self.message = '无法启动手动测速。'
            self.lock.release()

    def run_manual(self):
        try:
            with closing(open_database(ARGS.state)) as db:
                round_once(db, self.stop, subscription_uid(), select=False)
            self.message = '手动测速完成。'
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, sqlite3.Error) as exc:
            log(f'手动测速失败: {exc}')
            self.message = '手动测速失败，请查看运行日志。'
        finally:
            self.lock.release()


def run_selection(stop, lock=None):
    try:
        db = open_database(ARGS.state)
    except (OSError, sqlite3.Error) as exc:
        log(str(exc))
        return 1
    with closing(db):
        return selection_loop(db, stop, lock or Lock())


def selection_loop(db, stop, lock):
    log(f'运行配置: {ARGS.config} | 历史数据库: {ARGS.state}')
    log(f'启动: {ARGS.group} | {ARGS.url} | 每 {ARGS.interval}s | 历史 {ARGS.window}min')
    while not stop.is_set():
        start = time.monotonic()
        uid = None
        try:
            with lock:
                if stop.is_set():
                    return 0
                uid = subscription_uid()
                round_once(db, stop, uid)
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
        deadline = max(time.monotonic() + 1, start + ARGS.interval)
        while not stop.is_set():
            current = subscription_uid()
            if current is not None and current != uid:
                # Give Verge time to finish regenerating and loading its config.
                if stop.wait(1):
                    return 0
                if subscription_uid() == current:
                    log('检测到订阅切换，立即测速')
                    break
            remaining = deadline - time.monotonic()
            if remaining <= 0 or stop.wait(min(1, remaining)):
                break


class SubscriptionLogFilter(logging.Filter):
    def filter(self, record):
        return '/subscription/' not in record.getMessage()


def remote_profiles(config):
    items = read_yaml(config.parent / 'profiles.yaml').get('items')
    if not isinstance(items, list):
        raise RuntimeError('Clash Verge 订阅列表无效')
    return [item for item in items if isinstance(item, dict)
            and item.get('type') == 'remote' and isinstance(item.get('uid'), str)]


def read_subscription(config, profile):
    filename = profile.get('file')
    if not isinstance(filename, str) or not re.fullmatch(r'[A-Za-z0-9_-]+\.ya?ml', filename):
        raise RuntimeError('远程订阅缓存文件名无效')
    path = config.parent / 'profiles' / filename
    if path.is_symlink():
        raise RuntimeError('远程订阅缓存路径是符号链接')
    raw = path.read_bytes()
    data = parse_yaml(raw)
    if not isinstance(data.get('proxies'), list) or not data['proxies']:
        raise RuntimeError('订阅缓存缺少已解析的节点')
    return raw


def subscription_headers(profile):
    headers = {'Cache-Control': 'no-store'}
    extra = profile.get('extra')
    if isinstance(extra, dict):
        values = [f'{field}={extra[field]}' for field in ('upload', 'download', 'total', 'expire')
                  if type(extra.get(field)) is int and extra[field] >= 0]
        if values:
            headers['Subscription-Userinfo'] = '; '.join(values)

    name = profile.get('name')
    if isinstance(name, str) and '://' not in name:
        name = re.sub(r'[\\/":*?<>|\x00-\x1f\x7f]', '', name).strip()[:180]
    else:
        name = ''
    name = name or 'config.yaml'
    fallback = name if re.fullmatch(r'[A-Za-z0-9 ._-]+', name) else 'config.yaml'
    headers['Content-Disposition'] = f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{quote(name, safe="")}'

    option = profile.get('option')
    minutes = option.get('update_interval') if isinstance(option, dict) else None
    if type(minutes) is int and minutes > 0:
        # Clash Verge stores minutes and expects whole hours in the response.
        headers['Profile-Update-Interval'] = str((minutes + 59) // 60)

    home = profile.get('home')
    if isinstance(home, str) and home.isascii() and home == home.strip() and not re.search(r'[\x00-\x20\x7f]', home):
        try:
            url = urlsplit(home)
            if (url.scheme in ('http', 'https') and url.hostname and url.username is None
                    and not url.query and home != profile.get('url')):
                headers['Profile-Web-Page-Url'] = home
        except ValueError:
            pass

    updated = profile.get('updated')
    if type(updated) is int and updated > 0:
        try:
            headers['Last-Modified'] = formatdate(updated, usegmt=True)
        except (OverflowError, OSError, ValueError):
            pass
    return headers


def subscription_ip():
    import psutil

    interfaces = psutil.net_if_stats()
    addresses = {
        ipaddress.IPv4Address(address.address)
        for name, entries in psutil.net_if_addrs().items()
        if name in interfaces and interfaces[name].isup
        for address in entries
        if address.family == socket.AF_INET
    }
    preferred = None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
            route.connect(('1.1.1.1', 443))
            preferred = ipaddress.IPv4Address(route.getsockname()[0])
    except OSError:
        pass
    candidates = [ip for ip in addresses if (ip.is_global or ip.is_private)
                  and not (ip.is_loopback or ip.is_link_local or ip.is_multicast
                           or ip.is_unspecified or ip.is_reserved)]
    return min(candidates, key=lambda ip: (not ip.is_global, ip != preferred, ip), default=None)


def create_app(args, measurement):
    from flask import Flask, Response, abort, make_response, redirect, render_template, request, url_for

    app = Flask(__name__)
    test_key = state_key(args)
    settings_path = args.config.with_name('clash-dashboard-subscription.json')
    form_token = secrets.token_urlsafe(32)
    settings_lock = Lock()
    alias_pattern = re.compile(r'[\w -]{1,80}')

    def custom_alias_url(value, address):
        if not address or len(value) > 2048:
            return False
        try:
            url = urlsplit(value)
            if url.scheme != 'http' or url.netloc != f'{address}:{args.port}' or url.query or url.fragment:
                return False
        except ValueError:
            return False
        if value != f'http://{url.netloc}{url.path}':
            return False
        path = url.path[len('/subscription/'):]
        return (url.path.startswith('/subscription/')
                and re.fullmatch(r'[A-Za-z0-9_.~-]+(?:/[A-Za-z0-9_.~-]+)*', path)
                and not path.startswith('alias/')
                and not any(part in ('.', '..') for part in path.split('/'))
                and not (path.count('/') == 2 and path.endswith('/config.yaml')))
    logging.getLogger('werkzeug').addFilter(SubscriptionLogFilter())

    def subscription_settings():
        try:
            data = json.loads(settings_path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return dict(enabled=False, token='')
        if (not isinstance(data, dict) or not isinstance(data.get('enabled'), bool)
                or not isinstance(data.get('selected', []), list)
                or not all(isinstance(uid, str) for uid in data.get('selected', []))
                or not isinstance(data.get('token'), str)
                or not re.fullmatch(r'[A-Za-z0-9_-]{32,}', data['token'])):
            raise ValueError('Invalid subscription settings')
        bindings = data.get('bindings', {})
        if not isinstance(bindings, dict) or any(
                not isinstance(entry, dict) or not isinstance(entry.get('uid'), str)
                or not isinstance(entry.get('alias'), str)
                for entry in bindings.values()):
            raise ValueError('Invalid subscription bindings')
        return data

    @app.before_request
    def check_dashboard_host():
        if request.endpoint in ('subscription', 'alias_subscription', 'custom_subscription'):
            return
        if request.remote_addr != '127.0.0.1':
            abort(404)
        if request.host.split(':', 1)[0].lower() not in ('localhost', '127.0.0.1'):
            abort(400)

    def subscription_key(settings, uid):
        return hmac.new(settings['token'].encode('ascii'), uid.encode('utf-8'), hashlib.sha256).hexdigest()

    def subscription_response(uid):
        try:
            matches = [item for item in remote_profiles(args.config) if item['uid'] == uid]
            if not matches:
                abort(404)
            if len(matches) != 1:
                abort(503, 'Subscription profile is ambiguous')
            profile = matches[0]
            raw = read_subscription(args.config, profile)
        except (OSError, RuntimeError, ValueError):
            abort(503, 'Subscription cache unavailable')
        return Response(raw, content_type='text/yaml; charset=utf-8',
                        headers=subscription_headers(profile))

    @app.get('/subscription/<uid>/<key>/config.yaml')
    def subscription(key, uid):
        settings = subscription_settings_or_unavailable()
        expected = subscription_key(settings, uid)
        if not settings['enabled'] or uid not in settings.get('selected', []) or not hmac.compare_digest(
                key.encode('utf-8'), expected.encode('ascii')):
            abort(404)
        source = settings.get('bindings', {}).get(uid, {}).get('uid', uid)
        return subscription_response(source)

    @app.get('/subscription/alias/<alias>/<key>/config.yaml')
    def alias_subscription(alias, key):
        settings = subscription_settings_or_unavailable()
        expected = subscription_key(settings, 'alias:' + alias)
        if not settings['enabled'] or not alias_pattern.fullmatch(alias) or not hmac.compare_digest(
                key.encode('utf-8'), expected.encode('ascii')):
            abort(404)
        matches = [binding for uid, binding in settings.get('bindings', {}).items()
                   if uid in settings.get('selected', []) and binding['alias'] == alias]
        if len(matches) != 1:
            abort(404)
        return subscription_response(matches[0]['uid'])

    @app.get('/subscription/<path:alias_path>')
    def custom_subscription(alias_path):
        settings = subscription_settings_or_unavailable()
        if not settings['enabled']:
            abort(404)
        url = request.host_url + 'subscription/' + alias_path
        matches = [binding for uid, binding in settings.get('bindings', {}).items()
                   if uid in settings.get('selected', []) and binding['alias'] == url]
        if len(matches) != 1 or not custom_alias_url(url, subscription_ip()):
            abort(404)
        return subscription_response(matches[0]['uid'])

    def subscription_settings_or_unavailable():
        try:
            return subscription_settings()
        except (OSError, ValueError):
            abort(503, 'Subscription settings unavailable')

    def check_form_token():
        if not hmac.compare_digest(request.form.get('form_token', '').encode('utf-8'),
                                   form_token.encode('ascii')):
            abort(403)

    def write_subscription_settings(settings):
        temporary = tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                                dir=settings_path.parent, delete=False)
        temporary_path = Path(temporary.name)
        try:
            with temporary:
                json.dump(settings, temporary)
            temporary_path.replace(settings_path)
        finally:
            temporary_path.unlink(missing_ok=True)

    @app.post('/subscription-settings')
    def save_subscription():
        check_form_token()
        enabled = request.form.get('enabled') == '1'
        try:
            with settings_lock:
                settings = subscription_settings()
                try:
                    current_uids = {item['uid'] for item in remote_profiles(args.config)}
                except RuntimeError:
                    current_uids = set()
                current_uids.update(settings.get('selected', []))
                current_uids.update(settings.get('bindings', {}))
                displayed = current_uids.intersection(request.form.getlist('displayed_uid'))
                selected = set(request.form.getlist('selected_uid'))
                selected = (set(settings.get('selected', [])) - displayed) | (displayed & selected)
                settings.update(enabled=enabled, selected=sorted(selected),
                                token=settings['token'] or secrets.token_urlsafe(32))
                write_subscription_settings(settings)
        except (OSError, ValueError):
            return forwarding('无法保存订阅转发设置，请检查 Clash 配置目录的写入权限。'), 500
        return redirect(url_for('forwarding'), code=303)

    @app.post('/subscription-settings/<uid>')
    def update_subscription_link(uid):
        check_form_token()
        alias = request.form.get('alias', '').strip()
        try:
            with settings_lock:
                settings = subscription_settings()
                if not settings['enabled'] or uid not in settings.get('selected', []):
                    abort(404)
                bindings = settings.setdefault('bindings', {})
                binding = bindings.setdefault(uid, {'uid': uid, 'alias': ''})
                if alias and not alias_pattern.fullmatch(alias):
                    address = subscription_ip()
                    current_alias = binding['alias']
                    if address and alias_pattern.fullmatch(current_alias):
                        path = url_for('alias_subscription', alias=current_alias,
                                       key=subscription_key(settings, 'alias:' + current_alias))
                        if alias == f'http://{address}:{args.port}{path}':
                            alias = current_alias
                    if alias != current_alias and not custom_alias_url(alias, address):
                        return forwarding('请输入本机端口下的完整订阅 URL，路径须以 /subscription/ 开头。'), 400
                if alias and any(other_uid != uid and entry['alias'] == alias
                                 for other_uid, entry in bindings.items()):
                    return forwarding('这个别名已在使用，请换一个。'), 409
                binding['alias'] = alias
                write_subscription_settings(settings)
        except (OSError, RuntimeError, ValueError):
            return forwarding('无法保存订阅链接设置。'), 500
        return redirect(url_for('forwarding'), code=303)

    def render_dashboard(view, **context):
        response = make_response(render_template('clash_dashboard.html', view=view, **context))
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get('/forwarding')
    def forwarding(subscription_error=None):
        try:
            profiles = remote_profiles(args.config)
        except RuntimeError:
            profiles = []
        try:
            settings = subscription_settings()
        except (OSError, ValueError):
            settings = dict(enabled=False, token='')
            subscription_error = subscription_error or '无法读取已保存的转发设置。'
        subscription_links = []
        profile_names = {profile['uid']: profile.get('name') or profile['uid'] for profile in profiles}
        bindings = settings.get('bindings', {})
        selected = settings.get('selected', [])
        options = dict(profile_names)
        for uid in dict.fromkeys([*selected, *bindings]):
            if uid not in options:
                options[uid] = bindings.get(uid, {}).get('alias') or f'{uid}（原订阅已移除）'
        address = subscription_ip() if settings['enabled'] else None
        if settings['enabled'] and address is None:
            subscription_error = subscription_error or '找不到可用的公网或局域网 IPv4 地址。'
        if address is not None:
            origin = f'http://{address}:{args.port}'
            for uid, name in options.items():
                if uid not in selected:
                    continue
                binding = bindings.get(uid, {})
                url = origin + url_for('subscription', uid=uid, key=subscription_key(settings, uid))
                alias = binding.get('alias', '')
                alias_url = ''
                if alias_pattern.fullmatch(alias):
                    alias_url = origin + url_for('alias_subscription', alias=alias,
                                                 key=subscription_key(settings, 'alias:' + alias))
                elif custom_alias_url(alias, address):
                    alias_url = alias
                subscription_links.append((uid, name, url, alias, alias_url))
        return render_dashboard(
            'forwarding', options=options, subscription_settings=settings,
            subscription_links=subscription_links, subscription_error=subscription_error,
            form_token=form_token,
        )

    @app.post('/measure')
    def measure_now():
        check_form_token()
        measurement.start()
        return redirect(url_for('dashboard'), code=303)

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

        return render_dashboard(
            'selection', rows=rows, args=args, config_error=config_error,
            db_error=db_error, latest=datetime.fromtimestamp(latest) if latest is not None else None,
            successful=sum(row['delay'] > 0 for row in rows),
            form_token=form_token, measuring=measurement.lock.locked(),
            measurement_message=measurement.message,
        )

    return app


def run_with_tray(app, port, measuring, measurement, tray):
    from werkzeug.serving import make_server

    url = f'http://127.0.0.1:{port}/'
    with make_server('0.0.0.0', port, app, threaded=True) as server:
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            if measuring:
                Thread(target=run_selection, args=(measurement.stop, measurement.lock), daemon=True).start()
            print(f'Clash Dashboard 已启动 · {url}', flush=True)
            tray(url, measurement.start)
        finally:
            measurement.stop.set()
            server.shutdown()
            worker.join()


def run_windows_tray(url, measure_now):
    import pystray
    from PIL import Image

    with Image.open(TRAY_ICON) as asset:
        image = asset.convert('RGBA')

    icon = pystray.Icon(
        'clash_dashboard', image, 'Clash Dashboard',
        pystray.Menu(
            pystray.MenuItem('打开看板', lambda icon, item: webbrowser.open(url), default=True),
            pystray.MenuItem('立即测速', lambda icon, item: measure_now()),
            pystray.MenuItem('退出看板', lambda icon, item: icon.stop()),
        ),
    )
    icon.run()


def ubuntu_tray_bindings():
    try:
        import gi
        gi.require_version('Gtk', '3.0')
        gi.require_version('AyatanaAppIndicator3', '0.1')
        from gi.repository import AyatanaAppIndicator3, Gtk
    except (ImportError, ValueError) as exc:
        raise RuntimeError('Ubuntu 托盘需要 Python 的 GTK 3 和 Ayatana AppIndicator 绑定') from exc
    return AyatanaAppIndicator3, Gtk


def run_ubuntu_tray(url, measure_now):
    AyatanaAppIndicator3, Gtk = ubuntu_tray_bindings()
    indicator = AyatanaAppIndicator3.Indicator.new_with_path(
        'clash-dashboard-tray', TRAY_ICON.stem,
        AyatanaAppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        str(TRAY_ICON.parent),
    )
    indicator.set_title('Clash Dashboard')
    menu = Gtk.Menu()
    open_item = Gtk.MenuItem(label='打开看板')
    open_item.connect('activate', lambda _: Gtk.show_uri_on_window(None, url, 0))
    measure_item = Gtk.MenuItem(label='立即测速')
    measure_item.connect('activate', lambda _: measure_now())
    exit_item = Gtk.MenuItem(label='退出看板')
    exit_item.connect('activate', lambda _: Gtk.main_quit())
    menu.append(open_item)
    menu.append(measure_item)
    menu.append(exit_item)
    menu.show_all()
    indicator.set_menu(menu)
    indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.ACTIVE)
    Gtk.main()


def install_autostart(args):
    script = Path(__file__).resolve()
    command = ['-B', str(script), '--dashboard',
               '--dry-run' if args.dry_run else '--auto-select']
    for name in ('api', 'group', 'url', 'expected', 'interval', 'window', 'timeout',
                 'tolerance', 'jitter_weight', 'failure_penalty', 'retention',
                 'workers', 'port', 'config', 'core_home', 'state'):
        value = getattr(args, name)
        if value is not None:
            command.append(f'--{name.replace("_", "-")}={value}')
    if args.no_tray:
        command.append('--no-tray')

    python = Path(sys.executable).absolute()
    if sys.platform == 'win32':
        install_windows_task(python, script.parent, command)
    elif sys.platform.startswith('linux'):
        if not args.no_tray:
            ubuntu_tray_bindings()
        install_ubuntu_autostart(python, command)
    else:
        raise RuntimeError('--install 支持 Windows 和 Ubuntu 桌面')


def install_windows_task(python, directory, command):
    pythonw = python.with_name('pythonw.exe')
    if not pythonw.is_file():
        raise RuntimeError(f'找不到 Windows 后台 Python：{pythonw}')
    env = dict(os.environ, CLASH_INSTALL_PYTHON=str(pythonw),
               CLASH_INSTALL_ARGS=subprocess.list2cmdline(command),
               CLASH_INSTALL_DIR=str(directory))
    powershell = r"""
$ErrorActionPreference = 'Stop'
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $env:CLASH_INSTALL_PYTHON -Argument $env:CLASH_INSTALL_ARGS -WorkingDirectory $env:CLASH_INSTALL_DIR
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$trigger.Delay = 'PT30S'
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'Clash Dashboard' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
"""
    subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', powershell],
                   env=env, check=True)
    print('已注册计划任务 Clash Dashboard，登录后 30 秒启动')


def desktop_exec_arg(value):
    # Exec quoting is applied before desktop-entry string escaping.
    quoted = re.sub(r'(["\\\x60$])', r'\\\1', value.replace('%', '%%'))
    return '"' + quoted.replace('\\', '\\\\').replace('\n', '\\n').replace(
        '\r', '\\r').replace('\t', '\\t') + '"'


def install_ubuntu_autostart(python, command):
    if os.geteuid() == 0:
        raise RuntimeError('请使用运行 Clash 的普通用户执行 --install')
    config_home = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
    directory = config_home / 'autostart'
    directory.mkdir(parents=True, exist_ok=True)
    exec_command = ' '.join(desktop_exec_arg(value) for value in [str(python), *command])
    content = f"""[Desktop Entry]
Type=Application
Name=Clash Dashboard
Exec={exec_command}
Terminal=false
"""
    desktop_file = directory / 'clash-dashboard.desktop'
    with tempfile.NamedTemporaryFile(dir=directory, delete=False) as temporary:
        temporary.write(content.encode('utf-8'))
    temporary_path = Path(temporary.name)
    try:
        temporary_path.replace(desktop_file)
    finally:
        temporary_path.unlink(missing_ok=True)
    print(f'已写入 {desktop_file}，下次登录 Ubuntu 桌面时启动')


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
    parser.add_argument('--install', action='store_true', help='注册看板和自动选节点自启动')
    parser.add_argument('--dashboard', action='store_true', help='启动 Flask 看板')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true', help='测速并预览切换')
    mode.add_argument('--auto-select', action='store_true', help='随看板启动自动切换')
    parser.add_argument('--port', type=int, default=5050, help='看板端口')
    parser.add_argument('--no-tray', action='store_true', help='只启动网页服务')
    ARGS = parser.parse_args()
    if ARGS.install:
        if ARGS.once:
            parser.error('--install 不能与 --once 同用')
        ARGS.dashboard = True
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
        if ARGS.install:
            ARGS.state = ARGS.state.expanduser().resolve()
            install_autostart(ARGS)
            return 0
        install_verge_script(ARGS.config, ARGS.group, ARGS.url)
        SECRET = controller_secret(ARGS.config)
    except (RuntimeError, OSError, sqlite3.Error, subprocess.CalledProcessError) as exc:
        log(str(exc))
        return 1
    OPENER = build_opener(ProxyHandler({}))
    if ARGS.once:
        return run_selection(Event())

    stop = Event()
    measurement = Measurement(stop)
    try:
        app = create_app(ARGS, measurement)
        if not ARGS.no_tray and sys.platform == 'win32':
            run_with_tray(app, ARGS.port, measuring, measurement, run_windows_tray)
        elif (not ARGS.no_tray and sys.platform.startswith('linux')
              and (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'))):
            run_with_tray(app, ARGS.port, measuring, measurement, run_ubuntu_tray)
        else:
            if measuring:
                Thread(target=run_selection, args=(stop, measurement.lock), daemon=True).start()
            app.run(host='0.0.0.0', port=ARGS.port)
    except ImportError as exc:
        parser.error(f'看板依赖未安装，请运行 python -m pip install Flask pystray Pillow（{exc.name}）')
    except RuntimeError as exc:
        parser.error(str(exc))
    finally:
        stop.set()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log('已停止')
