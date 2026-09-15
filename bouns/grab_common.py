# -*- coding: utf-8 -*-
"""
秦岭云商自动抢单工具 - 公共模块
包含: HTTP 会话(Cookie 自动维护)、RSA 加密、登录、日志
"""
import sys
import io
import time
import json
import base64
import ssl
import re
import urllib.request
import urllib.parse
import http.cookiejar
from datetime import datetime

# 强制 stdout/stderr 使用 UTF-8 输出 (PyInstaller 打包后默认为系统编码 GBK, 导致中文乱码)
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
else:
    # Python < 3.7 兼容
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# cryptography 用于 RSA PKCS1 v1.5 加密 (与网站 JSEncrypt 一致)
from cryptography.hazmat.primitives.serialization import load_der_public_key
from cryptography.hazmat.primitives.asymmetric import padding

BASE = 'https://man.qinlingshuzi.com'
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')
REF_DISPATCH = '/busi/tms/shipment/dispatchPage?menuId=2465'
REF_VEHICLE = '/busi/base/vehicle/batchSearchList'

# 网站 login JS 使用的 512 位 RSA 公钥 (Base64 DER)
_RSA_PUB_B64 = ('MFwwDQYJKoZIhvcNAQEBBQADSwAwSAJBAKoR8mX0rGKLqzcWmOzbfj64K8ZI'
                'gOdHnzkXSOVOZbFu/TJhZ7rFAN+eaGkl3C4buccQd/EjEsj9ir7ijT7h96MCAwEAAQ==')


def log(msg):
    """直接写标准输出: 控制台/重定向/Web服务 三种场景下都是纯文本"""
    ts = datetime.now().strftime('%H:%M:%S.%f')[:-3]
    print('[%s] %s' % (ts, msg), flush=True)


def rsa_encrypt(text):
    """与网站 JSEncrypt 相同: PKCS1 v1.5, 返回 Base64"""
    der = base64.b64decode(_RSA_PUB_B64)
    pub = load_der_public_key(der)
    enc = pub.encrypt(text.encode('utf-8'), padding.PKCS1v15())
    return base64.b64encode(enc).decode('ascii')


def e(v):
    """URL 编码 (与 PowerShell [uri]::EscapeDataString 等价)"""
    return urllib.parse.quote(str(v) if v is not None else '', safe='')


class Session:
    """带 Cookie 自动维护 + 浏览器头的 HTTP 会话"""

    def __init__(self, timeout=20):
        # 强制 TLS1.2, 关闭 Expect:100-continue (否则会被站点 WAF 拦截)
        self._ssl_ctx = ssl.create_default_context()
        self._ssl_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        self.timeout = timeout
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            HTTPSContextHandler(self._ssl_ctx)
        )
        # 关闭 Expect:100-continue
        self.opener.addheaders = []

    def _browser_headers(self, referer='/index'):
        return [
            ('User-Agent', UA),
            ('Accept', 'application/json, text/javascript, */*; q=0.01'),
            ('Accept-Language', 'zh-CN,zh;q=0.9'),
            ('X-Requested-With', 'XMLHttpRequest'),
            ('Origin', BASE),
            ('Referer', BASE + referer),
        ]

    def _request(self, url, data=None, referer='/index'):
        req = urllib.request.Request(url, data=data)
        for k, v in self._browser_headers(referer):
            req.add_header(k, v)
        if data is not None:
            req.add_header('Content-Type', 'application/x-www-form-urlencoded; charset=UTF-8')
        resp = self.opener.open(req, timeout=self.timeout)
        raw = resp.read()
        # 处理 gzip/deflate
        enc = resp.headers.get('Content-Encoding', '').lower()
        if enc == 'gzip':
            import gzip
            raw = gzip.decompress(raw)
        elif enc == 'deflate':
            import zlib
            try:
                raw = zlib.decompress(raw)
            except zlib.error:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        return raw.decode('utf-8', errors='replace')

    def get(self, url, referer='/index'):
        return self._request(url, data=None, referer=referer)

    def post_form(self, url, body, referer='/index'):
        data = body.encode('utf-8')
        return self._request(url, data=data, referer=referer)

    def set_cookie_text(self, cookie_text):
        """从 'k=v; k2=v2' 字符串注入 Cookie (调试用)"""
        for part in cookie_text.split(';'):
            kv = part.strip().split('=', 1)
            if len(kv) == 2 and kv[0]:
                from http.cookiejar import Cookie
                c = Cookie(version=0, name=kv[0], value=kv[1],
                          port=None, port_specified=False,
                          domain='man.qinlingshuzi.com', domain_specified=True, domain_initial_dot=False,
                          path='/', path_specified=True,
                          secure=False, expires=None, discard=True,
                          comment=None, comment_url=None, rest={}, rfc2109=False)
                self.jar.set_cookie(c)

    def get_cookie_text(self):
        """将当前 Cookie Jar 序列化为 'k=v; k2=v2' 格式, 供共享登录使用"""
        parts = []
        for cookie in self.jar:
            parts.append('%s=%s' % (cookie.name, cookie.value))
        return '; '.join(parts)


class HTTPSContextHandler(urllib.request.HTTPSHandler):
    """让 urllib 使用指定的 SSL 上下文"""

    def __init__(self, ctx):
        super().__init__(context=ctx)


def parse_json(text, desc):
    """解析 JSON, 失败时给出可读错误"""
    try:
        return json.loads(text)
    except Exception:
        snippet = text[:180] if text else ''
        raise RuntimeError('[%s] 响应不是JSON, 可能登录已失效: %s' % (desc, snippet))


def login(sess, username, password, sms_code=None, interactive=False):
    """登录流程, 返回 True/False。
    interactive=True 时, 2FA 缺失会从 stdin 读取验证码
    """
    log('正在登录账号 [%s] ...' % username)
    body = ('username=' + e(rsa_encrypt(username)) +
            '&password=' + e(rsa_encrypt(password)) +
            '&validateCode=&type=password&vCode=' + e(rsa_encrypt('')))
    j = parse_json(sess.post_form(BASE + '/login', body, '/login'), '登录')
    if j.get('code') != 0:
        msg = j.get('msg', '')
        log('登录失败: %s' % msg)
        if '验证码' in str(msg):
            log('提示: 若系统要求图形验证码, 请先网页登录确认账号状态或稍后重试')
        return False

    data = j.get('data') or {}
    if data.get('type') == 3:
        log('该账号需要短信二次验证 (2FA)')
        code = sms_code
        if not code:
            if not interactive:
                log('无法交互式输入验证码(非交互模式), 请在界面填写短信验证码后重试')
                return False
            try:
                code = input('请输入手机短信验证码: ').strip()
            except Exception:
                log('读取短信验证码失败')
                return False
        vbody = ('username=' + e(rsa_encrypt(username)) +
                 '&tempToken=' + e(str(data.get('tempToken', ''))) +
                 '&type=vCode&vCode=' + e(rsa_encrypt(code)))
        j2 = parse_json(sess.post_form(BASE + '/login/verify', vbody, '/login'), '二次验证')
        if j2.get('code') != 0:
            log('二次验证失败: %s' % j2.get('msg'))
            return False
        log('二次验证通过, 登录成功')
        return True

    log('登录成功')
    return True
