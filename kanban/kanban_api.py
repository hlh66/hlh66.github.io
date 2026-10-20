#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""看班 · 三端同步 —— 挂在现有 5000 端口上的蓝图

不新开端口：作为 Flask 蓝图挂到 file_terminal_server.py 的 app 上。
    http://<手机IP>:5000/kanban/           教师端（管理）
    http://<手机IP>:5000/kanban/student    学生端（教室大屏 / 一体机）
    http://<手机IP>:5000/kanban/parent     家长端（回家看）

接口（三端共用同一份 state.json）：
    GET  /kanban/api/ping
    GET  /kanban/api/state
    POST /kanban/api/state        {state, base_rev}   乐观锁，只有教师端写
    GET  /kanban/api/health
    GET  /kanban/api/pc/clients   代理多端控制台
    POST /kanban/api/pc/send

挂载方式（file_terminal_server.py 里两行）：
    from kanban_api import init_kanban
    init_kanban(app)
"""
import json
import os
import ssl
import threading
import time
import urllib.request
from datetime import datetime

from flask import Blueprint, jsonify, request, send_from_directory, Response

BASE = os.path.dirname(os.path.abspath(__file__))


def _first(*cands):
    for p in cands:
        if p and os.path.isdir(p):
            return p
    return cands[0]


# 手机上：本文件随 file_terminal_server.py 放在 /root/Desktop/file_terminal/，
# 页面和数据仍在 /root/Desktop/kanban/。本地预览时全部用脚本同目录。
_KANBAN_HOME = '/root/Desktop/kanban'
WEB = _first(os.path.join(_KANBAN_HOME, 'web'), os.path.join(BASE, 'web'))
DOWN = _first(os.path.join(_KANBAN_HOME, 'down'), os.path.join(BASE, 'down'))
DATA = _first(os.path.join(_KANBAN_HOME, 'data'), os.path.join(BASE, 'data'))
BACKUPS = os.path.join(DATA, 'backups')
STATE = os.path.join(DATA, 'state.json')
VER = '1.1.0'

os.makedirs(BACKUPS, exist_ok=True)

bp = Blueprint('kanban', __name__, url_prefix='/kanban', static_folder=None)
_lock = threading.RLock()

CORS = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET,POST,OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Cache-Control': 'no-store',
}


def log(msg):
    print('[kanban %s] %s' % (datetime.now().strftime('%m-%d %H:%M:%S'), msg), flush=True)


# ───────────────────────── 存储 ─────────────────────────
def _blank():
    return {
        'v': 1, 'rev': 0, 'updated': 0,
        'meta': {'className': '', 'teacher': ''},
        'students': [], 'att': {}, 'pts': {}, 'log': [], 'tasks': [],
        'announce': None, 'notes': [], 'cfg': {},
    }


def read_state():
    with _lock:
        if not os.path.exists(STATE):
            return _blank()
        try:
            with open(STATE, 'r', encoding='utf-8') as f:
                d = json.load(f)
            if not isinstance(d, dict) or d.get('v') != 1:
                return _blank()
            d.setdefault('notes', [])
            return d
        except Exception as e:
            log('读 state 失败：%s' % e)
            try:
                os.replace(STATE, STATE + '.bad.%d' % int(time.time()))
            except Exception:
                pass
            return _blank()


def write_state(d):
    """原子写，避免并发读到半截。"""
    with _lock:
        tmp = STATE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False, separators=(',', ':'))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE)


def daily_backup(d):
    p = os.path.join(BACKUPS, datetime.now().strftime('%Y%m%d') + '.json')
    if os.path.exists(p):
        return
    try:
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False)
        for x in sorted(os.listdir(BACKUPS))[:-10]:
            try:
                os.remove(os.path.join(BACKUPS, x))
            except Exception:
                pass
    except Exception:
        pass


# ───────────────────────── 页面 ─────────────────────────
# 教师端放独立路径 /kanban/teacher，根路径给学生端 —— 学生打开 /kanban/ 看不到管理页
@bp.route('/')
@bp.route('/student')
@bp.route('/学生端')
def page_student():
    return send_from_directory(WEB, 'student.html')


@bp.route('/teacher')
@bp.route('/teacher/')
@bp.route('/老师')
def page_teacher():
    return send_from_directory(WEB, 'teacher.html')


@bp.route('/parent')
@bp.route('/家长端')
def page_parent():
    return send_from_directory(WEB, 'parent.html')


# 每个学生一个独立入口（文件夹）：/kanban/s/01/、/kanban/s/01、/kanban/parent/01
# key 可以是学号、姓名（URL 编码）或内部 id；页面自己解析后锁定到这个学生
@bp.route('/s/<key>')
@bp.route('/s/<key>/')
@bp.route('/student/<key>')
@bp.route('/student/<key>/')
@bp.route('/学生/<key>')
@bp.route('/学生/<key>/')
@bp.route('/parent/<key>')
@bp.route('/parent/<key>/')
def page_student_one(key):
    return send_from_directory(WEB, 'parent.html')


@bp.route('/<path:f>')
def page_asset(f):
    if f.startswith('api/'):
        return jsonify(ok=False, error='not found'), 404
    # 只放行学生端/家长端的静态文件；index.html、teacher.html 直连一律 404，
    # 教师端只能走 /kanban/teacher 这个入口（少一个能猜到的口子）
    if f not in ('student.html', 'parent.html'):
        return jsonify(ok=False, error='not found'), 404
    return send_from_directory(WEB, f)


# ── 模块页面文件夹：/kanban/pages/<文件> ──
# 工作台、数学工作台等扩展模块都放 web/pages/ 里，加新模块丢文件即可，不用改代码
# （send_from_directory 自带目录穿越防护，.. 会被拒）
PAGES = os.path.join(WEB, 'pages')


@bp.route('/pages/<path:f>')
def page_module(f):
    if not os.path.isdir(PAGES):
        return jsonify(ok=False, error='not found'), 404
    return send_from_directory(PAGES, f)


# 兼容旧入口：/kanban/workbench → pages/workbench.html
@bp.route('/workbench')
@bp.route('/workbench.html')
def page_workbench_alias():
    return send_from_directory(PAGES, 'workbench.html')


# ── 工具下载（白名单，只放 /kanban/down/ 里的指定文件） ──
_DOWN_WHITELIST = {
    'pc_agent_web.exe': 'application/octet-stream',
    'pc_agent.zip': 'application/zip',
    'pc_agent_使用说明.txt': 'text/plain; charset=utf-8',
    'pc_agent.ini': 'text/plain; charset=utf-8',
}


@bp.route('/down/<path:f>')
def page_download(f):
    if f not in _DOWN_WHITELIST:
        return jsonify(ok=False, error='not found'), 404
    p = os.path.join(DOWN, f)
    if not os.path.isfile(p):
        return jsonify(ok=False, error='not found'), 404
    return send_from_directory(DOWN, f, as_attachment=True)


# ───────────────────────── 接口 ─────────────────────────
@bp.route('/api/ping')
def api_ping():
    s = read_state()
    return jsonify(ok=True, ver=VER, name=s.get('meta', {}).get('className') or '看班',
                   rev=s.get('rev', 0), time=int(time.time()))


@bp.route('/api/state', methods=['GET'])
def api_get():
    s = read_state()
    return jsonify(ok=True, rev=s.get('rev', 0), state=s)


@bp.route('/api/state', methods=['POST'])
def api_set():
    body = request.get_json(silent=True) or {}
    new = body.get('state')
    if not isinstance(new, dict) or new.get('v') != 1:
        return jsonify(ok=False, error='state 格式不对')
    cur = read_state()
    cur_rev = int(cur.get('rev') or 0)
    base_rev = int(body.get('base_rev') or 0)
    if cur_rev > base_rev:
        return jsonify(ok=False, conflict=True, rev=cur_rev, state=cur)
    new['rev'] = max(cur_rev, int(new.get('rev') or 0), base_rev) + 1
    new['updated'] = int(time.time() * 1000)
    write_state(new)
    daily_backup(new)
    return jsonify(ok=True, rev=new['rev'])


@bp.route('/api/health')
def api_health():
    s = read_state()
    return jsonify(ok=True, ver=VER, port=5000, path='/kanban/',
                   students=len(s.get('students') or []),
                   tasks=len(s.get('tasks') or []),
                   rev=s.get('rev'), updated=s.get('updated'),
                   now=int(time.time()))


# ─────────────────── 多端控制台代理 ───────────────────
DEFAULT_API = 'http://127.0.0.1:5002/pc_agent_server.php'
_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE


def _http(url, data=None, timeout=20):
    req = urllib.request.Request(url, method='POST' if data is not None else 'GET')
    if data is not None:
        req.add_header('Content-Type', 'application/json')
        data = json.dumps(data).encode('utf-8')
    with urllib.request.urlopen(req, data=data, timeout=timeout, context=_SSL) as r:
        return json.loads(r.read().decode('utf-8', 'replace'))


def _api_base(explicit=''):
    s = read_state()
    return (explicit or s.get('cfg', {}).get('api') or DEFAULT_API).strip()


@bp.route('/api/pc/clients')
def pc_clients():
    base = _api_base(request.args.get('api', ''))
    try:
        j = _http(base + '?api=clients', timeout=12)
        raw = j.get('clients') or j.get('data') or []
        if isinstance(raw, dict):
            raw = list(raw.values())
        now = int(time.time())
        out = []
        for c in raw:
            seen = c.get('last_seen') or 0
            out.append({'id': c.get('id'),
                        'name': c.get('name') or c.get('hostname') or c.get('id'),
                        'online': (now - seen) < 90, 'last_seen': seen,
                        'ago': now - seen if seen else 99999})
        out.sort(key=lambda x: (not x['online'], x['ago']))
        return jsonify(ok=True, clients=out)
    except Exception as e:
        return jsonify(ok=False, error=str(e), clients=[])


@bp.route('/api/pc/send', methods=['POST'])
def pc_send():
    b = request.get_json(silent=True) or {}
    cid, cmd = b.get('cid'), b.get('cmd')
    if not cid or not cmd:
        return jsonify(ok=False, error='cid & cmd 必填')
    base = _api_base(b.get('api', ''))
    try:
        j = _http(base + '?api=send&wait=1', {'cid': cid, 'cmd': cmd}, timeout=30)
        ok = bool(j.get('ok'))
        log('下发 %s → %s : %s' % (str(cmd)[:60], cid, 'OK' if ok else j.get('error')))
        return jsonify(ok=ok, result=j)
    except Exception as e:
        return jsonify(ok=False, error=str(e))


# ─────────────────── Agent 直连通道 ───────────────────
# 被控端 exe 直连手机服务器（内网 HTTP），不经公网中转；
# 同时也是「Agent 直接控制电脑」的入口 —— 手机 / 网页 / 任何脚本
# 调 /api/agent/send 就能让教室电脑执行命令。
#   exe 侧：pc_agent.ini → [net] srv2 = http://<手机IP>:5000/kanban/api/agent
#   exe 请求格式沿用多端控制台：?api=poll&cid=...（见 /api/agent 分发路由）
AGENT_FILE = os.path.join(DATA, 'agents.json')


def _read_agents():
    try:
        with open(AGENT_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _write_agents(d):
    tmp = AGENT_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, AGENT_FILE)


@bp.route('/api/agent/register', methods=['POST'])
@bp.route('/api/agent', methods=['GET', 'POST'])
def agent_entry():
    # exe 用 ?api=xxx 的形式访问（和公网中转同一套格式），网页端走子路径
    api = request.args.get('api', '')
    if request.path.endswith('/register'):
        api = 'register'
    b = request.get_json(silent=True) or {}

    if api == 'register':
        name = (b.get('name') or 'PC').strip()
        ags = _read_agents()
        cid = None
        for k, v in ags.items():
            if v.get('name') == name:
                cid = k
                break
        if not cid:
            cid = ('a%08x' % (int(time.time() * 1000) & 0xFFFFFFFF))[:10]
        old = ags.get(cid, {})
        ags[cid] = {'name': name, 'last_seen': int(time.time()),
                    'pending': old.get('pending') or [],
                    'results': (old.get('results') or [])[-30:]}
        _write_agents(ags)
        log('Agent 注册: %s -> %s' % (name, cid))
        return jsonify(ok=True, cid=cid)

    cid = request.args.get('cid', '') or b.get('cid') or ''

    if api == 'poll':
        ags = _read_agents()
        if cid not in ags:
            return jsonify(ok=False, reregister=True)
        ag = ags[cid]
        ag['last_seen'] = int(time.time())
        pend = ag.get('pending') or []
        ag['pending'] = []
        _write_agents(ags)
        if pend:
            t = pend[0]
            return jsonify(ok=True, task={'tid': t.get('tid'), 'cmd': t.get('cmd')})
        return jsonify(ok=True)

    if api in ('report', 'result'):   # 公网中转管它叫 result，这里两个名字都认
        ags = _read_agents()
        if cid in ags:
            out = b.get('output') or ''
            if isinstance(out, str) and out.startswith('__IMAGE__:'):
                out = out[:120] + '...[图片结果，直连通道不落盘]'
            ags[cid].setdefault('results', []).append({
                'tid': b.get('tid'), 'ok': bool(b.get('ok')), 'cmd': b.get('cmd'),
                'output': str(out)[:60000], 'ts': int(time.time())})
            ags[cid]['results'] = ags[cid]['results'][-30:]
            ags[cid]['last_seen'] = int(time.time())
            _write_agents(ags)
        return jsonify(ok=True)

    if api == 'clients':
        ags = _read_agents()
        now = int(time.time())
        out = [{'id': k, 'name': v.get('name'),
                'online': (now - v.get('last_seen', 0)) < 90,
                'ago': now - v.get('last_seen', 0)} for k, v in ags.items()]
        out.sort(key=lambda x: (not x['online'], x['ago']))
        return jsonify(ok=True, clients=out)

    if api == 'send':
        cmd = (b.get('cmd') or '').strip()
        if not cid or not cmd:
            return jsonify(ok=False, error='cid & cmd 必填')
        ags = _read_agents()
        if cid not in ags:
            return jsonify(ok=False, error='没有这个 agent，让它先连一次')
        tid = 't%06x' % (int(time.time() * 1000) & 0xFFFFFF)
        ags[cid].setdefault('pending', []).append({'tid': tid, 'cmd': cmd})
        ags[cid]['last_seen'] = int(time.time())
        _write_agents(ags)
        log('Agent 下发 [%s] %s → %s' % (tid, cmd[:60], cid))
        return jsonify(ok=True, tid=tid)

    if api == 'results':
        ags = _read_agents()
        if cid not in ags:
            return jsonify(ok=True, results=[])
        return jsonify(ok=True, results=ags[cid].get('results', [])[-10:])

    return jsonify(ok=False, error='unknown api: %s' % api)


# ─────────────────── 定时任务线程 ───────────────────
_fired = {}


def ticker():
    log('定时任务线程已启动')
    while True:
        try:
            s = read_state()
            now = datetime.now()
            hm = now.strftime('%H:%M')
            wd = (now.weekday() + 1) % 7      # JS 周日=0
            changed = False
            for t in (s.get('tasks') or []):
                if not t.get('on') or t.get('time') != hm:
                    continue
                days = t.get('days') or []
                if days and wd not in days:
                    continue
                key = '%s|%s|%s' % (t.get('id'), now.strftime('%Y%m%d'), hm)
                if key in _fired:
                    continue
                _fired[key] = time.time()
                typ, text = t.get('type'), (t.get('text') or '').strip()
                if typ == 'announce' and text:
                    s['announce'] = {'text': text, 'ts': int(time.time() * 1000)}
                    s['rev'] = int(s.get('rev') or 0) + 1
                    changed = True
                    log('定时通知：%s' % text)
                elif typ == 'open' and text:
                    cid = s.get('cfg', {}).get('cid')
                    if cid:
                        try:
                            _http(_api_base() + '?api=send&wait=1',
                                  {'cid': cid, 'cmd': '!' + text}, timeout=30)
                            log('定时开网页：%s' % text)
                        except Exception as e:
                            log('定时开网页失败：%s' % e)
            if changed:
                write_state(s)
            if len(_fired) > 500:
                cut = time.time() - 7200
                for k in [k for k, v in _fired.items() if v < cut]:
                    _fired.pop(k, None)
        except Exception as e:
            log('ticker 异常：%s' % e)
        time.sleep(20)


_started = False


def init_kanban(app):
    """把三端挂到已有的 Flask app 上（不占新端口）。"""
    global _started
    app.register_blueprint(bp)

    @app.after_request
    def _k_cors(resp):
        if request.path.startswith('/kanban/'):
            for k, v in CORS.items():
                resp.headers[k] = v
        return resp

    @app.route('/kanban/api/<path:_p>', methods=['OPTIONS'])
    def _k_opt(_p):
        return ('', 204)

    if not _started:
        _started = True
        threading.Thread(target=ticker, daemon=True).start()
    print('[kanban] 三端已挂载 → /kanban/ · /kanban/student · /kanban/parent')
