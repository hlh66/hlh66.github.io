#!/bin/sh
# mk_install.sh — MonkeyCode 沙箱 MQTT 反连一键安装
# 用法（在 MonkeyCode 网页终端里粘贴这一行）:
#   curl -fsSL https://hlh66.github.io/tools/mk_install.sh | sh
set -e

# 优先落在 /workspace（MonkeyCode 默认工作目录），没有就用当前目录
if [ -d /workspace ]; then cd /workspace; fi
echo "工作目录: $(pwd)"

echo "[1/4] 下载代理脚本..."
curl -fsSL https://hlh66.github.io/tools/mk_mqtt_agent.py -o mk_mqtt_agent.py \
  || wget -q https://hlh66.github.io/tools/mk_mqtt_agent.py -O mk_mqtt_agent.py

echo "[2/4] 检查/安装 paho-mqtt..."
python3 -c "import paho.mqtt.client" 2>/dev/null \
  || pip3 install --user paho-mqtt 2>/dev/null \
  || pip3 install --user --break-system-packages paho-mqtt 2>/dev/null \
  || pip3 install --user --break-system-packages -i https://pypi.tuna.tsinghua.edu.cn/simple paho-mqtt
python3 -c "import paho.mqtt.client" && echo "paho-mqtt OK"

echo "[3/4] 写入 crontab 自愈（每2分钟拉活）..."
CRON_LINE='*/2 * * * * pgrep -f mk_mqtt_agent >/dev/null || nohup python3 '$(pwd)'/mk_mqtt_agent.py >> '$(pwd)'/mk_mqtt_agent.log 2>&1 &'
(crontab -l 2>/dev/null | grep -v mk_mqtt_agent; echo "$CRON_LINE") | crontab -
echo "crontab 已写入"

echo "[4/4] 启动代理..."
pgrep -f mk_mqtt_agent >/dev/null 2>&1 \
  || nohup python3 mk_mqtt_agent.py >> mk_mqtt_agent.log 2>&1 &
sleep 3
echo "--- 日志尾部 ---"
tail -5 mk_mqtt_agent.log 2>/dev/null || echo "(暂无日志)"
echo "安装完成。回到电脑端跑 mk_mqtt_cmd.py 发条命令测试回执。"
