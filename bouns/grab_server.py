# -*- coding: utf-8 -*-
"""
秦岭云商自动抢单工具 - 本地图形界面服务 (Python 版)
支持多任务并行抢单: 单账号下管理多个订单任务, 一键并行抢单
启动: python grab_server.py [--port 8787] [--no-browser]
访问: http://127.0.0.1:8787/  (Ctrl+C 停止)
"""
import os
import sys
import io
import re
import json
import time
import uuid
import ssl
import socket
import hmac
import secrets
import ipaddress
import tempfile
import subprocess
import shlex
import threading
import argparse
import webbrowser
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 远程访问: 令牌认证 + HTTPS 自签证书 + 状态加密持久化
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography import x509
from cryptography.x509.oid import NameOID

# 强制 stdout/stderr 使用 UTF-8 输出 (PyInstaller 打包后默认为系统编码 GBK, 导致中文乱码)
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
else:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# 打包模式(PyInstaller --onefile)判断
# sys.frozen 在打包后存在, 未打包时不存在
FROZEN = getattr(sys, 'frozen', False)
# 运行目录: 打包后为 exe 所在目录, 未打包时为脚本所在目录
if FROZEN:
    ROOT = os.path.dirname(sys.executable)
else:
    ROOT = os.path.dirname(os.path.abspath(__file__))

HTML_PATH = os.path.join(ROOT, 'grab_ui.html')
# 子脚本路径: 打包后为同目录 exe, 未打包时为 .py 文件
if FROZEN:
    ORDER_SCRIPT = os.path.join(ROOT, 'grab_order.exe')
    QUERY_SCRIPT = os.path.join(ROOT, 'grab_query.exe')
else:
    ORDER_SCRIPT = os.path.join(ROOT, 'grab_order.py')
    QUERY_SCRIPT = os.path.join(ROOT, 'grab_query.py')
DEFAULT_PORT = 8787

CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0

# 数据目录: 状态/令牌/证书持久化 (创建失败回退用户目录, 兼容 exe 位于只读目录)
try:
    DATA_DIR = os.path.join(ROOT, 'data')
    os.makedirs(DATA_DIR, exist_ok=True)
except Exception:
    DATA_DIR = os.path.expanduser('~/.qinling_grab')
    os.makedirs(DATA_DIR, exist_ok=True)
STATE_PATH = os.path.join(DATA_DIR, 'state.json')
SECRET_KEY_PATH = os.path.join(DATA_DIR, 'secret.key')
TOKEN_PATH = os.path.join(DATA_DIR, 'token.txt')

# 每日令牌轮换: 每天 12:00 重新生成访问令牌。
# 新令牌推送由系统定时任务 grab-token-notify.timer (每天 12:00:30, 经 lark-cli 发飞书) 完成,
# 或用 --notify-cmd 指定自定义推送命令 (stdin 读消息)。
ROTATE_AT_DEFAULT = '12:00'
CERT_PATH = os.path.join(DATA_DIR, 'server.crt')
KEY_PATH = os.path.join(DATA_DIR, 'server.key')
CERT_SANS_PATH = os.path.join(DATA_DIR, 'cert.sans.json')


def log(msg):
    print('[%s] %s' % (time.strftime('%H:%M:%S'), msg), flush=True)


def get_lan_ips():
    """获取本机局域网 IPv4 地址列表 (供手机等设备访问)"""
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(('8.8.8.8', 80))  # 不实际发包, 仅取主出口 IP
            ips.add(s.getsockname()[0])
        except Exception:
            pass
        finally:
            s.close()
    except Exception:
        pass
    try:
        for res in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(res[4][0])
    except Exception:
        pass
    ips.discard('127.0.0.1')
    return sorted(ips)


def _load_or_create_secret_key():
    """读取/生成 Fernet 密钥 (用于加密 state.json 中的密码)"""
    if os.path.exists(SECRET_KEY_PATH):
        try:
            with open(SECRET_KEY_PATH, 'rb') as f:
                key = f.read().strip()
            if key:
                return Fernet(key)
        except Exception:
            pass
    key = Fernet.generate_key()
    try:
        tmp = SECRET_KEY_PATH + '.tmp'
        with open(tmp, 'wb') as f:
            f.write(key)
        try:
            os.chmod(tmp, 0o600)
        except Exception:
            pass
        os.replace(tmp, SECRET_KEY_PATH)
    except Exception:
        pass
    return Fernet(key)


def _atomic_write(path, data, mode=None):
    """原子写文件: tmp + os.replace, 避免写入一半崩溃产生坏文件"""
    tmp = path + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(data)
    if mode:
        try:
            os.chmod(tmp, mode)
        except Exception:
            pass
    os.replace(tmp, path)


def _load_or_create_cert(extra_sans):
    """加载/生成自签名 HTTPS 证书, 返回 (cert_path, key_path)。
    SAN 列表变化或证书临近过期时自动重新生成。"""
    sans = ['localhost', socket.gethostname(), '127.0.0.1']
    sans += get_lan_ips()
    for d in (extra_sans or []):
        if d and d not in sans:
            sans.append(d)
    # 已有证书且 SAN 一致且未临近过期 → 复用 (手机等设备无需重新安装)
    if os.path.exists(CERT_PATH) and os.path.exists(KEY_PATH):
        try:
            saved = None
            if os.path.exists(CERT_SANS_PATH):
                saved = json.load(open(CERT_SANS_PATH, encoding='utf-8'))
            if saved == sans:
                with open(CERT_PATH, 'rb') as f:
                    cert = x509.load_pem_x509_certificate(f.read())
                try:
                    not_after = cert.not_valid_after_utc
                except AttributeError:
                    not_after = cert.not_valid_after  # cryptography < 42 兼容
                if not_after > datetime.now(timezone.utc) + timedelta(days=7):
                    return CERT_PATH, KEY_PATH
        except Exception:
            pass
    log('正在生成自签名 HTTPS 证书 (SAN: %s)...' % ', '.join(sans[:6]) + ('...' if len(sans) > 6 else ''))
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, 'qinling-grab-server'),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, 'QinlingGrab'),
    ])
    san_objs = []
    for s in sans:
        try:
            san_objs.append(x509.IPAddress(ipaddress.ip_address(s)))
        except ValueError:
            san_objs.append(x509.DNSName(s))
    cert = (x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(san_objs), critical=False)
            # ca=True: Android/iOS 可安装为受信任根的前提
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(
                digital_signature=True, content_commitment=False,
                key_encipherment=True, data_encipherment=False,
                key_agreement=False, key_cert_sign=True,
                crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
            .sign(key, hashes.SHA256()))
    _atomic_write(KEY_PATH, key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()), mode=0o600)
    _atomic_write(CERT_PATH, cert.public_bytes(serialization.Encoding.PEM))
    _atomic_write(CERT_SANS_PATH, json.dumps(sans, ensure_ascii=False).encode('utf-8'))
    return CERT_PATH, KEY_PATH


def _load_or_create_token(cli_token):
    """访问令牌: 命令行指定 > data/token.txt > 自动生成并落盘。
    落盘保证重启后令牌不变, 已连接设备不失效。"""
    if cli_token:
        try:
            _atomic_write(TOKEN_PATH, cli_token.strip().encode('utf-8'), mode=0o600)
        except Exception:
            pass
        return cli_token.strip()
    if os.path.exists(TOKEN_PATH):
        try:
            t = open(TOKEN_PATH, encoding='utf-8').read().strip()
            if t:
                return t
        except Exception:
            pass
    t = secrets.token_urlsafe(24)
    try:
        _atomic_write(TOKEN_PATH, t.encode('utf-8'), mode=0o600)
    except Exception:
        pass
    return t


def _parse_rotate_at(text):
    """解析轮换时刻 'HH:MM', 非法返回 None"""
    m = re.match(r'^(\d{1,2}):(\d{2})$', (text or '').strip())
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if hh > 23 or mm > 59:
        return None
    return hh, mm


def _next_rotation_epoch(now, hh, mm):
    """下一次轮换时刻的 epoch 秒: 今天的 HH:MM, 已过则为明天 (按服务器本地时区)"""
    lt = time.localtime(now)
    target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0,
                          lt.tm_wday, lt.tm_yday, -1))
    if target <= now:
        target += 24 * 3600
    return target


def _rotate_token_now(httpd, notify_cmd):
    """立即轮换访问令牌: 生成新令牌 → 落盘 → 生效 → 飞书推送。
    推送失败只记录日志不影响服务, 新令牌始终可从 data/token.txt 获取。"""
    new_token = secrets.token_hex(16)  # 32 位十六进制, 与默认生成长度一致
    _atomic_write(TOKEN_PATH, (new_token + '\n').encode('utf-8'), mode=0o600)
    httpd.auth_token = new_token
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    log('每日令牌已轮换 (%s), 已持久化到 %s' % (ts, TOKEN_PATH))
    if not notify_cmd:
        log('新令牌推送由系统定时任务 grab-token-notify.timer 负责 (每天 12:00:30)')
        return
    msg = ('【抢单控制台】每日访问令牌已更新\n\n'
           '新令牌: %s\n'
           '更新时间: %s\n\n'
           '浏览器会要求重新登录, 输入上面的新令牌即可。\n'
           '(如未收到此消息, 可登录服务器查看 %s)' % (new_token, ts, TOKEN_PATH))
    for attempt in range(3):
        try:
            proc = subprocess.run(notify_cmd, input=msg.encode('utf-8'),
                                  capture_output=True, timeout=90)
            if proc.returncode == 0:
                log('新令牌已通过飞书推送')
                return
            err = (proc.stderr or proc.stdout or b'').decode('utf-8', 'replace')[:200]
            log('飞书推送失败 (rc=%d): %s' % (proc.returncode, err.strip()))
        except Exception as ex:
            log('飞书推送异常: %s' % ex)
        if attempt < 2:
            time.sleep(30)
    log('飞书推送连续失败, 新令牌请从 %s 获取' % TOKEN_PATH)


def _token_rotation_loop(httpd, hh, mm, notify_cmd):
    """每日令牌轮换线程: 每天 HH:MM (服务器本地时间) 重新生成令牌并推送, 持续到进程退出。"""
    while True:
        target = _next_rotation_epoch(time.time(), hh, mm)
        log('下次令牌轮换: %s' % time.strftime('%Y-%m-%d %H:%M:%S',
                                                time.localtime(target)))
        try:
            time.sleep(max(1.0, target + 1.0 - time.time()))
        except Exception:
            return
        if time.time() < target:
            continue  # 系统时钟被调整导致提前唤醒, 重新计算
        try:
            _rotate_token_now(httpd, notify_cmd)
        except Exception as ex:
            log('令牌轮换异常: %s' % ex)


class ProcRunner:
    """子进程运行器: 实时读取 stdout, 支持 on_done 回调"""

    def __init__(self, on_line, on_done=None):
        self.proc = None
        self.lock = threading.Lock()
        self.done = False
        self.exit_code = None
        self._on_line = on_line
        self._on_done = on_done
        self._reader = None

    def start(self, cmd_args, cwd):
        try:
            # 设置 PYTHONUTF8=1 环境变量, 强制子进程使用 UTF-8 输出 (避免打包后 GBK 乱码)
            env = os.environ.copy()
            env['PYTHONUTF8'] = '1'
            self.proc = subprocess.Popen(
                cmd_args, cwd=cwd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
                env=env,
            )
        except Exception as ex:
            return str(ex)
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        return None

    def _read_loop(self):
        try:
            for raw in self.proc.stdout:
                line = raw.decode('utf-8', errors='replace')
                if self._on_line:
                    self._on_line(line)
        except Exception:
            pass
        try:
            code = self.proc.wait()
        except Exception:
            code = 1
        with self.lock:
            self.exit_code = code
            self.done = True
        if self._on_done:
            self._on_done(code)

    @property
    def has_exited(self):
        if self.proc is None:
            return True
        return self.proc.poll() is not None

    def kill(self):
        try:
            if self.proc and self.proc.poll() is None:
                # Windows: 使用 taskkill /F /T 终止整个进程树, 确保子进程也被结束
                if os.name == 'nt':
                    taskkill_path = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'),
                                                 'System32', 'taskkill.exe')
                    if not os.path.isfile(taskkill_path):
                        taskkill_path = 'taskkill'  # 回退到 PATH 查找
                    try:
                        subprocess.run(
                            [taskkill_path, '/F', '/T', '/PID', str(self.proc.pid)],
                            capture_output=True, timeout=5,
                            creationflags=CREATE_NO_WINDOW,
                        )
                    except Exception:
                        try:
                            self.proc.terminate()
                        except Exception:
                            self.proc.kill()
                else:
                    self.proc.kill()
                # 确保进程被终止
                try:
                    self.proc.wait(timeout=2)
                except Exception:
                    pass
        except Exception:
            pass
def _parse_result(log_lines, exit_code, order_code, fleet_name):
    """从任务日志中提取结果摘要"""
    import re
    full = ''.join(log_lines)
    if exit_code == 0:
        # 成功: 提取车辆数和总载重
        m1 = re.search(r'抢到车辆数[:：]\s*(\d+)\s*辆', full)
        m2 = re.search(r'总载重[:：]\s*([\d.]+)\s*吨', full)
        cnt = m1.group(1) if m1 else '?'
        wgt = m2.group(1) if m2 else '?'
        return '[RESULT] 订单 %s / 车队 %s → 抢单成功! 抢到 %s 辆车, 总载重 %s 吨' % (
            order_code, fleet_name, cnt, wgt)
    else:
        # 失败: 提取失败原因
        reasons = []
        m = re.search(r'派车未成功[:：]\s*(.+?)(?:\n|$)', full)
        if m:
            reasons.append(m.group(1).strip())
        if '重试次数用尽' in full:
            reasons.append('重试次数用尽')
        if '未找到订单号' in full:
            m2 = re.search(r'未找到订单号\s*\[([^\]]+)\]', full)
            reasons.append('订单号不存在: ' + (m2.group(1) if m2 else '未知'))
        if '未找到车队' in full:
            m3 = re.search(r'未找到车队\s*\[([^\]]+)\]', full)
            reasons.append('车队不存在: ' + (m3.group(1) if m3 else '未知'))
        if '无可派数量' in full or '已无可派' in full:
            reasons.append('今日已无可派数量')
        if '小于车队任何一辆车' in full:
            reasons.append('剩余量不足, 无法派任何车辆')
        if '登录失败' in full:
            m4 = re.search(r'登录失败[:：]\s*(.+?)(?:\n|$)', full)
            reasons.append('登录失败: ' + (m4.group(1).strip() if m4 else '未知'))
        if '容量校验异常' in full:
            m5 = re.search(r'容量校验异常[:：]\s*(.+?)(?:\n|$)', full)
            reasons.append('容量校验异常: ' + (m5.group(1).strip() if m5 else '未知'))
        if '黑名单校验失败' in full:
            m6 = re.search(r'黑名单校验失败[:：]\s*(.+?)(?:\n|$)', full)
            reasons.append('黑名单校验失败: ' + (m6.group(1).strip() if m6 else '未知'))
        if '无法识别失败原因' in full:
            reasons.append('无法识别失败原因')
        if '解析批量派车页面失败' in full:
            reasons.append('解析批量派车页面失败')
        if '订单详情查询失败' in full:
            reasons.append('订单详情查询失败')
        if '登录已失效' in full:
            reasons.append('登录会话已失效, 请检查账号/验证码后重试')
        if '没有可选车辆' in full:
            reasons.append('车队没有可选车辆')
        if '自动缩减后仍无法满足' in full:
            reasons.append('容量不足, 缩减后仍无法满足')
        if '可派余量' in full and '提交仍失败' in full:
            reasons.append('可派余量不足, 提交失败')
        if not reasons:
            # 兜底: 提取最后一行包含失败/错误/异常的行
            for line in reversed(log_lines):
                if re.search(r'失败|错误|异常|不支持|error', line, re.I):
                    reasons.append(line.strip())
                    break
        reason = '; '.join(reasons) if reasons else '未知原因'
        return '[RESULT] 订单 %s / 车队 %s → 抢单失败! 原因: %s' % (
            order_code, fleet_name, reason)


class GrabServer:
    """多任务管理器: 支持多账号下多个订单任务并行抢单"""

    def __init__(self):
        self.lock = threading.Lock()
        self.tasks = {}             # dict[str, Task]  (所有账号的任务汇总)
        self.accounts = {}          # dict[str, Account]  多账号管理
        self.master_log = []        # 统一日志缓冲 (所有任务汇总)
        self.log_dir = os.path.join(ROOT, 'logs')
        os.makedirs(self.log_dir, exist_ok=True)
        self._log_file = None       # 当天日志文件句柄
        self._log_date = ''         # 当前日志日期
        self._stop_requested = False  # 全局停止标志: stop_all 后阻止后续任务启动
        # ---- 远程访问: 状态持久化 / 演练开关 / 防重复启动 ----
        self.fernet = _load_or_create_secret_key()
        # 演练模式默认关闭 (2026-09-16 起); 已持久化的状态优先, 见 _restore_state
        self.global_dry_run = False
        self._active_accounts = set()   # 正在执行任务的账号 (防双设备重复启动)
        self._login_locks = {}          # account_id -> Lock: 同账号并发登录串行化 (登录一次, 其余复用Cookie)
        self._save_event = threading.Event()
        self._save_thread = threading.Thread(target=self._save_loop, daemon=True)
        self._save_thread.start()
        self._restore_state()

    # ---------- 状态持久化 ----------
    def _mark_dirty(self):
        """标记状态已变化, 通知保存线程去抖合并写盘"""
        self._save_event.set()

    def _save_loop(self):
        """保存线程: 去抖 0.5s 合并多次变更, 单线程串行写盘"""
        while True:
            self._save_event.wait()
            time.sleep(0.5)
            self._save_event.clear()
            self._save_state_now()

    def _enc(self, text):
        """Fernet 加密字符串 → base64 str (空值返回 '')"""
        if not text:
            return ''
        try:
            return self.fernet.encrypt(str(text).encode('utf-8')).decode('ascii')
        except Exception:
            return ''

    def _dec(self, blob):
        """Fernet 解密 → str (失败/空返回 '')"""
        if not blob:
            return ''
        try:
            return self.fernet.decrypt(blob.encode('ascii')).decode('utf-8')
        except Exception:
            return ''

    def _build_snapshot(self):
        """构建持久化快照 (必须在 self.lock 内调用)"""
        return {
            'version': 1,
            'accounts': [{
                'id': a['id'],
                'username': a.get('username', ''),
                'password_enc': self._enc(a.get('password', '')),
                'smsCode_enc': self._enc(a.get('smsCode', '')),
                'cookie': None,  # Cookie 不落盘 (重启后重新登录)
                'cookie_valid': False,
            } for a in self.accounts.values()],
            'tasks': [{
                'id': t['id'],
                'account_id': t.get('account_id', ''),
                'order_code': t['order_code'],
                'fleet_name': t['fleet_name'],
                'date': t['date'],
                'max_vehicle': t['max_vehicle'],
                'dry_run': t['dry_run'],
                'status': t['status'],
                'exit_code': t['exit_code'],
                'result_summary': t.get('result_summary', ''),
            } for t in self.tasks.values()],
            'settings': {'globalDryRun': self.global_dry_run},
        }

    def _save_state_now(self):
        """立即把当前状态原子写入 state.json (快照构建在锁内, 磁盘写在锁外)"""
        try:
            with self.lock:
                snap = self._build_snapshot()
            tmp = STATE_PATH + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(snap, f, ensure_ascii=False, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, STATE_PATH)
        except Exception as ex:
            log('状态保存失败: %s' % str(ex)[:120])

    def _restore_state(self):
        """启动时恢复账号/任务/设置; running 任务恢复为 stopped"""
        if not os.path.exists(STATE_PATH):
            return
        try:
            with open(STATE_PATH, 'r', encoding='utf-8') as f:
                snap = json.load(f)
            if not isinstance(snap, dict) or not isinstance(snap.get('accounts'), list):
                raise ValueError('状态文件格式不正确')
        except Exception as ex:
            corrupt = STATE_PATH + '.corrupt'
            try:
                os.replace(STATE_PATH, corrupt)
            except Exception:
                pass
            log('状态文件损坏(%s), 已备份为 %s, 以空状态启动' % (str(ex)[:60], corrupt))
            return
        with self.lock:
            for a in snap.get('accounts') or []:
                if not a.get('id') or not a.get('username'):
                    continue
                self.accounts[a['id']] = {
                    'id': a['id'],
                    'username': str(a.get('username', '')),
                    'password': self._dec(a.get('password_enc', '')),
                    'smsCode': self._dec(a.get('smsCode_enc', '')),
                    'cookie': None,  # 重启后 Cookie 一律失效, 重新登录
                    'cookie_valid': False,
                }
            for t in snap.get('tasks') or []:
                if not t.get('id'):
                    continue
                status = t.get('status', 'pending')
                if status == 'running':
                    status = 'stopped'
                self.tasks[t['id']] = {
                    'id': t['id'],
                    'account_id': t.get('account_id', ''),
                    'order_code': str(t.get('order_code', '')),
                    'fleet_name': str(t.get('fleet_name', '')),
                    'date': str(t.get('date', '今日')),
                    'max_vehicle': int(t.get('max_vehicle') or 0),
                    'dry_run': bool(t.get('dry_run', True)),
                    'status': status,
                    'exit_code': t.get('exit_code') if status != 'stopped' else None,
                    'log': [],
                    'runner': None,
                    'start_time': None,
                    'end_time': None,
                }
                if status == 'stopped':
                    self.tasks[t['id']]['log'].append('[SERVER] 服务重启, 任务已停止\n')
                if t.get('result_summary') and status != 'stopped':
                    self.tasks[t['id']]['result_summary'] = t['result_summary']
            st = snap.get('settings') or {}
            if isinstance(st.get('globalDryRun'), bool):
                self.global_dry_run = st['globalDryRun']
        if self.accounts or self.tasks:
            log('已恢复状态: %d 个账号, %d 个任务 (running → stopped)' %
                (len(self.accounts), len(self.tasks)))

    def save_state_now(self):
        """对外: 立即落盘 (供关闭时调用)"""
        self._save_event.set()
        try:
            self._save_thread.join(timeout=2)
        except Exception:
            pass
        self._save_state_now()

    # ---------- 账号管理 ----------
    def add_account(self, payload):
        """添加账号, 返回 account_id (按用户名幂等: 已存在则复用并更新凭据,
        防止多设备同时上推产生重复账号)"""
        username = str(payload.get('username', '')).strip()
        with self.lock:
            if username:
                for a in self.accounts.values():
                    if a['username'] == username:
                        new_pw = str(payload.get('password', ''))
                        new_sms = str(payload.get('smsCode', ''))
                        if new_pw:
                            a['password'] = new_pw
                        if new_sms:
                            a['smsCode'] = new_sms
                        if new_pw or new_sms:
                            a['cookie'] = None
                            a['cookie_valid'] = False
                            self._mark_dirty()
                        log('账号已存在, 复用: %s | %s' % (a['id'], username))
                        return a['id']
            account_id = uuid.uuid4().hex[:12]
            account = {
                'id': account_id,
                'username': username,
                'password': str(payload.get('password', '')),
                'smsCode': str(payload.get('smsCode', '')),
                'cookie': None,
                'cookie_valid': False,
            }
            self.accounts[account_id] = account
            self._mark_dirty()
        log('账号已添加: %s | %s' % (account_id, username))
        return account_id

    def remove_account(self, account_id):
        """删除账号及其所有任务"""
        with self.lock:
            # 停止该账号所有运行中任务
            for tid in list(self.tasks.keys()):
                t = self.tasks[tid]
                if t.get('account_id') == account_id:
                    if t['status'] == 'running' and t['runner']:
                        t['runner'].kill()
                    del self.tasks[tid]
            self.accounts.pop(account_id, None)
            self._mark_dirty()
        log('账号已删除: %s' % account_id)

    def login_account(self, account_id):
        """登录单个账号, 返回 (cookie, error)。
        同账号并发任务共享登录: 账号级锁保证只真正登录一次 (2FA验证码只消耗一次),
        等待中的任务拿到缓存Cookie后直接复用 (平台已验证支持同账号多会话并存)。"""
        account = self.accounts.get(account_id)
        if not account:
            return None, '账号不存在'
        if account.get('cookie_valid') and account.get('cookie'):
            log('使用已缓存的Cookie [%s]' % account['username'])
            return account['cookie'], None
        # 同一账号的登录串行化: 先登录者写入缓存, 后到者等待后直接复用
        with self.lock:
            login_lock = self._login_locks.setdefault(account_id, threading.Lock())
        with login_lock:
            account = self.accounts.get(account_id)
            if not account:
                return None, '账号不存在'
            # 双重检查: 等待期间可能已被其他任务登录并缓存
            if account.get('cookie_valid') and account.get('cookie'):
                log('使用已缓存的Cookie [%s] (同账号并发共享)' % account['username'])
                return account['cookie'], None
            log('正在执行登录 [%s] ...' % account['username'])
            try:
                import grab_common as gc
                from grab_common import Session, login as do_login
                sess = Session()
                if not do_login(sess, account['username'], account['password'],
                               account.get('smsCode', ''), interactive=False):
                    return None, '登录失败, 请检查账号密码或验证码'
                cookie = sess.get_cookie_text()
                with self.lock:
                    a = self.accounts.get(account_id)
                    if a:
                        a['cookie'] = cookie
                        a['cookie_valid'] = True
                        self._mark_dirty()
                log('登录成功 [%s], Cookie已缓存' % account['username'])
                return cookie, None
            except Exception as ex:
                return None, '登录异常: ' + str(ex)

    def clear_account_cookie(self, account_id):
        """清除账号的Cookie缓存 (登录失效时)"""
        with self.lock:
            a = self.accounts.get(account_id)
            if a:
                a['cookie'] = None
                a['cookie_valid'] = False
                self._mark_dirty()

    def update_creds(self, account_id, payload):
        """更新账号凭据 (仅非空字段, 防止空值覆盖; 凭据变化时清 Cookie 缓存), 返回 error 或 None"""
        with self.lock:
            a = self.accounts.get(account_id)
            if not a:
                return '账号不存在'
            username = str(payload.get('username', '')).strip()
            password = str(payload.get('password', ''))
            changed = False
            if username and username != a['username']:
                a['username'] = username
                changed = True
            if password and password != a.get('password', ''):
                a['password'] = password
                changed = True
            if 'smsCode' in payload:
                sms_code = str(payload.get('smsCode', ''))
                if sms_code != a.get('smsCode', ''):
                    a['smsCode'] = sms_code
                    changed = True
            if changed:
                a['cookie'] = None
                a['cookie_valid'] = False
                self._mark_dirty()
        return None

    # ---------- 任务管理 ----------
    def add_task(self, payload):
        """添加新任务, 返回 task_id"""
        task_id = uuid.uuid4().hex[:12]
        account_id = str(payload.get('accountId', ''))
        task = {
            'id': task_id,
            'account_id': account_id,
            'order_code': str(payload.get('orderCode', '')),
            'fleet_name': str(payload.get('fleetName', '')),
            'date': str(payload.get('date', '今日')),
            'max_vehicle': int(payload.get('maxVehicle') or 0),
            'dry_run': bool(payload.get('dryRun', True)),
            'status': 'pending',
            'exit_code': None,
            'log': [],
            'runner': None,
            'start_time': None,
            'end_time': None,
        }
        with self.lock:
            self.tasks[task_id] = task
            self._mark_dirty()
        log('任务已添加: %s | %s | %s' % (task_id, task['order_code'], task['fleet_name']))
        return task_id

    def remove_task(self, task_id):
        with self.lock:
            task = self.tasks.get(task_id)
            if task and task['status'] == 'running' and task['runner']:
                task['runner'].kill()
            self.tasks.pop(task_id, None)
            self._mark_dirty()
        log('任务已删除: %s' % task_id)

    # ---------- 单账号启停 ----------
    def start_account(self, account_id):
        """登录并启动单个账号的所有 pending 任务, 返回 error 或 None"""
        account = self.accounts.get(account_id)
        if not account:
            return '账号不存在'
        with self.lock:
            if account_id in self._active_accounts:
                return '该账号已有任务在运行, 请勿重复启动'
            pending = [t for t in self.tasks.values()
                       if t.get('account_id') == account_id and t['status'] == 'pending']
            # 调试: 打印所有任务信息(含日期和订单号)
            all_task_ids = [(t.get('order_code', '?')[:20], t.get('date', '?'), t.get('status', '?'))
                          for t in self.tasks.values()]
        # 排序: 今日 → 明日 → 后日 (不同订单号也按日期优先级)
        date_rank = {'今日': 0, '明日': 1, '后日': 2}
        pending.sort(key=lambda t: date_rank.get(t.get('date', '今日'), 0))
        # 打印排序后的任务顺序
        sorted_info = [(t.get('order_code', '?')[:20], t.get('date', '?')) for t in pending]
        log('start_account: aid=%s user=%s total=%d pending=%d sorted=%s all=%s' %
            (account_id, account.get('username', '?'), len(self.tasks), len(pending), sorted_info, all_task_ids))
        if not pending:
            return '账号 [%s] 没有待执行的任务' % account['username']
        log('启动账号 [%s] 的 %d 个任务 (并行执行, 同账号共享登录)...' % (account['username'], len(pending)))
        # 同账号任务并行执行: 共享一次登录(账号级锁+Cookie复用), 同订单同车队多任务自动拆分车辆份额
        with self.lock:
            self._active_accounts.add(account_id)
            self._stop_requested = False  # 新一轮启动时重置全局停止标志, 避免上次停止全部残留
        t = threading.Thread(target=self._run_sequential, args=(account_id, pending,), daemon=True)
        t.start()
        return None

    def _run_sequential(self, account_id, tasks):
        """并行执行同一账号的多个任务 (全部结束后清除运行标记)。
        同账号并发任务共享一次登录 (账号级锁 + Cookie复用); 同账号同订单同车队同日期
        的 N 个任务自动把车队车辆均分为 N 份互不重叠的份额, 各任务只抢自己的份额。
        """
        remaining = {'n': len(tasks)}

        # 车辆份额拆分: 同(订单, 车队, 日期)的 N 个任务 → 第 i 个任务拿第 i/N 份
        # (子进程按下标取模交错选取, 大车小车在各份额间分布更均匀)
        shares = {}  # task_id -> (share_index, share_total)
        groups = {}
        for task in tasks:
            key = (task.get('order_code', ''), task.get('fleet_name', ''), task.get('date', ''))
            groups.setdefault(key, []).append(task)
        for group in groups.values():
            total = len(group)
            for idx, task in enumerate(group):
                shares[task['id']] = (idx, total)

        def _one_done():
            remaining['n'] -= 1
            if remaining['n'] <= 0:
                with self.lock:
                    self._active_accounts.discard(account_id)
                log('账号 [%s] 本轮任务全部结束' %
                    self.accounts.get(account_id, {}).get('username', account_id))

        for task in tasks:
            share_index, share_total = shares[task['id']]
            t = threading.Thread(target=self._run_one_task,
                                 args=(account_id, task, _one_done, share_index, share_total),
                                 daemon=True)
            t.start()

    def _run_one_task(self, account_id, task, done_cb, share_index=0, share_total=1):
        """执行单个任务: 登录 → 启动子进程。不等待任务完成, 日志与结果由回调异步回传"""
        task_id = task['id']
        prefix = '[%s/%s] ' % (task['order_code'], task['fleet_name'])
        fired = {'done': False}

        def _done_once():
            if not fired['done']:
                fired['done'] = True
                done_cb()

        def _mark_stopped(reason):
            with self.lock:
                t = self.tasks.get(task_id)
                if t and t['status'] == 'pending':
                    t['status'] = 'stopped'
                    t['log'].append('[SERVER] %s\n' % reason)
                    self._mark_dirty()
            self._append_master(prefix + '[SERVER] %s\n' % reason)

        try:
            # 检查全局停止标志: stop_all 后不再启动
            if self._stop_requested:
                _mark_stopped('任务因停止全部而跳过')
                _done_once()
                return
            cookie, err = self.login_account(account_id)  # 账号级锁 + Cookie缓存: 同账号多任务共享一次登录
            if err:
                with self.lock:
                    t = self.tasks.get(task_id)
                    if t:
                        t['status'] = 'failed'
                        t['log'].append('[SERVER] 登录失败: %s\n' % err)
                        self._mark_dirty()
                self._append_master(prefix + '[SERVER] 登录失败: %s\n' % err)
                _done_once()
                return
            # 登录期间可能收到停止全部指令, 登录成功也不再启动
            if self._stop_requested:
                _mark_stopped('任务因停止全部而跳过')
                _done_once()
                return
            if not self._start_one(task, cookie, _done_once, share_index, share_total):
                _done_once()  # 子进程启动失败, on_done 不会触发, 手动记账
        except Exception as ex:
            with self.lock:
                t = self.tasks.get(task_id)
                if t and t['status'] == 'pending':
                    t['status'] = 'failed'
                    t['log'].append('[SERVER] 执行异常: %s\n' % ex)
                    self._mark_dirty()
            self._append_master(prefix + '[SERVER] 执行异常: %s\n' % ex)
            _done_once()

    def stop_account(self, account_id):
        """停止单个账号的所有运行中任务"""
        with self.lock:
            for t in self.tasks.values():
                if t.get('account_id') == account_id and t['status'] == 'running' and t['runner']:
                    t['runner'].kill()
                    t['status'] = 'stopped'
                    t['log'].append('[SERVER] 任务已被手动停止\n')
            self._mark_dirty()
        log('已停止账号 [%s] 的所有任务' % self.accounts.get(account_id, {}).get('username', account_id))

    # ---------- 全局操作 ----------
    def _get_log_path(self, date_str=None):
        if date_str is None:
            date_str = time.strftime('%Y-%m-%d')
        return os.path.join(self.log_dir, date_str + '.log')

    def _append_master(self, line):
        """追加一行到统一日志(内存+文件)"""
        today = time.strftime('%Y-%m-%d')
        with self.lock:
            # 写入内存缓冲
            self.master_log.append(line)
            # 滚动日志文件 (跨天切换)
            if self._log_date != today:
                if self._log_file:
                    self._log_file.close()
                self._log_date = today
                self._log_file = open(self._get_log_path(today), 'a', encoding='utf-8')
            # 写入文件
            if self._log_file:
                self._log_file.write(line)
                self._log_file.flush()
            # 清理10天前的日志文件
            self._cleanup_old_logs()

    def _cleanup_old_logs(self):
        """删除10天前的日志文件"""
        try:
            import glob
            cutoff = time.time() - 10 * 86400
            for f in glob.glob(os.path.join(self.log_dir, '*.log')):
                if os.path.getmtime(f) < cutoff:
                    os.remove(f)
        except Exception:
            pass

    def get_master_log(self, since):
        """获取统一日志增量, 返回 (text, total)"""
        with self.lock:
            text = ''.join(self.master_log)
            total = len(text)
            new_text = text[since:total] if since < total else ''
            return new_text, total

    def get_log_dates(self):
        """获取可用的日志日期列表 (最近10天)"""
        dates = []
        try:
            import glob
            for f in sorted(glob.glob(os.path.join(self.log_dir, '*.log')), reverse=True):
                name = os.path.basename(f).replace('.log', '')
                dates.append(name)
        except Exception:
            pass
        return dates

    def get_log_by_date(self, date_str):
        """读取指定日期的日志内容"""
        path = self._get_log_path(date_str)
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                return f.read()
        return ''

    def start_all(self):
        """遍历所有账号, 各自登录后并行启动任务"""
        if not self.accounts:
            return '请先添加账号'
        self._stop_requested = False  # 重置停止标志
        errors = []
        started = 0
        for account_id in list(self.accounts.keys()):
            err = self.start_account(account_id)
            if err:
                errors.append(err)
            else:
                started += 1
        if started > 0:
            return None  # 有账号成功启动, 不报错
        if errors and all('没有待执行的任务' in e for e in errors):
            return '所有账号都没有待执行的任务'
        if errors:
            return '; '.join(errors)
        return None

    def _start_one(self, task, cookie, done_cb=None, share_index=0, share_total=1):
        """启动单个任务子进程, 返回 True=已启动 False=启动失败; done_cb 在任务结束时调用
        share_index/share_total: 车辆份额拆分 (同账号同订单同车队多任务时各占 1/N)"""
        task_id = task['id']
        prefix = '[%s/%s] ' % (task['order_code'], task['fleet_name'])

        def on_line(line):
            with self.lock:
                t = self.tasks.get(task_id)
                if t:
                    t['log'].append(line)
            # 会话失效自愈: 共享Cookie过期时清除缓存, 后续任务重新登录 (避免坏Cookie连环失败)
            if '登录已失效' in line:
                self.clear_account_cookie(task['account_id'])
                log('检测到会话失效, 已清除账号 [%s] 的Cookie缓存, 后续任务将重新登录' %
                    self.accounts.get(task['account_id'], {}).get('username', '?'))
            # 追加到统一日志 (带任务前缀)
            self._append_master(prefix + line)

        def on_done(exit_code):
            with self.lock:
                t = self.tasks.get(task_id)
                if t:
                    t['exit_code'] = exit_code
                    t['end_time'] = time.time()
                    if t['status'] == 'running':
                        t['status'] = 'success' if exit_code == 0 else 'failed'
                    t['runner'] = None
                    summary = _parse_result(t['log'], exit_code, t['order_code'], t['fleet_name'])
                    t['result_summary'] = summary
                    t['log'].append(summary + '\n')
                    self._mark_dirty()
            # 结果写入统一日志 (锁外操作)
            self._append_master(prefix + summary + '\n')
            log('任务完成: %s exit=%s' % (task_id, exit_code))
            if done_cb:
                done_cb()

        runner = ProcRunner(on_line=on_line, on_done=on_done)
        cmd = self._build_grab_cmd_with_cookie(task, cookie, share_index, share_total)
        err = runner.start(cmd, ROOT)
        with self.lock:
            t = self.tasks.get(task_id)
            if err:
                if t:
                    t['log'].append('[SERVER] 启动失败: %s\n' % err)
                    t['status'] = 'failed'
            if t and not err:
                t['runner'] = runner
                t['status'] = 'running'
                t['start_time'] = time.time()
                if share_total > 1:
                    start_msg = '[SERVER] 任务已启动 (订单=%s, 车队=%s, 车辆份额=%d/%d)\n' % (
                        t['order_code'], t['fleet_name'], share_index + 1, share_total)
                else:
                    start_msg = '[SERVER] 任务已启动 (订单=%s, 车队=%s)\n' % (t['order_code'], t['fleet_name'])
                t['log'].append(start_msg)
            if t:
                self._mark_dirty()
        # 写入统一日志 (锁外操作, 避免死锁)
        if err:
            self._append_master(prefix + '[SERVER] 启动失败: %s\n' % err)
        else:
            self._append_master(prefix + start_msg)
        return err is None

    def _build_grab_cmd_with_cookie(self, task, cookie, share_index=0, share_total=1):
        if FROZEN:
            # 打包后: 直接调用 grab_order.exe
            cmd = [ORDER_SCRIPT,
                   '--cookie', cookie,
                   '--order-code', task['order_code'],
                   '--fleet-name', task['fleet_name'],
                   '--date', task['date'],
                   '--poll-interval', '1',
                   '--poll-timeout', '60']
        else:
            # 未打包: python + grab_order.py
            py = sys.executable
            cmd = [py, '-X', 'utf8', ORDER_SCRIPT,
                   '--cookie', cookie,
                   '--order-code', task['order_code'],
                   '--fleet-name', task['fleet_name'],
                   '--date', task['date'],
                   '--poll-interval', '1',
                   '--poll-timeout', '60']
        if task['max_vehicle'] > 0:
            cmd += ['--max-vehicle', str(task['max_vehicle'])]
        # 演练模式以全局开关为准: 关闭时一律真实抢单 (任务创建时的 dry_run 标记仅在全局开启时生效)
        use_dry = bool(task['dry_run']) and self.global_dry_run
        if use_dry:
            cmd += ['--dry-run']
        else:
            cmd += ['--no-dry-run']
        if share_total > 1:
            cmd += ['--share-index', str(share_index),
                    '--share-total', str(share_total)]
        return cmd

    def stop_all(self):
        self._stop_requested = True  # 设置全局停止标志, 阻止后续任务启动
        stopped_msgs = []
        with self.lock:
            for task in self.tasks.values():
                if task['status'] == 'running' and task['runner']:
                    task['runner'].kill()
                    task['status'] = 'stopped'
                    task['log'].append('[SERVER] 任务已被手动停止\n')
                    prefix = '[%s/%s] ' % (task['order_code'], task['fleet_name'])
                    stopped_msgs.append(prefix + '[SERVER] 任务已被手动停止\n')
            self._mark_dirty()
        # 写入统一日志 (锁外操作)
        for msg in stopped_msgs:
            self._append_master(msg)
        self._append_master('[SERVER] ===== 所有任务已停止 =====\n')
        log('已停止所有运行中的任务')

    def stop_task(self, task_id):
        msg = None
        with self.lock:
            task = self.tasks.get(task_id)
            if task and task['status'] == 'running' and task['runner']:
                task['runner'].kill()
                task['status'] = 'stopped'
                task['log'].append('[SERVER] 任务已被手动停止\n')
                prefix = '[%s/%s] ' % (task['order_code'], task['fleet_name'])
                msg = prefix + '[SERVER] 任务已被手动停止\n'
                self._mark_dirty()
        if msg:
            self._append_master(msg)
        log('已停止任务: %s' % task_id)

    def reset_task(self, task_id):
        """重置任务为待执行 (重跑), 返回 error 或 None"""
        with self.lock:
            t = self.tasks.get(task_id)
            if not t:
                return '任务不存在'
            if t['status'] == 'running':
                return '任务正在运行, 无法重置'
            t['status'] = 'pending'
            t['exit_code'] = None
            t.pop('result_summary', None)
            t['start_time'] = None
            t['end_time'] = None
            t['log'].append('[SERVER] 任务已重置, 等待重新执行\n')
            self._mark_dirty()
        log('任务已重置: %s' % task_id)
        return None

    def reset_account_tasks(self, account_id):
        """重置某账号全部任务状态为待执行 (账号级重跑), 返回 (error, count)"""
        with self.lock:
            tasks = [t for t in self.tasks.values()
                     if str(t.get('account_id')) == str(account_id)]
            if not tasks:
                return '该账号下没有任务', 0
            if any(t['status'] == 'running' for t in tasks):
                return '该账号有任务正在运行, 请先停止后再重置', 0
            n = 0
            for t in tasks:
                if t['status'] == 'pending':
                    continue
                t['status'] = 'pending'
                t['exit_code'] = None
                t.pop('result_summary', None)
                t['start_time'] = None
                t['end_time'] = None
                t['log'].append('[SERVER] 任务已重置, 等待重新执行\n')
                n += 1
            if n:
                self._mark_dirty()
        if n:
            log('账号 [%s] 已重置 %d 个任务' %
                (self.accounts.get(account_id, {}).get('username', account_id), n))
        return None, n

    def get_global_dry_run(self):
        with self.lock:
            return self.global_dry_run

    def set_global_dry_run(self, value):
        with self.lock:
            self.global_dry_run = bool(value)
            self._mark_dirty()

    def get_task_log(self, task_id, since):
        with self.lock:
            task = self.tasks.get(task_id)
            if not task:
                return '', 0, 'pending', None
            text = ''.join(task['log'])
            total = len(text)
            new_text = text[since:total] if since < total else ''
            return new_text, total, task['status'], task['exit_code']

    def get_all_tasks_status(self):
        with self.lock:
            result = []
            for task in self.tasks.values():
                result.append({
                    'id': task['id'],
                    'accountId': task.get('account_id', ''),
                    'orderCode': task['order_code'],
                    'fleetName': task['fleet_name'],
                    'date': task['date'],
                    'maxVehicle': task['max_vehicle'],
                    'dryRun': task['dry_run'],
                    'status': task['status'],
                    'exitCode': task['exit_code'],
                    'resultSummary': task.get('result_summary', ''),
                })
            return result

    def get_accounts_status(self):
        """获取所有账号及其任务摘要"""
        with self.lock:
            result = []
            for account in self.accounts.values():
                tasks = [t for t in self.tasks.values()
                        if t.get('account_id') == account['id']]
                result.append({
                    'id': account['id'],
                    'username': account['username'],
                    'hasCookie': bool(account.get('cookie_valid')),
                    # 不回传密码本身 (安全), 前端据此判断是否需要在本机补录
                    'passwordSet': bool(account.get('password')),
                    'tasks': [{
                        'id': t['id'],
                        'accountId': t.get('account_id', ''),
                        'orderCode': t['order_code'],
                        'fleetName': t['fleet_name'],
                        'date': t['date'],
                        'maxVehicle': t['max_vehicle'],
                        'dryRun': t['dry_run'],
                        'status': t['status'],
                        'exitCode': t['exit_code'],
                        'resultSummary': t.get('result_summary', ''),
                    } for t in tasks],
                })
            return result

    def run_account_query(self, account_id, action, fleet_id=None, with_quota=False):
        """使用指定账号的凭证执行查询"""
        account = self.accounts.get(account_id)
        if not account:
            return None, '账号不存在'
        payload = {
            'username': account['username'],
            'password': account['password'],
            'smsCode': account.get('smsCode', ''),
        }
        return self.run_query(payload, action, fleet_id=fleet_id, with_quota=with_quota)

    def run_query(self, payload, action, fleet_id=None, with_quota=False):
        with self.lock:
            running = any(t['status'] == 'running' for t in self.tasks.values())
        if running:
            return None, '有任务正在运行, 请稍后再试'
        if FROZEN:
            # 打包后: 直接调用 grab_query.exe
            cmd = [QUERY_SCRIPT,
                   '--username', str(payload.get('username', '')),
                   '--password', str(payload.get('password', '')),
                   '--action', action]
        else:
            # 未打包: python + grab_query.py
            py = sys.executable
            cmd = [py, '-X', 'utf8', QUERY_SCRIPT,
                   '--username', str(payload.get('username', '')),
                   '--password', str(payload.get('password', '')),
                   '--action', action]
        if payload.get('smsCode'):
            cmd += ['--sms-code', str(payload['smsCode'])]
        if action == 'Orders' and with_quota:
            cmd += ['--with-quota']
        if action == 'FleetVehicles' and fleet_id:
            cmd += ['--fleet-id', str(fleet_id)]
        out_file = os.path.join(tempfile.gettempdir(),
                                'grab_q_' + uuid.uuid4().hex + '.json')
        cmd += ['--out-file', out_file]
        try:
            # 设置 PYTHONUTF8=1 环境变量, 强制子进程使用 UTF-8 输出
            q_env = os.environ.copy()
            q_env['PYTHONUTF8'] = '1'
            proc = subprocess.Popen(
                cmd, cwd=ROOT,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
                env=q_env,
            )
            out, _ = proc.communicate(timeout=180)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
            return None, '查询超时(180s)'
        except Exception as ex:
            return None, ('启动查询失败: ' + str(ex))
        if not os.path.exists(out_file):
            return None, '查询进程异常退出, 请查看运行日志'
        try:
            with open(out_file, 'rb') as f:
                raw_bytes = f.read()
        except Exception as ex:
            try:
                os.remove(out_file)
            except Exception:
                pass
            return None, ('读取查询结果失败: ' + str(ex))
        try:
            os.remove(out_file)
        except Exception:
            pass
        if raw_bytes[:3] == b'\xef\xbb\xbf':
            raw_bytes = raw_bytes[3:]
        return raw_bytes.decode('utf-8', errors='replace'), None


def kill_old_instances(port):
    if os.name != 'nt':
        return False
    try:
        result = subprocess.run(
            ['netstat', '-ano', '-p', 'tcp'],
            capture_output=True, text=True, timeout=5,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        return False
    killed = False
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1].endswith(':' + str(port)) and parts[3] == 'LISTENING':
            pid = parts[4]
            try:
                subprocess.run(['taskkill', '/F', '/PID', pid],
                               capture_output=True, timeout=5,
                               creationflags=CREATE_NO_WINDOW)
                killed = True
            except Exception:
                pass
    return killed


class SSLThreadingHTTPServer(ThreadingHTTPServer):
    """带 TLS 的线程化 HTTP 服务: 在 get_request 中包装 accept 后的 socket"""

    def __init__(self, addr, handler_cls, cert_path, key_path):
        super().__init__(addr, handler_cls)
        self.ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.ssl_context.load_cert_chain(cert_path, key_path)

    def get_request(self):
        sock, addr = super().get_request()
        try:
            return self.ssl_context.wrap_socket(sock, server_side=True), addr
        except (ssl.SSLError, OSError):
            # 明文 HTTP / 端口扫描打到 HTTPS 端口: 静默关闭, 不影响服务
            try:
                sock.close()
            except Exception:
                pass
            raise

    def handle_error(self, request, client_address):
        ex = sys.exc_info()[1]
        if isinstance(ex, (ssl.SSLError, ConnectionResetError, BrokenPipeError, TimeoutError)):
            return  # 客户端中途断开等常见网络噪音, 静默
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    server_version = 'QinlingGrabPy/2.0'

    def log_message(self, fmt, *args):
        pass

    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _send_raw(self, text):
        body = text.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get('Content-Length', 0) or 0)
        if length <= 0:
            return ''
        raw = self.rfile.read(length)
        return raw.decode('utf-8', errors='replace')

    def _resolve_task_id(self, path):
        parts = path.strip('/').split('/')
        if len(parts) >= 3 and parts[0] == 'api' and parts[1] == 'tasks':
            return parts[2]
        return None

    def _resolve_account_id(self, path):
        """从路径中解析 account_id, 如 /api/accounts/abc123/start -> abc123"""
        m = re.match(r'/api/accounts/([^/]+)(/.*)?$', path)
        if m:
            return m.group(1), m.group(2) or ''
        return None, ''

    # ---------- 访问令牌鉴权 ----------
    def _check_auth(self):
        """校验访问令牌: ?token= query → X-Auth-Token header → grab_token Cookie"""
        supplied = None
        try:
            query = urllib.parse.urlparse(self.path).query
            params = dict(urllib.parse.parse_qsl(query)) if query else {}
            supplied = params.get('token') or self.headers.get('X-Auth-Token')
            if not supplied:
                ck = self.headers.get('Cookie') or ''
                m = re.search(r'(?:^|;\s*)grab_token=([^;]+)', ck)
                if m:
                    supplied = m.group(1)
        except Exception:
            supplied = None
        token = getattr(self.server, 'auth_token', '')
        if not supplied or not token:
            return False
        try:
            return hmac.compare_digest(supplied.encode('utf-8'), token.encode('utf-8'))
        except Exception:
            return False

    def _send_unauthorized(self):
        body = json.dumps({'ok': False, 'msg': '未授权: 访问令牌无效或缺失'},
                          ensure_ascii=False).encode('utf-8')
        self.send_response(401)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('WWW-Authenticate', 'Token')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split('?', 1)[0]
        # 页面公开 (登录遮罩需要先加载), 其余全部需要令牌
        if path != '/' and not self._check_auth():
            self._send_unauthorized()
            return
        if path == '/':
            self._serve_html()
        elif path == '/api/settings':
            self._handle_get_settings()
        elif path == '/cert':
            self._handle_get_cert()
        elif path == '/api/status':
            self._handle_status()
        elif path == '/api/log':
            self._handle_master_log()
        elif path == '/api/logs/dates':
            self._handle_log_dates()
        elif path == '/api/logs':
            self._handle_log_by_date()
        elif path == '/api/accounts':
            self._handle_list_accounts()
        elif path.startswith('/api/tasks/') and path.endswith('/log'):
            task_id = self._resolve_task_id(path)
            if task_id:
                self._handle_task_log(task_id)
            else:
                self._send_json({'ok': False, 'msg': 'not found'}, 404)
        else:
            self._send_json({'ok': False, 'msg': 'not found'}, 404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-Auth-Token')
        self.end_headers()

    def do_POST(self):
        path = self.path.split('?', 1)[0]  # 注意: 不 log 完整 self.path, 防止 ?token= 泄漏
        if not self._check_auth():
            self._send_unauthorized()
            return
        log('POST %s' % path)
        body = self._read_body()
        if path == '/api/auth':
            self._handle_auth()
        elif path == '/api/settings':
            self._handle_set_settings(body)
        elif path == '/api/accounts':
            self._handle_add_account(body)
        elif path.startswith('/api/accounts/') and path.endswith('/creds'):
            aid, _ = self._resolve_account_id(path)
            if aid: self._handle_update_creds(aid, body)
            else: self._send_json({'ok': False, 'msg': 'not found'}, 404)
        elif path == '/api/tasks':
            self._handle_add_task(body)
        elif path == '/api/tasks/start-all':
            self._handle_start_all()
        elif path == '/api/tasks/stop-all':
            self._handle_stop_all()
        elif path.startswith('/api/accounts/') and path.endswith('/start'):
            aid, _ = self._resolve_account_id(path)
            if aid: self._handle_start_account(aid)
            else: self._send_json({'ok': False, 'msg': 'not found'}, 404)
        elif path.startswith('/api/accounts/') and path.endswith('/stop'):
            aid, _ = self._resolve_account_id(path)
            if aid: self._handle_stop_account(aid)
            else: self._send_json({'ok': False, 'msg': 'not found'}, 404)
        elif path.startswith('/api/accounts/') and path.endswith('/reset-tasks'):
            aid, _ = self._resolve_account_id(path)
            if aid: self._handle_reset_account_tasks(aid)
            else: self._send_json({'ok': False, 'msg': 'not found'}, 404)
        elif path.startswith('/api/accounts/') and '/query/' in path:
            aid, rest = self._resolve_account_id(path)
            if aid:
                if '/query/orders' in rest:
                    self._handle_account_query(aid, body, 'Orders')
                elif '/query/fleets' in rest:
                    self._handle_account_query(aid, body, 'Fleets')
                elif '/query/fleetVehicles' in rest:
                    self._handle_account_query(aid, body, 'FleetVehicles')
                else:
                    self._send_json({'ok': False, 'msg': 'not found'}, 404)
            else:
                self._send_json({'ok': False, 'msg': 'not found'}, 404)
        elif path == '/api/query/orders':
            self._handle_query(body, 'Orders')
        elif path == '/api/query/fleets':
            self._handle_query(body, 'Fleets')
        elif path == '/api/query/fleetVehicles':
            self._handle_query(body, 'FleetVehicles')
        elif path.startswith('/api/tasks/') and path.endswith('/stop'):
            task_id = self._resolve_task_id(path)
            if task_id: self._handle_stop_task(task_id)
            else: self._send_json({'ok': False, 'msg': 'not found'}, 404)
        elif path.startswith('/api/tasks/') and path.endswith('/reset'):
            task_id = self._resolve_task_id(path)
            if task_id: self._handle_reset_task(task_id)
            else: self._send_json({'ok': False, 'msg': 'not found'}, 404)
        else:
            self._send_json({'ok': False, 'msg': 'not found'}, 404)

    def do_DELETE(self):
        path = self.path.split('?', 1)[0]
        if not self._check_auth():
            self._send_unauthorized()
            return
        if path.startswith('/api/accounts/'):
            aid, _ = self._resolve_account_id(path)
            if aid: self._handle_remove_account(aid)
            else: self._send_json({'ok': False, 'msg': 'not found'}, 404)
        elif path.startswith('/api/tasks/'):
            task_id = self._resolve_task_id(path)
            if task_id: self._handle_remove_task(task_id)
            else: self._send_json({'ok': False, 'msg': 'not found'}, 404)
        else:
            self._send_json({'ok': False, 'msg': 'not found'}, 404)

    def _serve_html(self):
        try:
            with open(HTML_PATH, 'rb') as f:
                html = f.read()
        except Exception as ex:
            self._send_json({'ok': False, 'msg': '读取页面失败: ' + str(ex)}, 500)
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(html)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(html)

    def _handle_add_task(self, body):
        try:
            payload = json.loads(body) if body else {}
        except Exception:
            self._send_json({'ok': False, 'msg': '请求体不是合法 JSON'}, 400)
            return
        if not payload.get('accountId'):
            self._send_json({'ok': False, 'msg': '缺少账号ID'}, 400)
            return
        if not payload.get('orderCode') or not payload.get('fleetName'):
            self._send_json({'ok': False, 'msg': '订单号/车队名称 不能为空'}, 400)
            return
        date = payload.get('date')
        if date not in ('今日', '明日', '后日'):
            payload['date'] = '今日'
        if 'dryRun' not in payload:
            payload['dryRun'] = True
        # 并行拆分: 1 份=原行为; N 份=自动创建 N 个相同子任务 (启动时按订单+车队+日期分组均分车队车辆)
        try:
            split_count = int(payload.get('splitCount') or 1)
        except (TypeError, ValueError):
            split_count = 1
        if split_count < 1 or split_count > 10:
            split_count = 1
        gs = self.server.grab_server
        task_ids = [gs.add_task(payload) for _ in range(split_count)]
        self._send_json({'ok': True, 'taskId': task_ids[0], 'taskIds': task_ids})

    def _handle_remove_task(self, task_id):
        self.server.grab_server.remove_task(task_id)
        self._send_json({'ok': True})

    def _handle_start_all(self):
        gs = self.server.grab_server
        err = gs.start_all()
        if err:
            self._send_json({'ok': False, 'msg': err}, 500)
            return
        self._send_json({'ok': True, 'msg': '所有任务已启动'})

    def _handle_stop_all(self):
        self.server.grab_server.stop_all()
        self._send_json({'ok': True, 'msg': '停止指令已发送'})

    def _handle_stop_task(self, task_id):
        self.server.grab_server.stop_task(task_id)
        self._send_json({'ok': True})

    # ---------- 账号相关 handlers ----------
    def _handle_add_account(self, body):
        try:
            payload = json.loads(body) if body else {}
        except Exception:
            self._send_json({'ok': False, 'msg': '请求体不是合法 JSON'}, 400)
            return
        gs = self.server.grab_server
        account_id = gs.add_account(payload)
        self._send_json({'ok': True, 'accountId': account_id})

    def _handle_remove_account(self, account_id):
        self.server.grab_server.remove_account(account_id)
        self._send_json({'ok': True})

    def _handle_auth(self):
        """令牌验证 (do_POST 入口已通过 _check_auth, 到这里即合法)"""
        self._send_json({'ok': True, 'server': 'QinlingGrab/3.0'})

    def _handle_update_creds(self, account_id, body):
        try:
            payload = json.loads(body) if body else {}
        except Exception:
            self._send_json({'ok': False, 'msg': '请求体不是合法 JSON'}, 400)
            return
        err = self.server.grab_server.update_creds(account_id, payload)
        if err:
            self._send_json({'ok': False, 'msg': err}, 400)
            return
        self._send_json({'ok': True})

    def _handle_reset_task(self, task_id):
        err = self.server.grab_server.reset_task(task_id)
        if err:
            self._send_json({'ok': False, 'msg': err}, 409)
            return
        self._send_json({'ok': True})

    def _handle_reset_account_tasks(self, account_id):
        gs = self.server.grab_server
        err, n = gs.reset_account_tasks(account_id)
        if err:
            self._send_json({'ok': False, 'msg': err}, 409)
            return
        self._send_json({'ok': True, 'msg': '已重置 %d 个任务' % n})

    def _handle_get_settings(self):
        gs = self.server.grab_server
        self._send_json({'ok': True, 'settings': {'globalDryRun': gs.get_global_dry_run()}})

    def _handle_set_settings(self, body):
        try:
            payload = json.loads(body) if body else {}
        except Exception:
            self._send_json({'ok': False, 'msg': '请求体不是合法 JSON'}, 400)
            return
        if 'globalDryRun' not in payload:
            self._send_json({'ok': False, 'msg': '缺少 globalDryRun 字段'}, 400)
            return
        self.server.grab_server.set_global_dry_run(payload['globalDryRun'])
        msg = '已切换为演练模式' if payload['globalDryRun'] else '已切换为真实抢单模式'
        self._send_json({'ok': True, 'msg': msg})

    def _handle_get_cert(self):
        """下发 CA 证书 PEM, 供手机安装信任"""
        try:
            with open(CERT_PATH, 'rb') as f:
                cert = f.read()
        except Exception as ex:
            self._send_json({'ok': False, 'msg': '证书不可用: ' + str(ex)}, 500)
            return
        self.send_response(200)
        self.send_header('Content-Type', 'application/x-x509-ca-cert')
        self.send_header('Content-Disposition', 'attachment; filename="qinling-grab-ca.crt"')
        self.send_header('Content-Length', str(len(cert)))
        self.end_headers()
        self.wfile.write(cert)

    def _handle_list_accounts(self):
        gs = self.server.grab_server
        accounts = gs.get_accounts_status()
        self._send_json({'ok': True, 'accounts': accounts})

    def _handle_start_account(self, account_id):
        gs = self.server.grab_server
        err = gs.start_account(account_id)
        if err:
            self._send_json({'ok': False, 'msg': err}, 500)
            return
        self._send_json({'ok': True, 'msg': '账号任务已启动'})

    def _handle_stop_account(self, account_id):
        self.server.grab_server.stop_account(account_id)
        self._send_json({'ok': True})

    def _handle_account_query(self, account_id, body, action):
        try:
            payload = json.loads(body) if body else {}
        except Exception:
            self._send_json({'ok': False, 'msg': '请求体不是合法 JSON'}, 400)
            return
        fleet_id = payload.get('fleetId')
        with_quota = bool(payload.get('withQuota'))
        # 优先使用请求体中的凭据, 并同步到服务端账号
        gs = self.server.grab_server
        if payload.get('username') and payload.get('password'):
            query_payload = {
                'username': str(payload['username']),
                'password': str(payload['password']),
                'smsCode': str(payload.get('smsCode', '')),
            }
            # 同步凭据到服务端账号 (凭据可能变化, 旧 Cookie 失效)
            with gs.lock:
                a = gs.accounts.get(account_id)
                if a:
                    a['username'] = query_payload['username']
                    a['password'] = query_payload['password']
                    a['smsCode'] = query_payload['smsCode']
                    a['cookie'] = None
                    a['cookie_valid'] = False
                    gs._mark_dirty()
            raw, err = gs.run_query(query_payload, action, fleet_id=fleet_id, with_quota=with_quota)
        else:
            raw, err = gs.run_account_query(account_id, action, fleet_id=fleet_id, with_quota=with_quota)
        if err:
            self._send_json({'ok': False, 'msg': err}, 500)
            return
        self._send_raw(raw)

    def _handle_status(self):
        gs = self.server.grab_server
        accounts = gs.get_accounts_status()
        self._send_json({
            'ok': True,
            'accounts': accounts,
            'settings': {'globalDryRun': gs.get_global_dry_run()},
        })

    def _handle_master_log(self):
        """统一日志轮询 (实时)"""
        qs = self.path.split('?', 1)[1] if '?' in self.path else ''
        params = dict(urllib.parse.parse_qsl(qs)) if qs else {}
        try:
            since = int(params.get('since', 0))
        except ValueError:
            since = 0
        gs = self.server.grab_server
        text, total = gs.get_master_log(since)
        # 检查是否有任务在运行
        running = any(t['status'] == 'running' for t in gs.get_all_tasks_status())
        self._send_json({
            'ok': True,
            'text': text,
            'offset': total,
            'running': running,
        })

    def _handle_log_dates(self):
        """返回可用的日志日期列表"""
        gs = self.server.grab_server
        dates = gs.get_log_dates()
        self._send_json({'ok': True, 'dates': dates})

    def _handle_log_by_date(self):
        """读取指定日期的历史日志"""
        qs = self.path.split('?', 1)[1] if '?' in self.path else ''
        params = dict(urllib.parse.parse_qsl(qs)) if qs else {}
        date_str = params.get('date', '')
        if not date_str:
            self._send_json({'ok': False, 'msg': '缺少日期参数'}, 400)
            return
        gs = self.server.grab_server
        text = gs.get_log_by_date(date_str)
        self._send_json({'ok': True, 'text': text, 'date': date_str})

    def _handle_task_log(self, task_id):
        qs = self.path.split('?', 1)[1] if '?' in self.path else ''
        params = dict(urllib.parse.parse_qsl(qs)) if qs else {}
        try:
            since = int(params.get('since', 0))
        except ValueError:
            since = 0
        gs = self.server.grab_server
        text, total, status, exit_code = gs.get_task_log(task_id, since)
        self._send_json({
            'ok': True,
            'text': text,
            'offset': total,
            'status': status,
            'exitCode': exit_code,
            'done': status in ('success', 'failed', 'stopped'),
        })

    def _handle_query(self, body, action):
        try:
            payload = json.loads(body) if body else {}
        except Exception:
            self._send_json({'ok': False, 'msg': '请求体不是合法 JSON'}, 400)
            return
        if not payload.get('username') or not payload.get('password'):
            self._send_json({'ok': False, 'msg': '请先填写登录账号和密码'}, 400)
            return
        fleet_id = payload.get('fleetId')
        with_quota = bool(payload.get('withQuota'))
        raw, err = self.server.grab_server.run_query(
            payload, action,
            fleet_id=fleet_id, with_quota=with_quota,
        )
        if err:
            self._send_json({'ok': False, 'msg': err}, 500)
            return
        self._send_raw(raw)
def _pause_exit():
    """非交互环境(systemd/后台运行)不阻塞等待回车"""
    try:
        if sys.stdin and sys.stdin.isatty():
            input('按回车退出')
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(description='秦岭云商自动抢单工具远程服务')
    parser.add_argument('--host', default='0.0.0.0',
                        help='监听地址, 默认 0.0.0.0 (所有网卡); 仅本机用 127.0.0.1')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT)
    parser.add_argument('--token', default='',
                        help='访问令牌; 不指定则读取/自动生成 data/token.txt')
    parser.add_argument('--https', dest='https', action='store_true', default=True,
                        help='启用 HTTPS (自签证书, 默认开启)')
    parser.add_argument('--no-https', dest='https', action='store_false',
                        help='关闭 HTTPS (令牌将明文传输, 仅用于 frp/nginx 终结 TLS 场景)')
    parser.add_argument('--cert-sans', default='',
                        help='额外证书 SAN (公网域名/IP), 逗号分隔')
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--rotate-at', default=ROTATE_AT_DEFAULT,
                        help='每日令牌自动轮换时刻 (HH:MM, 默认 12:00; off 关闭)')
    parser.add_argument('--notify-cmd', default='',
                        help='令牌轮换后推送新令牌的自定义命令 (stdin 读消息; 默认不推送, 生产环境由 grab-token-notify.timer 推送)')
    args = parser.parse_args()

    if not os.path.exists(HTML_PATH):
        print('错误: 未找到 ' + HTML_PATH)
        _pause_exit()
        sys.exit(1)
    if not os.path.exists(ORDER_SCRIPT):
        print('错误: 未找到 ' + ORDER_SCRIPT)
        _pause_exit()
        sys.exit(1)
    if not os.path.exists(QUERY_SCRIPT):
        print('错误: 未找到 ' + QUERY_SCRIPT)
        _pause_exit()
        sys.exit(1)

    # 初始化访问令牌与 HTTPS 证书 (先于服务启动)
    auth_token = _load_or_create_token(args.token)
    cert_path = key_path = None
    if args.https:
        extra_sans = [s.strip() for s in (args.cert_sans or '').split(',') if s.strip()]
        cert_path, key_path = _load_or_create_cert(extra_sans)

    grab_server = GrabServer()

    httpd = None
    for attempt in range(2):
        try:
            if args.https:
                httpd = SSLThreadingHTTPServer((args.host, args.port), Handler,
                                               cert_path, key_path)
            else:
                httpd = ThreadingHTTPServer((args.host, args.port), Handler)
            httpd.daemon_threads = True
            break
        except OSError as ex:
            if attempt == 0:
                log('端口 %d 被占用, 尝试关闭旧实例...' % args.port)
                if kill_old_instances(args.port):
                    time.sleep(1.0)
                    continue
            print('启动失败: ' + str(ex))
            print('解决办法(二选一):')
            print('  1. 关闭占用该端口的程序后重试')
            print('  2. 用 --port 指定其他端口')
            _pause_exit()
            sys.exit(1)

    httpd.grab_server = grab_server
    httpd.auth_token = auth_token
    scheme = 'https' if args.https else 'http'
    log('========== 秦岭云商抢单控制台已启动 ==========')
    log('访问令牌: %s (请妥善保存, 已存于 %s)' % (auth_token, TOKEN_PATH))
    log('本机访问: %s://127.0.0.1:%d/' % (scheme, args.port))
    if args.host not in ('127.0.0.1', 'localhost'):
        for ip in get_lan_ips():
            log('局域网访问: %s://%s:%d/' % (scheme, ip, args.port))
    if args.https:
        log('提示: 自签证书首次访问会有安全警告, 手机可下载 %s://<IP>:%d/cert 安装信任'
            % (scheme, args.port))
    log('关闭本服务(Ctrl+C)即可停止')
    if not args.no_browser and args.host in ('127.0.0.1', 'localhost'):
        try:
            webbrowser.open('%s://127.0.0.1:%d/' % (scheme, args.port))
        except Exception:
            pass

    # 每日令牌轮换 (显式 --token 时停用, 命令行令牌为准)。
    # 服务进程默认不持有飞书凭据: 生产环境新令牌由系统定时任务 grab-token-notify.timer
    # 以 wupengyu 的 lark-cli 登录态推送飞书; --notify-cmd 可指定自定义推送命令。
    notify_cmd = None
    notify_arg = (args.notify_cmd or '').strip()
    if notify_arg and notify_arg.lower() != 'off':
        notify_cmd = shlex.split(notify_arg)
    rotate_arg = (args.rotate_at or '').strip().lower()
    if rotate_arg == 'off':
        log('每日令牌轮换已关闭 (--rotate-at off)')
    elif args.token:
        log('检测到 --token 指定令牌, 每日令牌轮换已停用')
    else:
        parsed = _parse_rotate_at(rotate_arg)
        if not parsed:
            log('警告: --rotate-at 格式无效 (%s), 使用默认 %s'
                % (args.rotate_at, ROTATE_AT_DEFAULT))
            parsed = _parse_rotate_at(ROTATE_AT_DEFAULT)
        threading.Thread(target=_token_rotation_loop,
                         args=(httpd, parsed[0], parsed[1], notify_cmd),
                         daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log('收到中断, 停止服务')
    finally:
        grab_server.stop_all()
        grab_server.save_state_now()
        httpd.server_close()


if __name__ == '__main__':
    main()