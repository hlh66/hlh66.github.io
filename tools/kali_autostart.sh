#!/system/bin/sh
# kali_autostart.sh — 开机自动进入 Kali chroot 并拉起全部服务
# 部署位置(安卓层): /data/adb/service.d/kali_autostart.sh (Magisk 开机自动执行)
# 双上下文自适应:
#   A) 安卓层开机执行 → setsid 拉起独立 worker → 挂载伪文件系统 + chroot 拉起 boot_guard
#   B) 在 Kali chroot 里被手动执行 → 直接拉起 boot_guard(无需挂载)
# 日志(安卓层): /data/local/kali_autostart.log
# 日志(chroot): /root/Desktop/kali_autostart.log
# 2026-11-12 由 WorkBuddy 创建

# ---- 上下文B: 已经在 Kali chroot 里(有 /etc/debian_version; 安卓的/etc没有这文件) ----
if [ -f /etc/debian_version ]; then
  echo "===== $(date) 在chroot内被手动执行, 直接拉起 boot_guard =====" >> /root/Desktop/kali_autostart.log
  setsid nohup /root/Desktop/boot_guard.sh >/dev/null 2>&1 </dev/null &
  exit 0
fi

# ---- worker 模式: 实际启动逻辑(独立会话里前台跑) ----
if [ "$1" = "--worker" ]; then
  LOG=/data/local/kali_autostart.log
  ROOT=/data/local/nhsystem/kali-arm64

  # 1. 等安卓开机完成(最多等5分钟) —— getprop 必须绝对路径
  GP=/system/bin/getprop
  i=0
  while [ "$($GP sys.boot_completed 2>/dev/null)" != "1" ] && [ $i -lt 60 ]; do
    sleep 5; i=$((i+1))
  done
  sleep 15   # 再等网络/存储稳定

  echo "===== $(date) kali_autostart worker 开始(安卓层) =====" >> "$LOG"

  # 2. 挂载伪文件系统(照 NetHunter chroot 布局; 已挂的自动跳过)
  mounted() { grep -q " $1 " /proc/mounts; }
  mounted "$ROOT/dev"     || mount --bind /dev     "$ROOT/dev"
  mounted "$ROOT/dev/pts" || mount --bind /dev/pts "$ROOT/dev/pts"
  mounted "$ROOT/proc"    || mount -t proc proc    "$ROOT/proc"
  mounted "$ROOT/sys"     || mount --bind /sys     "$ROOT/sys"
  mounted "$ROOT/sdcard"  || mount --bind /sdcard  "$ROOT/sdcard" 2>/dev/null

  # 3. DNS 保险(外面 WiFi 常封 53 端口, /etc/hosts 已写死 broker.emqx.io 的 IP)
  echo "nameserver 223.5.5.5" > "$ROOT/etc/resolv.conf"

  # 4. 进 chroot 拉起守护全家桶
  #    boot_guard.sh(flock 单例, 重复执行安全)会自动拉起:
  #    sshd / cron / 文件终端5000 / 多端控制台5002 / Hermes飞书网关 /
  #    MQTT代理 / SakuraFrp App(掉线用 am 拉起) / USB相关服务
  chroot "$ROOT" /bin/bash -c "setsid nohup /root/Desktop/boot_guard.sh >/dev/null 2>&1 </dev/null &"

  echo "===== $(date) boot_guard 已拉起, 开机自启完成 =====" >> "$LOG"
  exit 0
fi

# ---- 上下文A: 安卓层入口 —— 用 setsid 生成完全独立会话的 worker(免疫一切挂断) ----
setsid nohup /system/bin/sh "$0" --worker >/dev/null 2>&1 </dev/null &
exit 0
