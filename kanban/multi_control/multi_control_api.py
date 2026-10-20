#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多端控制 · 被控端 agent 接口（手机服务器 / 平板服务器 通用同一份文件）
================================================================
用途：教室电脑的被控端 exe（pc_agent_web.exe）直连本接口，
      不依赖公网中转。手机、平板各部署一份，exe 多线路里手动切换。

放在 5000 端口所在服务的同机目录「多端控制」里，幂等挂载：
    import sys; sys.path.insert(0, '/root/Desktop/多端控制')   # 平板是 Termux 的 Desktop
    from multi_control_api import init_multi_control
    init_multi_control(app)

对外协议（与公网中转同一套格式，exe 零改动可连）：
    POST /mctl/api/agent?api=register  {name}              -> {ok, cid}
    GET  /mctl/api/agent?api=poll&cid=                     -> {ok, task:{tid,cmd}}
    POST /mctl/api/agent?api=result  {cid,tid,cmd,ok,output}
    GET  /mctl/api/agent?api=clients                      -> {ok, clients:[...]}
    POST /mctl/api/agent?api=send   {cid,cmd}              -> {ok, tid}
    GET  /mctl/api/agent?api=results&cid=                  -> {ok, results:[...]}
"""
import json
import os
import time
import threading

from flask import Blueprint, jsonify, request, send_from_directory

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, 'data')
DOWN = os.path.join(HERE, 'down')
# 只有列在这里的文件能下载，路径乱猜一律 404
DOWN_OK = ('pc_agent_web.exe', 'pc_agent.zip', '使用说明.txt')
AGENTS = os.path.join(DATA, 'agents.json')
_lock = threading.Lock()

bp = Blueprint('mctl', __name__, url_prefix='/mctl')


def _log(msg):
    print('[mctl %s] %s' % (time.strftime('%m-%d %H:%M:%S'), msg), flush=True)


def _read():
    try:
        with open(AGENTS, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _write(d):
    os.makedirs(DATA, exist_ok=True)
    tmp = AGENTS + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, AGENTS)


@bp.route('/health')
def health():
    with _lock:
        ags = _read()
    now = int(time.time())
    online = sum(1 for v in ags.values() if now - v.get('last_seen', 0) < 90)
    return jsonify(ok=True, service='multi-control', agents=len(ags), online=online)


@bp.route('/api/agent/register', methods=['POST'])
@bp.route('/api/agent', methods=['GET', 'POST'])
def agent_entry():
    api = request.args.get('api', '')
    if request.path.endswith('/register'):
        api = 'register'
    b = request.get_json(silent=True) or {}
    with _lock:
        ags = _read()

        if api == 'register':
            name = (b.get('name') or 'PC').strip()
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
            _write(ags)
            _log('注册: %s -> %s' % (name, cid))
            return jsonify(ok=True, cid=cid)

        cid = request.args.get('cid', '') or b.get('cid') or ''

        if api == 'poll':
            if cid not in ags:
                return jsonify(ok=False, reregister=True)
            ags[cid]['last_seen'] = int(time.time())
            pend = ags[cid].get('pending') or []
            ags[cid]['pending'] = []
            _write(ags)
            if pend:
                t = pend[0]
                return jsonify(ok=True, task={'tid': t.get('tid'), 'cmd': t.get('cmd')})
            return jsonify(ok=True)

        if api in ('report', 'result'):
            if cid in ags:
                out = b.get('output') or ''
                if isinstance(out, str) and out.startswith('__IMAGE__:'):
                    out = out[:120] + '...[图片结果，多端控制通道不落盘]'
                ags[cid].setdefault('results', []).append({
                    'tid': b.get('tid'), 'ok': bool(b.get('ok')), 'cmd': b.get('cmd'),
                    'output': str(out)[:60000], 'ts': int(time.time())})
                ags[cid]['results'] = ags[cid]['results'][-30:]
                ags[cid]['last_seen'] = int(time.time())
                _write(ags)
            return jsonify(ok=True)

        if api == 'clients':
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
            if cid not in ags:
                return jsonify(ok=False, error='没有这个 agent，让它先连一次')
            tid = 't%06x' % (int(time.time() * 1000) & 0xFFFFFF)
            ags[cid].setdefault('pending', []).append({'tid': tid, 'cmd': cmd})
            ags[cid]['last_seen'] = int(time.time())
            _write(ags)
            _log('下发 [%s] %s → %s' % (tid, cmd[:60], cid))
            return jsonify(ok=True, tid=tid)

        if api == 'results':
            if cid not in ags:
                return jsonify(ok=True, results=[])
            return jsonify(ok=True, results=ags[cid].get('results', [])[-10:])

    return jsonify(ok=False, error='unknown api: %s' % api)


# ════════════ 工具下载（exe 就在服务器上，点链接直接下） ════════════
@bp.route('/down/')
def down_index():
    files = []
    for n in DOWN_OK:
        p = os.path.join(DOWN, n)
        if os.path.isfile(p):
            files.append((n, os.path.getsize(p)))
    rows = ''.join(
        '<li><a href="/mctl/down/%s">%s</a> <span style="color:#888">%.1f KB</span></li>'
        % (n, n, sz / 1024.0) for n, sz in files)
    html = (
        '<!doctype html><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>多端控制 · 工具下载</title>'
        '<body style="font-family:system-ui;background:#0f1115;color:#e8ecf1;padding:24px">'
        '<h2>📥 教室电脑被控端</h2>'
        '<p style="color:#9aa7b4">双击 exe 即可运行，首次运行自动生成配置；'
        '默认收进右下角托盘，点托盘图标开窗。</p>'
        '<ul style="line-height:2">%s</ul>'
        '<p style="color:#6b7785;font-size:13px">下载后如被杀毒拦，把所在文件夹加白名单。</p>'
        '</body>' % rows)
    return html


@bp.route('/down/<path:name>')
def down_file(name):
    if name not in DOWN_OK:
        return jsonify(ok=False, error='not found'), 404
    if not os.path.isfile(os.path.join(DOWN, name)):
        return jsonify(ok=False, error='not found'), 404
    return send_from_directory(DOWN, name, as_attachment=True)


_started = False


def init_multi_control(app):
    """挂到已有 Flask app（不占新端口）。"""
    global _started
    if _started:
        return
    app.register_blueprint(bp)
    _started = True
    _log('多端控制已挂载 /mctl（data=%s）' % DATA)
