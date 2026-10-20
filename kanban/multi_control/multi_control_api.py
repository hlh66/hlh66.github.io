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
import socket
import time
import threading

from flask import Blueprint, jsonify, request, send_from_directory

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, 'data')
DOWN = os.path.join(HERE, 'down')
WEB = os.path.join(HERE, 'web')
# 只有列在这里的文件能下载，路径乱猜一律 404
DOWN_OK = ('pc_agent_web.exe', 'pc_agent.zip', '使用说明.txt')
# 管理网页口令（页面下发命令要它；被控端 exe 走 /mctl/api/agent 不需要）
MGMT_KEY = os.environ.get('MCTL_KEY', 'kban2026')
AGENTS = os.path.join(DATA, 'agents.json')
_lock = threading.Lock()

# ══════ MQTT 通道（复制平板 nav_server 的连接方式：paho → broker.emqx.io） ══════
# 被控端 exe 的 [mqtt] 配置：订阅 pcagent/<token>/cmd，结果发回 pcagent/<token>/res
# token 双保险：payload 里也必须带同样的 token，exe 才会执行
MQTT_HOST = os.environ.get('MCTL_MQTT_HOST', 'broker.emqx.io')
MQTT_PORT = int(os.environ.get('MCTL_MQTT_PORT', '1883'))
MQTT_TOKEN = os.environ.get('MCTL_MQTT_TOKEN', '9c53a8a2fe2f8ee45d9190c6c97a88b3')
TOPIC_CMD = 'pcagent/%s/cmd' % MQTT_TOKEN
TOPIC_RES = 'pcagent/%s/res' % MQTT_TOKEN
MQTT_RESULTS = os.path.join(DATA, 'mqtt_results.json')
MQTT_STATE = {'ok': False, 'err': '', 'since': 0, 'res_n': 0, 'pub_n': 0}

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


# ════════════ MQTT 桥（与被控端 exe 的 mqtt_client.c 协议对接） ════════════
def _mqtt_client(cid_prefix):
    """兼容 paho 1.x / 2.x 建客户端。"""
    import paho.mqtt.client as pm
    try:
        return pm.Client(pm.CallbackAPIVersion.VERSION1,
                         client_id='%s_%d' % (cid_prefix, int(time.time() * 1000)))
    except Exception:
        return pm.Client(client_id='%s_%d' % (cid_prefix, int(time.time() * 1000)))


def _mqtt_pub(topic, payload):
    """一次性发布（平板 nav_server 同款方式：connect → publish → disconnect）。"""
    c = _mqtt_client('mctl_pub')
    c.connect(MQTT_HOST, MQTT_PORT, 10)
    info = c.publish(topic, payload, qos=0)
    try:
        info.wait_for_publish(timeout=6)
    except Exception:
        time.sleep(0.4)
    c.disconnect()
    MQTT_STATE['pub_n'] += 1


def _mqtt_results_read():
    try:
        with open(MQTT_RESULTS, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return []


def _mqtt_results_write(rs):
    os.makedirs(DATA, exist_ok=True)
    tmp = MQTT_RESULTS + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(rs[-50:], f, ensure_ascii=False, indent=1)
    os.replace(tmp, MQTT_RESULTS)


def _mqtt_sub_worker():
    """常驻订阅 pcagent/<token>/res，把 exe 的执行结果落盘。"""
    while True:
        try:
            c = _mqtt_client('mctl_sub')

            def on_conn(cl, ud, rc, *a):
                cl.subscribe(TOPIC_RES, qos=0)
                MQTT_STATE['ok'] = True
                MQTT_STATE['since'] = int(time.time())
                MQTT_STATE['err'] = ''
                _log('MQTT 已连接并订阅 %s' % TOPIC_RES)

            def on_msg(cl, ud, m):
                try:
                    d = json.loads(m.payload.decode('utf-8', 'replace'))
                except Exception:
                    return
                if d.get('token') != MQTT_TOKEN:
                    _log('[安全] MQTT 结果 token 不匹配，丢弃')
                    return
                rec = {'tid': d.get('tid'), 'ok': bool(d.get('ok')),
                       'output': str(d.get('output') or '')[:20000],
                       'ts': int(time.time()), 'via': 'mqtt'}
                with _lock:
                    rs = _mqtt_results_read()
                    rs.append(rec)
                    _mqtt_results_write(rs)
                    MQTT_STATE['res_n'] += 1
                _log('MQTT 结果 [%s] ok=%s' % (rec['tid'], rec['ok']))

            def on_disc(cl, ud, rc, *a):
                MQTT_STATE['ok'] = False

            c.on_connect = on_conn
            c.on_message = on_msg
            c.on_disconnect = on_disc
            c.connect(MQTT_HOST, MQTT_PORT, 30)
            c.loop_forever(retry_first_connection=True)
        except Exception as e:
            MQTT_STATE['ok'] = False
            MQTT_STATE['err'] = str(e)[:120]
            time.sleep(8)


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


# ════════════ 管理网页（/mctl/）+ 口令接口（/mctl/admin/*） ════════════
def _key_ok():
    k = request.args.get('key', '') or request.headers.get('X-Mctl-Key', '')
    return k == MGMT_KEY


def _deny():
    return jsonify(ok=False, error='口令不对'), 401


@bp.route('/')
@bp.route('/index.html')
def home():
    if not os.path.isfile(os.path.join(WEB, 'mctl.html')):
        return jsonify(ok=False, error='web/mctl.html 未上传'), 404
    return send_from_directory(WEB, 'mctl.html')


@bp.route('/admin/health')
def admin_health():
    if not _key_ok():
        return _deny()
    with _lock:
        ags = _read()
    now = int(time.time())
    online = sum(1 for v in ags.values() if now - v.get('last_seen', 0) < 90)
    return jsonify(ok=True, service='multi-control', agents=len(ags), online=online,
                   server=request.host, host=socket.gethostname(),
                   key_default=(MGMT_KEY == 'kban2026'),
                   mqtt={'enabled': True, 'connected': MQTT_STATE['ok'],
                         'host': MQTT_HOST, 'err': MQTT_STATE['err']})


@bp.route('/admin/clients')
def admin_clients():
    if not _key_ok():
        return _deny()
    with _lock:
        ags = _read()
    now = int(time.time())
    out = [{'id': k, 'name': v.get('name'),
            'online': (now - v.get('last_seen', 0)) < 90,
            'ago': now - v.get('last_seen', 0)} for k, v in ags.items()]
    out.sort(key=lambda x: (not x['online'], x['ago']))
    return jsonify(ok=True, clients=out)


@bp.route('/admin/send', methods=['POST'])
def admin_send():
    if not _key_ok():
        return _deny()
    b = request.get_json(silent=True) or {}
    cid = (b.get('cid') or '').strip()
    cmd = (b.get('cmd') or '').strip()
    if not cmd:
        return jsonify(ok=False, error='cmd 必填')

    # ── MQTT 通道：广播发给所有在线 exe（token 相同的都会收到并执行）──
    if (b.get('via') or '').strip().lower() == 'mqtt':
        tid = 'm%06x' % (int(time.time() * 1000) & 0xFFFFFF)
        payload = json.dumps({'token': MQTT_TOKEN, 'tid': tid, 'cmd': cmd}, ensure_ascii=False)
        try:
            _mqtt_pub(TOPIC_CMD, payload)
        except Exception as e:
            _log('MQTT 下发失败: %s' % e)
            MQTT_STATE['err'] = str(e)[:120]
            return jsonify(ok=False, error='MQTT 发送失败: %s' % str(e)[:80])
        _log('网页 MQTT 下发 [%s] %s' % (tid, cmd[:60]))
        return jsonify(ok=True, tid=tid, via='mqtt', topic=TOPIC_CMD)

    if not cid:
        return jsonify(ok=False, error='cid 必填（或带 via:"mqtt" 走 MQTT 广播）')
    with _lock:
        ags = _read()
        if cid not in ags:
            return jsonify(ok=False, error='没有这台电脑，让它先连一次')
        tid = 't%06x' % (int(time.time() * 1000) & 0xFFFFFF)
        ags[cid].setdefault('pending', []).append({'tid': tid, 'cmd': cmd})
        ags[cid]['last_seen'] = int(time.time())
        _write(ags)
    _log('网页下发 [%s] %s → %s' % (tid, cmd[:60], cid))
    return jsonify(ok=True, tid=tid)


@bp.route('/admin/results')
def admin_results():
    if not _key_ok():
        return _deny()
    if (request.args.get('via') or '').lower() == 'mqtt':
        with _lock:
            rs = _mqtt_results_read()
        return jsonify(ok=True, results=rs[-10:])
    cid = request.args.get('cid', '')
    with _lock:
        ags = _read()
    if cid not in ags:
        return jsonify(ok=True, results=[])
    return jsonify(ok=True, results=ags[cid].get('results', [])[-10:])


@bp.route('/admin/mqtt')
def admin_mqtt():
    if not _key_ok():
        return _deny()
    return jsonify(ok=True, enabled=True, host=MQTT_HOST, port=MQTT_PORT,
                   connected=MQTT_STATE['ok'], err=MQTT_STATE['err'],
                   topic_cmd=TOPIC_CMD, topic_res=TOPIC_RES,
                   pub_n=MQTT_STATE['pub_n'], res_n=MQTT_STATE['res_n'])


_started = False


def init_multi_control(app):
    """挂到已有 Flask app（不占新端口）。"""
    global _started
    if _started:
        return
    app.register_blueprint(bp)
    _started = True
    _log('多端控制已挂载 /mctl（data=%s）' % DATA)
    # MQTT 结果订阅线程（paho 缺失时降级：HTTP 通道照常，只是没 MQTT）
    try:
        import paho.mqtt.client  # noqa: F401
        threading.Thread(target=_mqtt_sub_worker, daemon=True).start()
    except Exception as e:
        _log('MQTT 未启用（缺 paho-mqtt）: %s' % e)
        MQTT_STATE['err'] = 'no paho: %s' % str(e)[:80]
