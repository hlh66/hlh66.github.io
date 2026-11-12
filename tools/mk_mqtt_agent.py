#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mk_mqtt_agent.py — MonkeyCode 沙箱端 MQTT 命令代理
====================================================
部署位置: /workspace/mk_mqtt_agent.py（MonkeyCode 云开发环境内）
背景: MonkeyCode 沙箱入站全封（SSH/端口映射不可用），只能出站连接，
      因此用 MQTT 反连模式实现远程控制（与手机/平板代理同一套路）。

Topic:
    下行(电脑->沙箱): mk_cmd_1112
    上行(沙箱->电脑): mk_res_1112
    broker: broker.emqx.io:1883, qos=1

消息格式（与手机/平板代理一致）:
    纯文本:  echo hello
    JSON:    {"id": "abc", "cmd": "echo hello"}

回执 JSON:
    {"id":..., "cmd":..., "stdout":..., "stderr":..., "code":..., "time":..., "host":"monkeycode-sandbox"}

限制:
    - 单条命令超时 55 秒（长任务自己 nohup 后台化）
    - 输出截断 4000 字符
    - 黑名单防手滑

自愈: crontab 每2分钟拉活（见 mk_install.sh）
依赖: paho-mqtt（脚本启动时自动安装，Debian 12 会自动加 --break-system-packages）
"""

import json
import os
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE, "mk_mqtt_agent.log")
PID_FILE = os.path.join(BASE, "mk_mqtt_agent.pid")

# ============ 配置 ============
MQTT_HOST = "broker.emqx.io"
MQTT_PORT = 1883
TOPIC_CMD = "mk_cmd_1112"   # 电脑 -> 沙箱
TOPIC_RES = "mk_res_1112"   # 沙箱 -> 电脑

CMD_TIMEOUT = 55
OUT_LIMIT = 4000

BLACKLIST = (
    "rm -rf /", "mkfs", "dd if=", ":(){", "chmod -R 777 /",
    "shutdown", "reboot now", "wipe",
)


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ============ paho-mqtt 自动安装 ============
def ensure_paho():
    try:
        import paho.mqtt.client  # noqa
        return True
    except ImportError:
        pass
    log("缺 paho-mqtt，尝试自动安装...")
    cmds = [
        [sys.executable, "-m", "pip", "install", "-q", "paho-mqtt"],
        [sys.executable, "-m", "pip", "install", "-q", "--user", "paho-mqtt"],
        [sys.executable, "-m", "pip", "install", "-q", "--user",
         "--break-system-packages", "paho-mqtt"],
        [sys.executable, "-m", "pip", "install", "-q", "--user",
         "--break-system-packages", "-i",
         "https://pypi.tuna.tsinghua.edu.cn/simple", "paho-mqtt"],
    ]
    for c in cmds:
        try:
            r = subprocess.run(c, capture_output=True, text=True, timeout=180)
            if r.returncode == 0:
                log("paho-mqtt 安装成功: %s" % " ".join(c[-4:]))
                import paho.mqtt.client  # noqa
                return True
        except Exception:
            continue
    log("paho-mqtt 安装失败，退出")
    return False


# ============ 单实例锁 ============
def acquire_lock():
    try:
        old = open(PID_FILE).read().strip()
        if old and os.path.exists("/proc/%s" % old):
            with open("/proc/%s/cmdline" % old) as f:
                if "mk_mqtt_agent" in f.read():
                    print("已有实例在跑 PID=%s，退出" % old)
                    raise SystemExit(0)
    except (IOError, OSError):
        pass  # Windows/非 Linux 环境跳过 /proc 检查
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))


# ============ 命令执行 ============
def run_cmd(cmd):
    for bad in BLACKLIST:
        if bad in cmd:
            return {"stdout": "", "stderr": "已拒绝(黑名单: %s)" % bad, "code": -2}
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True,
                           text=True, timeout=CMD_TIMEOUT)
        out = (r.stdout or "")[-OUT_LIMIT:]
        err = (r.stderr or "")[-OUT_LIMIT:]
        return {"stdout": out, "stderr": err, "code": r.returncode}
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": "命令超时(%ds)" % CMD_TIMEOUT, "code": -1}
    except Exception as e:
        return {"stdout": "", "stderr": str(e)[:500], "code": -3}


# ============ MQTT ============
def main():
    acquire_lock()
    if not ensure_paho():
        raise SystemExit(1)
    import paho.mqtt.client as mqtt

    def on_connect(c, u, flags, rc, p=None):
        log("已连接 broker, 订阅 %s" % TOPIC_CMD)
        c.subscribe(TOPIC_CMD, qos=1)
        # 上线广播，方便电脑端确认存活
        c.publish(TOPIC_RES, json.dumps({
            "id": None, "cmd": "__online__", "stdout": "agent online",
            "stderr": "", "code": 0, "time": time.strftime("%H:%M:%S"),
            "host": "monkeycode-sandbox",
        }, ensure_ascii=False), qos=1)

    def on_disconnect(c, u, rc, p=None):
        log("与 broker 断开(将自动重连)")

    def on_message(c, u, msg):
        raw = msg.payload.decode("utf-8", "ignore").strip()
        if not raw:
            return
        cmd_id, cmd = None, raw
        try:
            j = json.loads(raw)
            if isinstance(j, dict) and "cmd" in j:
                cmd_id = j.get("id")
                cmd = str(j["cmd"])
        except (ValueError, TypeError):
            pass

        log("收到命令: %s" % cmd[:120])
        res = run_cmd(cmd)
        reply = {
            "id": cmd_id, "cmd": cmd[:200],
            "stdout": res["stdout"], "stderr": res["stderr"],
            "code": res["code"], "time": time.strftime("%H:%M:%S"),
            "host": "monkeycode-sandbox",
        }
        try:
            c.publish(TOPIC_RES, json.dumps(reply, ensure_ascii=False), qos=1)
        except Exception as e:
            log("回执发送失败: %s" % e)

    cid = "mk_agent_%d" % int(time.time())
    client = mqtt.Client(client_id=cid, clean_session=True)
    try:  # 兼容 paho-mqtt 2.x
        client = mqtt.Client(client_id=cid, clean_session=True,
                             callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    except (AttributeError, TypeError):
        pass

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=5, max_delay=60)
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
    log("=== mk_mqtt_agent 启动 (PID %d) ===" % os.getpid())
    client.loop_forever(retry_first_connection=True)


if __name__ == "__main__":
    main()
