#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
看班 · 一键部署到手机服务器（三端合并版）
==========================================
三端共用 5000 端口（不再单开 5001）：
    http://<手机IP>:5000/kanban/teacher    教师端（独立路径，别外传）
    http://<手机IP>:5000/kanban/           学生端（教室大屏，根路径）
    http://<手机IP>:5000/kanban/parent     家长端

做什么：
  1. 上传 kanban_api.py → /root/Desktop/file_terminal/（蓝图，挂进 5000 的 Flask）
  2. 上传 web/*.html    → /root/Desktop/kanban/web/
  3. 幂等挂载到远端 file_terminal_server.py（绝不整文件覆盖！）
  4. 从 boot_guard.sh 里摘掉旧的 5001 kanban 条目
  5. 重启 5000 服务（boot_guard 30 秒内自动拉起，本脚本也会手动拉一次）

用法：
    python 部署.py                # 自动扫局域网找手机
    python 部署.py 192.168.123.51
    python 部署.py --stop         # 停掉旧 5001 服务

凭据默认取「服务器连接指南」：root / Kali2026@Secure! (22)
可用环境变量覆盖：KANBAN_HOST / KANBAN_USER / KANBAN_PASS / KANBAN_SSH_PORT
"""
import base64
import os
import socket
import sys
import time
import concurrent.futures as cf

try:
    import paramiko
except ImportError:
    sys.exit('缺 paramiko：pip install paramiko')

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, 'web')
USER = os.environ.get('KANBAN_USER', 'root')
PASS = os.environ.get('KANBAN_PASS', 'Kali2026@Secure!')
SSH_PORT = int(os.environ.get('KANBAN_SSH_PORT', '22'))
PORT = 5000
R_FT = '/root/Desktop/file_terminal'      # 5000 服务所在
R_KB = '/root/Desktop/kanban'             # 页面与数据所在
R_BG = '/root/Desktop/boot_guard.sh'


def scan(net='192.168.123.'):
    def probe(i):
        ip = net + str(i)
        s = socket.socket(); s.settimeout(0.4)
        try:
            s.connect((ip, SSH_PORT)); return ip
        except Exception:
            return None
        finally:
            s.close()
    with cf.ThreadPoolExecutor(max_workers=160) as ex:
        return [x for x in ex.map(probe, range(1, 255)) if x]


def connect(ip):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(ip, port=SSH_PORT, username=USER, password=PASS,
              timeout=8, banner_timeout=15, auth_timeout=15,
              allow_agent=False, look_for_keys=False)
    return c


def sh(c, cmd, timeout=90):
    _, out, err = c.exec_command(cmd, timeout=timeout)
    return out.read().decode('utf-8', 'replace').strip(), err.read().decode('utf-8', 'replace').strip()


def find_host(explicit=None):
    if explicit:
        return explicit
    if os.environ.get('KANBAN_HOST'):
        return os.environ['KANBAN_HOST']
    print(' 扫描局域网 22 端口…')
    for ip in scan():
        try:
            c = connect(ip)
        except Exception:
            continue
        o, _ = sh(c, 'hostname; [ -d /root/Desktop ] && echo HAS_DESKTOP')
        c.close()
        if 'HAS_DESKTOP' in o:
            print(' 找到手机：%s（%s）' % (ip, o.splitlines()[0]))
            return ip
    return None


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    ip = find_host(args[0] if args else None)
    if not ip:
        sys.exit('✗ 没找到手机。确认手机开着、和电脑同一 Wi-Fi，然后手动传 IP。')

    print('→ 连接 %s …' % ip)
    try:
        c = connect(ip)
    except Exception as e:
        sys.exit('✗ SSH 连不上：%s\n  密码对不对？看 00_服务器连接指南_推荐阅读.md' % e)

    if '--stop' in sys.argv:
        sh(c, 'pkill -f "[k]anban_server.py"')
        print('✓ 已停')
        c.close(); return

    # 1. 上传文件
    sh(c, 'mkdir -p %s/web %s' % (R_KB, R_FT))
    sftp = c.open_sftp()
    uploads = [(os.path.join(HERE, 'kanban_api.py'), R_FT + '/kanban_api.py')]
    for f in ('teacher.html', 'student.html', 'parent.html'):
        uploads.append((os.path.join(WEB_DIR, f), R_KB + '/web/' + f))
    for src, dst in uploads:
        src = os.path.normpath(src)
        if not os.path.exists(src):
            print(' 跳过（本机没有）：%s' % src); continue
        sftp.put(src, dst)
        print(' ↑ %-52s (%.1f KB)' % (dst, os.path.getsize(src) / 1024))
    sftp.close()

    # 1.5 幂等挂载：往远端 file_terminal_server.py 里插入 kanban 蓝图
    #     （不要整文件覆盖！手机上的 file_terminal.py 与本地版本不同步）
    MOUNT = r'''
import io
p = "/root/Desktop/file_terminal/file_terminal_server.py"
try:
    s = io.open(p, encoding="utf-8").read()
except IOError:
    print("no-server-file"); raise SystemExit
if "init_kanban" in s:
    print("mounted")
else:
    blk = (
        "\n# 看班三端（/kanban/teacher 教师端 · /kanban/ 学生端 · /kanban/parent 家长端）\n"
        "try:\n"
        "    from kanban_api import init_kanban\n"
        "    init_kanban(app)\n"
        "except Exception as _kb_err:\n"
        "    print('[file_terminal] 看班三端未挂载：%s' % _kb_err)\n\n"
    )
    i = s.find("if __name__")
    s = s[:i] + blk + s[i:] if i >= 0 else s + blk
    io.open(p, "w", encoding="utf-8").write(s)
    print("patched")
'''
    b64 = base64.b64encode(MOUNT.encode('utf-8')).decode()
    o, e = sh(c, 'python3 -c "import base64;exec(base64.b64decode(\'%s\'))"' % b64)
    print(' 挂载 5000：%s %s' % (o or e, ''))

    # 2. 摘掉 boot_guard 里旧的 5001 条目（幂等）
    PATCH = r'''
p = "/root/Desktop/boot_guard.sh"
s = open(p, encoding="utf-8").read()
lines = s.splitlines(True)
out, skip = [], False
for ln in lines:
    if "kanban_server.py" in ln or ("看班" in ln and "5001" in ln) or ("看班" in ln and "班级小管家" in ln):
        skip = "ensure_proc" not in ln and "\\" not in ln
        continue
    if skip:
        skip = ln.rstrip().endswith("\\\\")
        continue
    out.append(ln)
open(p, "w", encoding="utf-8").write("".join(out))
print("cleaned" if "kanban_server.py" not in "".join(out) else "still-there")
'''
    b64 = base64.b64encode(PATCH.encode('utf-8')).decode()
    o, e = sh(c, 'python3 -c "import base64;exec(base64.b64decode(\'%s\'))"' % b64)
    print(' boot_guard 清理：%s %s' % (o or e, ''))
    o, _ = sh(c, 'bash -n %s && echo SYNTAX_OK' % R_BG)
    print(' boot_guard 语法：%s' % o)
    if 'SYNTAX_OK' not in o:
        print('✗ boot_guard 语法坏了，别重启！先手工检查 %s' % R_BG)
        c.close(); return

    # 3. 停旧 5001、重启 5000（boot_guard 会拉起，这里手动拉更快）
    sh(c, 'pkill -f "[k]anban_server.py" || true')
    sh(c, 'pkill -f "[f]ile_terminal_server.py" || true')
    time.sleep(1.5)
    sh(c, 'cd %s && (setsid nohup python3 file_terminal_server.py >> file_terminal.log 2>&1 &)' % R_FT)
    time.sleep(3)

    # 4. 验证
    ok = True
    for path in ('/kanban/api/ping', '/kanban/', '/kanban/student', '/kanban/parent', '/files'):
        o, _ = sh(c, 'curl -s -o /dev/null -w "%%{http_code}" --max-time 5 http://127.0.0.1:%d%s' % (PORT, path))
        good = o == '200'
        ok = ok and good
        print(' %-18s %s %s' % (path, o, '✓' if good else '✗'))

    if not ok:
        o, _ = sh(c, 'tail -20 %s/file_terminal.log' % R_FT)
        print('✗ 有接口没起来，日志：\n%s' % o)
    else:
        print('\n✓ 三端已合并到 5000 端口：')
        print('  教师端  http://%s:%d/kanban/teacher   （别外传）' % (ip, PORT))
        print('  学生端  http://%s:%d/kanban/          （根路径就是学生端）' % (ip, PORT))
        print('  家长端  http://%s:%d/kanban/parent' % (ip, PORT))
        print('  大屏（旧链接 5001 已失效，用上面的学生端）')
    c.close()


if __name__ == '__main__':
    main()
