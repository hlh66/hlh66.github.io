#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
phone6_mqtt_agent.py — 一加6手机(Kali)端 MQTT 命令代理
=======================================================
部署位置: /root/Desktop/phone6_mqtt_agent.py
功能: 订阅公网 broker 的命令 topic，执行 shell 命令，结果发回结果 topic。
      手机放外面/不在局域网时，电脑通过 MQTT 远程操作手机的唯一通道。

Topic（特意避开平板占用的 phone_cmd_001/phone_res_001）:
    下行(电脑->手机): phone6_cmd_1112
    上行(手机->电脑): phone6_res_1112

消息格式（两种都支持，与平板代理保持一致）:
    纯文本:  echo hello
    JSON:    {"id": "abc", "cmd": "echo hello"}

回执 JSON:
    {"id":..., "cmd":..., "stdout":..., "stderr":..., "code":..., "time":..., "host":...}

限制:
    - 单条命令超时 55 秒（要跑长任务请自己 nohup 后台化）
    - 输出截断到 4000 字符
    - 黑名单命令直接拒绝（rm -rf / 之类防手滑）

自愈:
    - 依赖手机 crontab（boot_guard 会保活 cron）:
      */2 * * * * pgrep -f phone6_mqtt_agent >/dev/null || nohup python3 /root/Desktop/phone6_mqtt_agent.py >> /root/Desktop/phone6_mqtt_agent.log 2>&1 &

依赖: pip install paho-mqtt   （手机 Kali 里已有）
"""

import json
import os
import subprocess
import time

try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("缺 paho-mqtt: pip install paho-mqtt")
    raise SystemExit(1)

# ============ 配置 ============
MQTT_HOST = "broker.emqx.io"
MQTT_PORT = 1883
TOPIC_CMD = "phone6_cmd_1112"   # 电脑 -> 手机
TOPIC_RES = "phone6_res_1112"   # 手机 -> 电脑

CMD_TIMEOUT = 55          # 单条命令超时（秒）
OUT_LIMIT = 4000          # 回执输出上限（字符）
LOG_FILE = "/root/Desktop/phone6_mqtt_agent.log"
PID_FILE = "/var/run/phone6_mqtt_agent.pid"

# 黑名单（防手滑/防外人乱来）
BLACKLIST = (
    "rm -rf /", "mkfs", "dd if=", ":(){", "chmod -R 777 /",
    "shutdown", "reboot now", "wipe",
)

# ============ 单实例锁 ============
def acquire_lock():
    try:
        old = open(PID_FILE).read().strip()
        if old and os.path.exists("/proc/%s" % old):
            with open("/proc/%s/cmdline" % old) as f:
                if "phone6_mqtt_agent" in f.read():
                    print("已有实例在跑 PID=%s，退出" % old)
                    raise SystemExit(0)
    except (IOError, OSError):
        pass
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


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
    state = {"online": False}

    def on_connect(c, u, flags, rc, p=None):
        state["online"] = True
        log("已连接 broker, 订阅 %s" % TOPIC_CMD)
        c.subscribe(TOPIC_CMD, qos=1)

    def on_disconnect(c, u, rc, p=None):
        state["online"] = False
        log("与 broker 断开(将自动重连)")

    def on_message(c, u, msg):
        raw = msg.payload.decode("utf-8", "ignore").strip()
        if not raw:
            return
        # 解析：JSON 或纯文本
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
            "host": "oneplus6-kali",
        }
        try:
            c.publish(TOPIC_RES, json.dumps(reply, ensure_ascii=False), qos=1)
        except Exception as e:
            log("回执发送失败: %s" % e)

    cid = "oneplus6_agent_%d" % int(time.time())
    client = mqtt.Client(client_id=cid, clean_session=True)
    try:  # 兼容 paho-mqtt 2.x
        client = mqtt.Client(client_id=cid, clean_session=True,
                             callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    except (AttributeError, TypeError):
        pass

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    # MQTT 心跳 30s + 自动重连(默认 loop_start 内置重连)
    client.reconnect_delay_set(min_delay=5, max_delay=60)
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
    log("=== phone6_mqtt_agent 启动 (PID %d) ===" % os.getpid())
    client.loop_forever(retry_first_connection=True)


if __name__ == "__main__":
    main()
