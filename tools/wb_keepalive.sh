#!/system/bin/sh
# ============================================================
# wb_keepalive.sh — 一加6(Kali chroot) 安卓层 root 保活看门狗 v1
# ============================================================
# 安装位置: /data/adb/service.d/wb_keepalive.sh（Magisk 开机以 root 自动运行）
# 也可手动启动: chroot /proc/1/root /system/bin/sh /data/adb/service.d/wb_keepalive.sh
#
# 为什么需要它：chroot 里的 boot_guard/crontab 自身会被安卓「打盹模式(Doze)」
# 连锅端——息屏后安卓冻结后台进程，守护进程自己也睡着了。这个脚本跑在安卓层，
# 每 60 秒用 root 权限做四件事：
#   1. dumpsys deviceidle disable —— 禁用打盹（息屏冻后台的元凶）
#   2. 确保 chroot 伪文件系统挂载（/proc /sys /dev/pts）
#   3. 确保 MQTT 代理活着（死了立即拉起，不等 crontab 的2分钟）
#   4. 确保 boot_guard.sh 活着（它是全家桶守护，死了也拉起）
#
# 日志: /data/local/wb_keepalive.log（自动只保留最近 200 行）
# ============================================================

ROOTFS=/data/local/nhsystem/kali-arm64
LOG=/data/local/wb_keepalive.log
LOCK=/data/local/wb_keepalive.lock
A=/system/bin

log() {
    echo "[$(date '+%m-%d %H:%M:%S')] $1" >> "$LOG" 2>/dev/null
    tail -200 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
}

# ---- 单实例锁：已有看门狗在跑就退出 ----
if [ -f "$LOCK" ]; then
    oldpid=$(cat "$LOCK" 2>/dev/null)
    if [ -n "$oldpid" ] && [ -d "/proc/$oldpid" ]; then
        exit 0
    fi
fi
echo $$ > "$LOCK"

main_loop() {
    log "=== wb_keepalive 启动 (PID $$) ==="
    while true; do
        # 1. 防打盹（每次循环刷一遍，成本极低）
        $A/dumpsys deviceidle disable >/dev/null 2>&1

        # 2. rootfs 存在才继续
        if [ -d "$ROOTFS/root/Desktop" ]; then
            # 挂载检查（kali_autostart.sh 也会做，这里兜底，幂等）
            if ! grep -q " $ROOTFS/proc " /proc/mounts 2>/dev/null; then
                log "[修复] 挂载 chroot 伪文件系统"
                $A/mount --bind /dev    "$ROOTFS/dev"    2>/dev/null
                $A/mount --bind /proc   "$ROOTFS/proc"   2>/dev/null
                $A/mount --bind /sys    "$ROOTFS/sys"    2>/dev/null
                $A/mount -t devpts devpts "$ROOTFS/dev/pts" 2>/dev/null
            fi

            # 3. MQTT 代理保活
            if ! $A/chroot "$ROOTFS" /bin/sh -c 'pgrep -f phone6_mqtt_agent >/dev/null' 2>/dev/null; then
                log "[拉起] phone6_mqtt_agent"
                $A/chroot "$ROOTFS" /bin/sh -c \
                    'setsid nohup python3 /root/Desktop/phone6_mqtt_agent.py >> /root/Desktop/phone6_mqtt_agent.log 2>&1 < /dev/null &' \
                    >/dev/null 2>&1
            fi

            # 4. boot_guard 保活（全家桶守护）
            if ! $A/chroot "$ROOTFS" /bin/sh -c 'pgrep -f boot_guard.sh >/dev/null' 2>/dev/null; then
                log "[拉起] boot_guard.sh"
                $A/chroot "$ROOTFS" /bin/sh -c \
                    'setsid nohup /root/Desktop/boot_guard.sh >/dev/null 2>&1 < /dev/null &' \
                    >/dev/null 2>&1
            fi
        else
            log "[警告] rootfs 不存在: $ROOTFS"
        fi

        sleep 60
    done
}

# 后台化自己，不让调用方（Magisk/终端）被阻塞
main_loop &
