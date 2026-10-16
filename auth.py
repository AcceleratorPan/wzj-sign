"""TeacherMate API credentials, identity checks and automatic renewal."""

import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

BASE_URL = "https://v18.teachermate.cn"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.4 Safari/605.1.15"
TOKEN = re.compile(r"[a-zA-Z0-9_-]{32}\Z")
URL_TOKEN = re.compile(rb"(?i)https://v18\.teachermate\.cn/[^\x00\s\"<>]{0,250}?\bopenid=([a-zA-Z0-9_-]{32})(?![a-zA-Z0-9_-])")
EXPIRED_MESSAGE = re.compile(r"登录信息失效|登录已过期|请.*重新登录|openid.*(?:invalid|expired|失效)|unauthorized", re.I)
logger = logging.getLogger(__name__)


class ApiError(RuntimeError):
    pass


class AuthExpired(ApiError):
    def __init__(self, message, http_status=None):
        super().__init__(message)
        self.http_status = http_status


class TransientApiError(ApiError):
    pass


class IdentityMismatch(ApiError):
    pass


class RefreshError(RuntimeError):
    pass


def fingerprint(value):
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def private_directory(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, UnicodeError):
        return default


def write_json(path, value):
    private_directory(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Api:
    def __init__(self, ua=USER_AGENT, *, transport=None, timeout=15):
        self.client = httpx.AsyncClient(
            base_url=BASE_URL, timeout=timeout, trust_env=False,
            transport=transport if transport is not None else httpx.AsyncHTTPTransport(retries=1),
            headers={"User-Agent": ua, "Accept": "application/json"},
        )

    async def close(self):
        await self.client.aclose()

    async def _request(self, method, path, open_id, **kwargs):
        try:
            response = await self.client.request(method, path, headers={"openId": open_id}, **kwargs)
        except httpx.RequestError as exc:
            raise TransientApiError("API 网络异常：" + type(exc).__name__) from exc
        if response.status_code == 401:
            raise AuthExpired("微助教凭证已失效 (HTTP 401)", http_status=401)
        if response.status_code in (408, 429) or response.status_code >= 500:
            raise TransientApiError(f"微助教暂时不可用 (HTTP {response.status_code})")
        try:
            data = response.json()
        except ValueError as exc:
            raise ApiError(f"API 返回非 JSON 数据 (HTTP {response.status_code})") from exc
        if isinstance(data, dict) and EXPIRED_MESSAGE.search(str(data.get("message", ""))):
            raise AuthExpired("微助教返回登录失效", http_status=response.status_code)
        if not response.is_success:
            code = data.get("errorCode") if isinstance(data, dict) else None
            raise ApiError(f"API 请求被拒绝 (HTTP {response.status_code}，错误码 {code})")
        return data

    async def students_role(self, open_id):
        return await self._request("GET", "/wechat-api/v2/students/role", open_id)

    async def verify_identity(self, open_id, expected_student_id):
        if not TOKEN.fullmatch(open_id):
            raise AuthExpired("凭证格式无效")
        roles = await self.students_role(open_id)
        if roles == []:
            raise AuthExpired("凭证未绑定学生角色")
        if not isinstance(roles, list) or not all(isinstance(r, dict) for r in roles):
            raise ApiError("学生角色接口返回结构异常")
        ids = {str(r.get("student_id")) for r in roles if r.get("student_id") is not None}
        if str(expected_student_id) not in ids:
            raise IdentityMismatch("凭证对应的学生与指定学生不一致，拒绝应用")
        return roles

    async def active_sign(self, open_id):
        data = await self._request("GET", "/wechat-api/v1/class-attendance/student/active_signs", open_id)
        if not isinstance(data, list) or not all(
            isinstance(x, dict) and {"signId", "courseId"} <= x.keys() for x in data
        ):
            raise ApiError("活动签到接口返回结构异常")
        return data

    async def sign_in(self, open_id, payload):
        return await self._request("POST", "/wechat-api/v1/class-attendance/student-sign-in", open_id, json=payload)


@dataclass
class Credential:
    value: str = field(repr=False)
    captured_at: float
    source: str


class WeChatCache:
    def __init__(self, profile):
        self.profile = Path(profile).expanduser()

    def scan(self, since=0):
        candidates = {}
        root = self.profile / "Cache" / "Cache_Data"
        if not root.is_dir():
            raise RefreshError("指定的微信网页缓存目录不存在")
        # pathlib.glob silently returns nothing for a TCC-denied directory on
        # Python 3.9, which otherwise looks like an OAuth timeout.
        try:
            with os.scandir(root) as entries:
                paths = [Path(entry.path) for entry in entries if entry.name.endswith("_0")]
        except PermissionError as exc:
            raise RefreshError("后台无法读取微信缓存；检查 Python/Xcode 的完全磁盘访问权限") from exc
        except OSError as exc:
            raise RefreshError("暂时无法读取微信缓存目录；稍后自动重试") from exc
        denied = 0
        readable = 0
        for path in paths:
            try:
                modified = path.stat().st_mtime
                if modified < since:
                    continue
                with path.open("rb") as source:
                    # Cache keys and redirect headers precede the response body.
                    data = source.read(16384)
                readable += 1
            except PermissionError:
                denied += 1
                continue
            except FileNotFoundError:
                continue
            for match in URL_TOKEN.finditer(data):
                value = match.group(1).decode("ascii")
                if value not in candidates or modified > candidates[value].captured_at:
                    candidates[value] = Credential(value, modified, "wechat_cache")
        if denied and not readable:
            raise RefreshError("后台无法读取微信缓存文件；检查 Python/Xcode 的完全磁盘访问权限")
        return sorted(candidates.values(), key=lambda x: x.captured_at, reverse=True)


class AuthProvider:
    def __init__(self, api, auth_config, runtime, *, cache=None, refresher=None, issuer=None):
        self.api = api
        self.expected_student_id = auth_config["expected_student_id"]
        self.config = auth_config
        self.path = Path(runtime) / "credentials.json"
        self.mode = auth_config.get("mode", "http")
        self.last_source = None
        self.observer = None
        self.acquisition_trigger = "manual"
        self.cache = None
        self.issuer = None
        if self.mode == "http":
            from http_auth import HttpCredentialIssuer
            self.issuer = issuer or HttpCredentialIssuer(
                runtime, self.expected_student_id, timeout=auth_config.get("request_timeout", 15),
                identity_path=auth_config.get("identity_file"),
            )
        elif self.mode == "wechat":
            self.cache = cache or WeChatCache(auth_config["wechat_profile"])
        else:
            raise ValueError("auth.mode 仅支持 http 或 wechat")
        self.refresher = refresher or self._refresh_wechat
        self.rejected = set()

    def load(self):
        data = read_json(self.path, {})
        if isinstance(data, dict) and str(data.get("student_id")) == str(self.expected_student_id):
            value = data.get("open_id", "")
            if isinstance(value, str) and TOKEN.fullmatch(value):
                return Credential(value, data.get("captured_at", 0), data.get("source", "saved"))
        return None

    async def apply(self, credential):
        await self.api.verify_identity(credential.value, self.expected_student_id)
        write_json(self.path, {
            "open_id": credential.value,
            "student_id": self.expected_student_id,
            "captured_at": credential.captured_at,
            "applied_at": time.time(),
            "source": credential.source,
        })
        self.last_source = credential.source
        if self.observer is not None:
            self.observer.applied(credential, self.acquisition_trigger)
        logger.info("已校验身份并应用凭证 %s（来源：%s）", fingerprint(credential.value), credential.source)
        return credential.value

    async def _try(self, credential):
        if credential.value in self.rejected:
            return None
        try:
            return await self.apply(credential)
        except AuthExpired as exc:
            if self.observer is not None:
                self.observer.expired(credential, exc.http_status, self.acquisition_trigger)
            self.rejected.add(credential.value)
            return None
        except IdentityMismatch:
            self.rejected.add(credential.value)
            return None

    async def _refresh_wechat(self):
        environment = os.environ.copy()
        if self.config.get("ui_debug"):
            environment["WZJ_UI_DEBUG"] = str(self.path.parent)
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(Path(__file__).with_name("wechat_ui.py")),
            "--account", self.config.get("account", "微助教服务号"),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=environment,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            process.kill()
            await process.communicate()
            raise
        if process.returncode:
            try:
                detail = json.loads(stdout).get("error", "微信自动操作失败")
            except (ValueError, AttributeError):
                detail = "微信自动操作失败；请检查登录及系统权限"
            raise RefreshError(detail)

    async def acquire(self, *, force=False, reject=None, trigger="automatic"):
        self.acquisition_trigger = "forced" if force and trigger == "automatic" else trigger
        if reject:
            self.rejected.add(reject)
        if not force:
            saved = self.load()
            if saved:
                token = await self._try(saved)
                if token:
                    return token
        if self.mode == "http":
            logger.info("正在通过 HTTP 获取新凭证（无需微信或浏览器）...")
            credential = await self.issuer.issue()
            if credential.value in self.rejected:
                raise RefreshError("HTTP 换证返回了已拒绝的凭证；稍后重试")
            try:
                return await self.apply(credential)
            except AuthExpired:
                self.rejected.add(credential.value)
                raise RefreshError("HTTP 新凭证未通过学生接口校验；稍后重试") from None
        cached = await asyncio.to_thread(self.cache.scan)
        if not force:
            for credential in cached[:self.config.get("max_cached_candidates", 12)]:
                token = await self._try(credential)
                if token:
                    return token
        baseline = {x.value for x in cached}
        started = time.time()
        logger.info("正在通过微信服务号重新获取凭证...")
        try:
            await self.refresher()
        except asyncio.TimeoutError as exc:
            raise RefreshError("微信自动操作超时") from exc
        deadline = time.monotonic() + self.config.get("refresh_timeout", 45)
        while time.monotonic() < deadline:
            fresh = await asyncio.to_thread(self.cache.scan, started)
            for credential in fresh:
                if credential.value in baseline:
                    continue
                token = await self._try(credential)
                if token:
                    return token
            await asyncio.sleep(1)
        raise RefreshError("微信授权后未取得指定学生的新凭证；稍后自动重试")
