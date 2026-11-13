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
    - boot_guard.sh(30秒巡检) + crontab(每2分钟) 双保险自动拉起
    - 安卓层 wb_keepalive.sh(root看门狗, 每60秒) 在 chroot 之外兜底

依赖: pip install paho-mqtt   （手机 Kali 里已有）
"""

import json
import os
import socket
import subprocess
import threading
import time

try:
    from urllib.request import urlopen
except ImportError:
    urlopen = None

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
TOPIC_STATUS = "phone6_status_1112"  # 心跳(retain)，电脑据此判断手机在不在线

# broker 直连 IP（外面 WiFi 封 53 端口时的保底；全挂了会走 DoH 自动刷新）
FALLBACK_IPS = ["34.243.217.54", "44.232.241.40"]
HOSTS_FILE = "/etc/hosts"
HEARTBEAT_SEC = 300       # 每5分钟上报一次心跳

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


# ============ DoH 自救（IP 失效时自动刷新） ============
def resolve_via_doh():
    """阿里 DoH 查 broker 最新 IP 并更新 /etc/hosts（绕过被封的 53 端口）"""
    if not urlopen:
        return []
    try:
        r = urlopen("https://223.5.5.5/resolve?name=broker.emqx.io&type=A", timeout=10)
        data = json.loads(r.read().decode())
        ips = [a["data"] for a in data.get("Answer", []) if a.get("type") == 1]
        if ips:
            try:
                lines = [l for l in open(HOSTS_FILE).read().splitlines()
                         if "broker.emqx.io" not in l]
                lines += ["%s broker.emqx.io" % ip for ip in ips[:4]]
                open(HOSTS_FILE, "w").write("\n".join(lines) + "\n")
                log("hosts 已更新为: %s" % ",".join(ips[:4]))
            except Exception:
                pass
        return ips
    except Exception:
        return []


def connect_with_fallback(client):
    """依次试 IP 直连；全失败则 DoH 刷新再试；无限重试直到连上"""
    ips = list(FALLBACK_IPS)
    while True:
        for ip in ips:
            try:
                client.connect(ip, MQTT_PORT, keepalive=30)
                return True
            except Exception as e:
                log("连 %s 失败: %s" % (ip, str(e)[:80]))
        new = resolve_via_doh()
        if new:
            ips = new + FALLBACK_IPS
        time.sleep(20)


# ============ 心跳（电脑判断手机在不在线的依据） ============
def heartbeat_loop(client):
    while True:
        try:
            up = ""
            try:
                up = open("/proc/uptime").read().split()[0]
            except Exception:
                pass
            client.publish(TOPIC_STATUS, json.dumps({
                "host": "oneplus6-kali",
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "uptime_s": up.split(".")[0] if up else "",
                "pid": os.getpid(),
            }), qos=1, retain=True)
        except Exception:
            pass
        time.sleep(HEARTBEAT_SEC)


# ============ MQTT ============
def main():
    acquire_lock()
    state = {"online": False}

    def on_connect(c, u, *args):
        state["online"] = True
        log("已连接 broker, 订阅 %s" % TOPIC_CMD)
        c.subscribe(TOPIC_CMD, qos=1)

    def on_disconnect(c, u, *args):
        # 注意: 参数个数用 *args 兼容 paho 2.x 回调签名(写死参数会 TypeError 崩溃)
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
    log("=== phone6_mqtt_agent 启动 (PID %d) ===" % os.getpid())
    connect_with_fallback(client)   # v2: IP直连+DoH自救，不再依赖域名解析
    threading.Thread(target=heartbeat_loop, args=(client,), daemon=True).start()
    client.loop_forever(retry_first_connection=True)


if __name__ == "__main__":
    main()
