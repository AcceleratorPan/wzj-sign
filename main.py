"""Unattended TeacherMate attendance with identity-bound HTTP credential renewal."""

import argparse
import asyncio
import fcntl
import ipaddress
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from auth import (
    Api, ApiError, AuthExpired, AuthProvider, IdentityMismatch, RefreshError,
    TransientApiError, USER_AGENT, fingerprint,
    private_directory, read_json, write_json,
)
from auth_observation import AuthObservation
from log_ui import start_server
from notify import Notifier

logger = logging.getLogger(__name__)


def load_config(path):
    config = read_json(path)
    if not isinstance(config, dict):
        raise ValueError("配置不存在或不是有效 JSON")
    config.setdefault("interval", 5000)
    config.setdefault("wait", 1000)
    config.setdefault("ua", USER_AGENT)
    config.setdefault("auth_check_interval", 60)
    config.setdefault("retry_initial", 5)
    config.setdefault("retry_max", 300)
    config.setdefault("prevent_sleep", True)
    web = config.setdefault("web", {})
    if not isinstance(web, dict):
        raise ValueError("web 必须是对象")
    web.setdefault("enabled", True)
    web.setdefault("host", "127.0.0.1")
    web.setdefault("port", 8765)
    push = config.setdefault("push", {"enable": False})
    if not isinstance(push, dict) or not isinstance(push.get("enable", False), bool):
        raise ValueError("push.enable 必须是布尔值")
    if not isinstance(push.get("ntfy", {}), dict):
        raise ValueError("push.ntfy 必须是对象")
    push_fields = {"xizhi_api": push.get("xizhi_api", ""),
                   "service_ip": push.get("service_ip", "")}
    push_fields.update({"ntfy." + key: push.get("ntfy", {}).get(key, "")
                        for key in ("api", "topic", "token")})
    for field, value in push_fields.items():
        if not isinstance(value, str):
            raise ValueError("push." + field + " 必须是字符串")
    if push.get("service_ip"):
        try:
            ipaddress.ip_address(push["service_ip"])
        except ValueError:
            raise ValueError("push.service_ip 必须是有效 IP 地址") from None
    if not isinstance(web["enabled"], bool):
        raise ValueError("web.enabled 必须是布尔值")
    if not isinstance(web["host"], str) or not web["host"]:
        raise ValueError("web.host 必须是非空字符串")
    if not isinstance(web["port"], int) or not 1 <= web["port"] <= 65535:
        raise ValueError("web.port 必须是 1-65535 的整数")
    auth = config.get("auth", {})
    if not isinstance(auth, dict) or not auth.get("expected_student_id"):
        raise ValueError("auth.expected_student_id 必须配置")
    mode = auth.setdefault("mode", "http")
    if mode not in ("http", "wechat"):
        raise ValueError("auth.mode 仅支持 http 或 wechat")
    if mode == "wechat" and not auth.get("wechat_profile"):
        raise ValueError("微信模式必须配置 auth.wechat_profile")
    timeout = auth.get("request_timeout", 15)
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError("auth.request_timeout 必须为正数")
    identity_file = auth.get("identity_file")
    if identity_file is not None and (not isinstance(identity_file, str) or not Path(identity_file).is_absolute()):
        raise ValueError("auth.identity_file 必须为绝对路径")
    for key in ("interval", "auth_check_interval", "retry_initial", "retry_max"):
        if not isinstance(config[key], (int, float)) or config[key] <= 0:
            raise ValueError(key + " 必须为正数")
    if not isinstance(config["wait"], (int, float)) or config["wait"] < 0:
        raise ValueError("wait 必须为非负毫秒数")
    return config


def action_required(exc):
    if isinstance(exc, IdentityMismatch):
        return True
    if isinstance(exc, ApiError) and not isinstance(exc, (AuthExpired, TransientApiError)):
        return True
    if isinstance(exc, RefreshError):
        return any(phrase in str(exc) for phrase in (
            "缺少", "无效", "不一致", "被拒绝", "已失效", "重新导入", "重新执行",
        )) and "二维码已过期" not in str(exc)
    return False


def service_ip(config):
    if config.get("push", {}).get("service_ip"):
        return config["push"]["service_ip"]
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
            connection.connect(("1.1.1.1", 80))
            return connection.getsockname()[0]
    except OSError:
        return "127.0.0.1"


class ProcessLock:
    def __init__(self, path):
        self.path = path
        self.file = None

    def __enter__(self):
        self.file = self.path.open("a+")
        os.chmod(self.path, 0o600)
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.close()
            raise RuntimeError("已有脚本在运行；先用 service.py stop 停止，避免并行授权")
        self.file.seek(0)
        self.file.truncate()
        self.file.write(str(os.getpid()))
        self.file.flush()
        return self

    def __exit__(self, *_):
        self.file.close()


class Worker:
    def __init__(self, config, runtime, api, provider):
        self.config = config
        self.runtime = runtime
        self.api = api
        self.provider = provider
        self.open_id = ""
        self.last_identity_check = 0
        self.last_idle_log = 0
        self.identity = config["auth"]["expected_student_id"]
        self.notifier = Notifier(config.get("push", {}))
        self.pending_notifications = set()
        notified = read_json(runtime / "notified.json", {})
        self.notified = notified.get("events", {}) if isinstance(notified, dict) and str(notified.get("student_id")) == str(self.identity) else {}
        if not isinstance(self.notified, dict):
            self.notified = {}
        self.observation = AuthObservation(runtime, self.identity)
        self.provider.observer = self.observation
        self.auth_trigger = "automatic"
        saved = read_json(runtime / "signed.json", {})
        if not isinstance(saved, dict):
            saved = {}
        self.signed = saved.get("signs", {}) if str(saved.get("student_id")) == str(self.identity) else {}
        if not isinstance(self.signed, dict):
            self.signed = {}
        self.status = {"pid": os.getpid(), "started_at": time.time(), "student_id": self.identity,
                       "auth_mode": config["auth"].get("mode", "http")}

    def update_status(self, phase, **fields):
        self.status.update(phase=phase, heartbeat_at=time.time(), **fields)
        if self.open_id:
            self.status["credential_fingerprint"] = fingerprint(self.open_id)
        write_json(self.runtime / "status.json", self.status)

    async def recover(self, *, force=False, reject=None):
        self.update_status("refreshing", last_error=None)
        self.open_id = await self.provider.acquire(force=force, reject=reject, trigger=self.auth_trigger)
        self.last_identity_check = time.monotonic()
        self.update_status("ready", last_auth_at=time.time(), last_error=None,
                           credential_source=self.provider.last_source)

    def remember_sign(self, key):
        self.signed[key] = time.time()
        ordered = sorted(self.signed.items(), key=lambda x: x[1], reverse=True)
        self.signed = dict(ordered[:4096])
        write_json(self.runtime / "signed.json", {"student_id": self.identity, "signs": self.signed})

    async def notify_once(self, key, title, content):
        if not self.config.get("push", {}).get("enable") or key in self.notified:
            return
        self.notified[key] = time.time()
        self.notified = dict(sorted(self.notified.items(), key=lambda item: item[1], reverse=True)[:8192])
        write_json(self.runtime / "notified.json", {"student_id": self.identity, "events": self.notified})
        task = asyncio.create_task(self.notifier.send(title, content))
        self.pending_notifications.add(task)
        task.add_done_callback(self._notification_done)

    def _notification_done(self, task):
        self.pending_notifications.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning("推送失败：%s", type(exc).__name__)

    async def flush_notifications(self):
        if self.pending_notifications:
            await asyncio.gather(*self.pending_notifications, return_exceptions=True)

    async def notify_problem(self, key, detail):
        detail = re.sub(r"[A-Za-z0-9_-]{28,}", "<redacted>", str(detail))[:200]
        await self.notify_once("problem:" + key + ":" + detail, "签到遇到问题，请检查",
                               f"{detail}\n服务运行于 {service_ip(self.config)}。\n"
                               f"{datetime.now().astimezone():%Y-%m-%d %H:%M:%S}")

    async def tick(self, *, dry_run=False):
        if not self.open_id:
            await self.recover()
        if time.monotonic() - self.last_identity_check >= self.config["auth_check_interval"]:
            await self.api.verify_identity(self.open_id, self.identity)
            self.observation.api_success(self.open_id, polling=False)
            self.last_identity_check = time.monotonic()
        active = await self.api.active_sign(self.open_id)
        self.observation.api_success(self.open_id)
        self.update_status("running", last_api_at=time.time(), active_signs=len(active),
                           dry_run=dry_run, last_error=None)
        if not active and time.monotonic() - self.last_idle_log >= 600:
            logger.info("运行正常，当前没有活动签到")
            self.last_idle_log = time.monotonic()
        for sign in active:
            sign_id, course_id = sign["signId"], sign["courseId"]
            mode = "qrcode" if sign.get("isQR") else "gps" if sign.get("isGPS") else "normal"
            key = f"{course_id}:{sign_id}"
            if key in self.signed:
                continue
            name = sign.get("name") or str(course_id)
            course = sign.get("courseName") or sign.get("className") or name
            course = str(course).strip().removesuffix("课程").strip() or str(name)
            method = {"normal": "普通签到", "gps": "GPS定位签到", "qrcode": "二维码签到"}[mode]
            if dry_run:
                logger.info("只读检查：发现签到 %s（%s），不提交", name, key)
                continue
            await self.notify_once("found:" + key, f"{course}开启签到（{method}）",
                                   f"{course}开启了{method}。\n"
                                   f"服务运行于 {service_ip(self.config)}。\n"
                                   f"{datetime.now().astimezone():%Y-%m-%d %H:%M:%S}")
            payload = {"courseId": course_id, "signId": sign_id}
            if sign.get("isGPS"):
                lat, lon = self.config.get("lat"), self.config.get("lon")
                if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    logger.error("GPS 签到缺少有效的配置坐标，跳过 %s", key)
                    self.observation.event("attendance_failed", mode=mode, course_id=course_id,
                                           sign_id=sign_id, reason="invalid_coordinates")
                    await self.notify_problem(key, f"GPS 签到 {name}（{key}）缺少有效坐标，请检查 config.json")
                    continue
                payload.update(lat=lat, lon=lon)
            logger.info("发现签到 %s（%s）", name, key)
            await asyncio.sleep(self.config["wait"] / 1000)
            try:
                if sign.get("isQR"):
                    from http_auth import HttpCredentialIssuer
                    from qr_feed import attendance_qr
                    issuer = self.provider.issuer or HttpCredentialIssuer(
                        self.runtime, self.identity, timeout=self.config["auth"].get("request_timeout", 15),
                        identity_path=self.config["auth"].get("identity_file"),
                    )
                    await self.api.verify_identity(self.open_id, self.identity)
                    async with attendance_qr(course_id, sign_id) as qr:
                        _, response = await issuer.sign_in_qr(qr.url)
                        rank = response.get("studentRank")
                        if response.get("errorCode") == 0 and not (str(rank).isdigit() and int(rank) > 0):
                            response["studentRank"] = await qr.rank_for(self.identity)
                    self.last_identity_check = time.monotonic()
                else:
                    response = await self.api.sign_in(self.open_id, payload)
            except (AuthExpired, TransientApiError):
                raise
            except (ApiError, RefreshError, RuntimeError) as exc:
                self.update_status("running", last_error=str(exc))
                self.observation.event("attendance_failed", mode=mode, course_id=course_id,
                                       sign_id=sign_id, reason=type(exc).__name__)
                logger.error("签到未成功：%s，%s；下次检查重试", key, exc)
                if action_required(exc):
                    await self.notify_problem(key, f"签到 {name}（{key}）失败：{exc}")
                continue
            if isinstance(response, dict) and (
                response.get("errorCode") in (0, 305) or response.get("studentRank") not in (None, "")
            ):
                self.remember_sign(key)
                self.observation.event("attendance_succeeded", mode=mode, course_id=course_id, sign_id=sign_id)
                self.update_status("running", last_sign_at=time.time(), last_sign=key, last_error=None)
                logger.info("签到成功：%s（%s）", name, key)
                rank = response.get("studentRank")
                result = f"，您是第{rank}位" if str(rank).isdigit() and int(rank) > 0 else ""
                await self.notify_once("success:" + key, f"{course}签到成功（{method}）",
                                       f"{course}签到成功{result}。\n"
                                       f"签到方式为{method}。\n服务运行于 {service_ip(self.config)}。\n"
                                       f"{datetime.now().astimezone():%Y-%m-%d %H:%M:%S}")
                if mode == "qrcode":
                    # QR login changes the account session; renew once without resubmitting the signed task.
                    previous = self.open_id
                    self.open_id = ""
                    logger.info("二维码签到完成，更新后续轮询凭证")
                    await self.recover(reject=previous)
            else:
                code = response.get("errorCode") if isinstance(response, dict) else None
                message = response.get("msgClient", "") if isinstance(response, dict) else "返回结构异常"
                # Server messages can contain links; keep credentials out of logs and status.
                message = re.sub(r"[A-Za-z0-9_-]{28,}", "<redacted>", str(message))[:200]
                error = f"{key}，错误码 {code}，{message}"
                self.update_status("running", last_error=error)
                self.observation.event("attendance_failed", mode=mode, course_id=course_id,
                                       sign_id=sign_id, error_code=code)
                logger.error("签到未成功：%s；下次检查重试", error)
                if code not in (None, 408, 429) and not (isinstance(code, int) and code >= 500):
                    await self.notify_problem(key, f"签到 {name}（{key}）失败：错误码 {code}，{message}")

    async def iteration(self, *, dry_run=False):
        try:
            await self.tick(dry_run=dry_run)
        except AuthExpired as exc:
            from auth import Credential
            saved = self.provider.load()
            credential = saved if saved and saved.value == self.open_id else Credential(self.open_id, 0, "unknown")
            self.observation.expired(credential, exc.http_status, self.auth_trigger)
            self.update_status("auth_expired", last_auth_failure_at=time.time(),
                               last_auth_failure_http_status=exc.http_status)
            logger.warning("%s，自动重新获取并应用", exc)
            await self.recover(reject=self.open_id)
            await self.tick(dry_run=dry_run)

    async def verify_recovery(self):
        self.auth_trigger = "diagnostic"
        try:
            await self._verify_recovery()
        finally:
            self.auth_trigger = "automatic"

    async def _verify_recovery(self):
        self.update_status("recovery_testing", last_recovery_test_started_at=time.time(), last_error=None)
        await self.recover()
        before = fingerprint(self.open_id)
        # Ignore existing credentials so the diagnostic exercises issuance.
        if self.provider.mode == "wechat":
            known = await asyncio.to_thread(self.provider.cache.scan)
            self.provider.rejected.update(x.value for x in known)
        self.provider.rejected.add(self.open_id)
        self.open_id = "0" * 32
        self.last_identity_check = 0
        started = time.time()
        logger.info("恢复测试：使用无效的本地测试凭证，执行自动失效恢复路径")
        await self.iteration(dry_run=True)
        if self.status.get("last_auth_failure_at", 0) < started:
            raise RuntimeError("恢复测试失败：未收到服务端凭证失效响应")
        if fingerprint(self.open_id) == before:
            raise RuntimeError("恢复测试失败：没有取得新凭证")
        await self.tick(dry_run=True)
        evidence = {
            "checked_at": time.time(), "result": "passed", "student_id": self.identity,
            "trigger": "local_invalid_test_credential_rejected_by_server",
            "expired_response_http_status": self.status.get("last_auth_failure_http_status"),
            "before_fingerprint": before, "after_fingerprint": fingerprint(self.open_id),
            "identity_verified": True, "active_signs_checked": True,
            "attendance_submitted": False,
            "auth_mode": self.provider.mode, "credential_source": self.provider.last_source,
        }
        write_json(self.runtime / "recovery-test.json", evidence)
        self.observation.event("recovery_test_passed", before_fingerprint=before,
                               after_fingerprint=fingerprint(self.open_id), identity_verified=True,
                               polling_resumed=True)
        self.update_status("recovery_test_passed")
        logger.info("恢复测试通过：失效响应 → 获取新凭证 → 身份校验 → 应用 → 恢复轮询")


async def run(args, config, runtime):
    api = Api(config["ua"], timeout=config.get("request_timeout", 15))
    provider = AuthProvider(api, config["auth"], runtime)
    worker = Worker(config, runtime, api, provider)
    worker.observation.event("worker_started", pid=os.getpid(), auth_mode=provider.mode)
    worker.observation.save()
    stop = asyncio.Event()
    wake = asyncio.Event()
    refresh_requested = asyncio.Event()
    recovery_test_requested = asyncio.Event()
    loop = asyncio.get_running_loop()
    running_task = asyncio.current_task()

    def request_stop():
        stop.set()
        running_task.cancel()

    def request_action(event):
        event.set()
        wake.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, request_stop)
    loop.add_signal_handler(signal.SIGUSR1, request_action, refresh_requested)
    loop.add_signal_handler(signal.SIGUSR2, request_action, recovery_test_requested)
    caffeine = None
    if config["prevent_sleep"] and sys.platform == "darwin":
        sleep_flags = "-i" if provider.mode == "http" else "-di"
        caffeine = subprocess.Popen(["/usr/bin/caffeinate", sleep_flags, "-w", str(os.getpid())],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if args.verify_recovery:
            await worker.verify_recovery()
            return
        if args.check_auth:
            await worker.recover(force=args.refresh_auth)
            logger.info("学生 %s 的身份及凭证校验通过（%s 模式）", worker.identity, provider.mode)
            return
        if args.refresh_auth:
            await worker.recover(force=True)
        if args.once:
            await worker.iteration(dry_run=args.dry_run)
            return
        failures = 0
        while not stop.is_set():
            wake.clear()
            try:
                if recovery_test_requested.is_set():
                    recovery_test_requested.clear()
                    await worker.verify_recovery()
                if refresh_requested.is_set():
                    refresh_requested.clear()
                    await worker.recover(force=True)
                await worker.iteration(dry_run=args.dry_run)
                failures = 0
                delay = config["interval"] / 1000
            except (TransientApiError, RefreshError, ApiError) as exc:
                failures += 1
                delay = min(config["retry_max"], config["retry_initial"] * 2 ** min(failures - 1, 10))
                if isinstance(exc, (RefreshError, IdentityMismatch)):
                    delay = max(delay, 30)
                worker.update_status("retrying", last_error=str(exc), retry_in=delay)
                worker.observation.retry(exc, delay)
                logger.warning("%s；%s 秒后自动重试", exc, delay)
                if action_required(exc):
                    await worker.notify_problem("worker", exc)
            except Exception as exc:
                worker.update_status("crashed", last_error=type(exc).__name__)
                logger.exception("未预期错误，交由后台守护重启")
                await worker.notify_problem("crashed", "后台进程异常退出：" + type(exc).__name__)
                raise
            try:
                await asyncio.wait_for(wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
    except asyncio.CancelledError:
        if not stop.is_set():
            raise
    finally:
        await worker.flush_notifications()
        worker.update_status("stopped")
        worker.observation.event("worker_stopped", pid=os.getpid())
        await api.close()
        if caffeine is not None:
            caffeine.terminate()
            caffeine.wait(timeout=5)
        logger.info("脚本已停止")


def cli(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    parser.add_argument("--runtime-dir", type=Path, help="凭证、状态和日志目录；默认使用配置目录下的 .runtime")
    parser.add_argument("--once", action="store_true", help="检查一次后退出")
    parser.add_argument("--dry-run", action="store_true", help="只读检查，不提交签到")
    parser.add_argument("--check-auth", action="store_true", help="校验并应用凭证后退出")
    parser.add_argument("--refresh-auth", action="store_true", help="按配置的认证方式强制取得新凭证")
    parser.add_argument("--verify-recovery", action="store_true", help="实测服务端失效响应和自动续取；不提交签到")
    parser.add_argument("--status", action="store_true", help="读取后台状态，不访问微信或签到接口")
    parser.add_argument("--auth-report", action="store_true", help="读取凭证自然失效及续期统计，不发送网络请求")
    args = parser.parse_args(argv)
    config_path = args.config.expanduser().resolve()
    runtime = args.runtime_dir.expanduser().resolve() if args.runtime_dir else config_path.parent / ".runtime"
    if args.status:
        import json
        print(json.dumps(read_json(runtime / "status.json", {"phase": "not_started"}), ensure_ascii=False, indent=2))
        return 0
    config = load_config(config_path)
    if args.auth_report:
        import json
        report = AuthObservation(runtime, config["auth"]["expected_student_id"]).report()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    private_directory(runtime)
    file_log = RotatingFileHandler(runtime / "worker.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    os.chmod(runtime / "worker.log", 0o600)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[file_log, logging.StreamHandler()])
    logging.getLogger("httpx").setLevel(logging.WARNING)
    with ProcessLock(runtime / "process.lock"):
        ui_server = None
        if config["web"]["enabled"]:
            try:
                ui_server = start_server(runtime, config["web"]["host"], config["web"]["port"])
                logger.info("日志 UI 已启动：http://%s:%s/", config["web"]["host"], config["web"]["port"])
            except OSError as exc:
                logger.warning("日志 UI 启动失败（继续运行后台任务）：%s", exc)
        try:
            asyncio.run(run(args, config, runtime))
        finally:
            if ui_server is not None:
                ui_server.shutdown()
                ui_server.server_close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(cli())
    except (ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
