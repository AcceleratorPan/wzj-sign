"""Issue TeacherMate student credentials using the saved, bound account identity.

The endpoint currently accepts this identity without web cookies or a JWT.
Treat the identity file as a secret. AuthProvider verifies the returned student
role before committing any credential; this module never opens a client UI.
"""

import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

from auth import (
    BASE_URL, TOKEN, Credential, IdentityMismatch, RefreshError,
    TransientApiError, read_json,
)


class HttpCredentialIssuer:
    def __init__(self, runtime, expected_student_id, *, timeout=15, transport=None, identity_path=None):
        self.identity_path = Path(identity_path) if identity_path else Path(runtime) / "web-identity.json"
        self.expected_student_id = expected_student_id
        self.timeout = timeout
        self.transport = transport

    def load_identity(self):
        data = read_json(self.identity_path)
        if not isinstance(data, dict):
            raise RefreshError("缺少有效的 .runtime/web-identity.json，请先导入该学生已验证的身份文件")
        if str(data.get("student_id")) != str(self.expected_student_id):
            raise IdentityMismatch("保存的换证身份与指定学生不一致，拒绝请求")
        if data.get("source") != "authenticated_web_profile":
            raise RefreshError("换证身份缺少已认证账号资料来源，请重新执行 web_session.py inspect")
        value = data.get("raw_wechat_openid")
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{28}", value):
            raise RefreshError("保存的微信原始 OpenID 格式无效")
        return value

    @staticmethod
    def parse_credential(data):
        if not isinstance(data, dict) or not isinstance(data.get("url"), str):
            raise RefreshError("HTTP 换证未返回凭证地址")
        try:
            url = urlsplit(data["url"])
            allowed = (
                url.scheme == "https" and url.netloc == "v18.teachermate.cn"
                and url.path in ("/wechat-pro-ssr", "/wechat-pro-ssr/")
                and not url.fragment
            )
            values = parse_qs(url.query, keep_blank_values=True).get("openid", [])
        except ValueError:
            raise RefreshError("HTTP 换证返回的地址格式无效") from None
        if not allowed or len(values) != 1 or not TOKEN.fullmatch(values[0]):
            raise RefreshError("HTTP 换证返回了非预期地址或无效凭证")
        return values[0]

    async def _login(self, module, extra=""):
        raw_openid = self.load_identity()
        # A fresh client sends no browser cookies or authorization headers.
        # Do not follow the returned URL: extract locally, then verify the role.
        async with httpx.AsyncClient(
            base_url=BASE_URL, timeout=self.timeout,
            transport=self.transport if self.transport is not None else httpx.AsyncHTTPTransport(retries=1),
            trust_env=False, follow_redirects=False,
        ) as client:
            try:
                response = await client.post("/wechat-api/v1/accounts/login", json={
                    "openid": raw_openid, "module": module, "originCode": "",
                    "extra": extra, "force": False,
                })
            except httpx.RequestError as exc:
                raise TransientApiError("HTTP 换证网络异常：" + type(exc).__name__) from None
        if response.status_code in (408, 429) or response.status_code >= 500:
            raise TransientApiError(f"HTTP 换证暂时不可用 (HTTP {response.status_code})")
        if response.status_code in (401, 403):
            raise RefreshError(f"HTTP 换证身份被拒绝 (HTTP {response.status_code})；请检查绑定或接口认证要求")
        if response.status_code != 200:
            raise RefreshError(f"HTTP 换证请求被拒绝 (HTTP {response.status_code})")
        try:
            data = response.json()
        except ValueError:
            raise RefreshError("HTTP 换证返回非 JSON 数据") from None
        return data

    async def issue(self):
        data = await self._login("ssr_hub")
        return Credential(self.parse_credential(data), time.time(), "http_account_login")

    async def sign_in_qr(self, qr_url):
        if not isinstance(qr_url, str) or not re.fullmatch(
            r"https://www\.teachermate\.com\.cn/api/v1/qr/attendance/[a-f0-9]{96}", qr_url,
        ):
            raise RefreshError("二维码地址无效；必须使用微助教当前签到页面的二维码")
        data = await self._login("s_qr_sign", qr_url.rsplit("/", 1)[-1])
        if not isinstance(data, dict) or not isinstance(data.get("url"), str):
            raise RefreshError("二维码签到未返回结果地址")
        try:
            url = urlsplit(data["url"])
            query = parse_qs(url.query, keep_blank_values=True)
        except ValueError:
            raise RefreshError("二维码签到结果地址格式无效") from None
        tokens = query.get("openid", [])
        success = query.get("success", [])
        if (url.scheme != "https" or url.netloc != "v18.teachermate.cn"
                or url.path != "/wechat-pro-ssr/student/signresult" or url.fragment
                or len(tokens) != 1 or not TOKEN.fullmatch(tokens[0])
                or success not in (["0"], ["1"])):
            raise RefreshError("二维码签到返回了非预期结果地址")
        response = {"errorCode": 0 if success == ["1"] else None,
                    "msgClient": query.get("message", [""])[0],
                    "studentRank": query.get("studentRank", [None])[0]}
        credential = Credential(tokens[0], time.time(), "http_account_login")
        return credential, response
