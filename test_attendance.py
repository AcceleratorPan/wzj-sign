"""Attendance protocol and identity checks, with no live requests."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

import httpx

from auth import Api, ApiError, AuthProvider, Credential, IdentityMismatch, RefreshError, TransientApiError, read_json, write_json
from auth_observation import AuthObservation, start_acceptance
from http_auth import HttpCredentialIssuer
from main import Worker, action_required
from notify import Notifier
from qr_feed import attendance_qr

TOKEN = "a" * 32
QR = "https://www.teachermate.com.cn/api/v1/qr/attendance/" + "b" * 96


class FakeQrSession:
    def __init__(self, rank=None):
        self.url = QR
        self.rank = rank

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def rank_for(self, student_id):
        return self.rank


class AttendanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        platform = patch("main.sys.platform", "darwin")
        platform.start()
        self.addCleanup(platform.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.runtime = Path(self.directory.name)
        write_json(self.runtime / "web-identity.json", {
            "student_id": 42, "source": "authenticated_web_profile",
            "raw_wechat_openid": "c" * 28,
        })
        self.calls = []
        self.signs = [
            {"courseId": 10, "signId": 1, "isGPS": 0, "isQR": 0},
            {"courseId": 10, "signId": 2, "isGPS": 1, "isQR": 0},
            {"courseId": 10, "signId": 3, "isGPS": 0, "isQR": 1},
        ]
        self.qr_result = {"openid": TOKEN, "success": "1", "studentRank": "1"}
        self.qr_session = FakeQrSession()
        self.role_id = 42
        self.reply = {"studentRank": 1}
        self.issue_count = 0
        self.fail_qr_refresh = False

        def handle(request):
            body = json.loads(request.content) if request.content else None
            self.calls.append((request.url.path, body))
            if request.url.path.endswith("/accounts/login"):
                if body["module"] == "s_qr_sign":
                    url = "https://v18.teachermate.cn/wechat-pro-ssr/student/signresult?" + urlencode(self.qr_result)
                else:
                    self.issue_count += 1
                    if self.fail_qr_refresh and self.issue_count > 1:
                        return httpx.Response(503, json={})
                    token = TOKEN if self.issue_count == 1 else "d" * 32
                    url = "https://v18.teachermate.cn/wechat-pro-ssr?openid=" + token
                return httpx.Response(200, json={"url": url})
            if request.url.path.endswith("/students/role"):
                return httpx.Response(200, json=[{"student_id": self.role_id}])
            if request.url.path.endswith("/active_signs"):
                return httpx.Response(200, json=self.signs)
            if request.url.path.endswith("/student-sign-in"):
                return httpx.Response(200, json=self.reply)
            raise AssertionError("Unexpected endpoint: " + request.url.path)

        transport = httpx.MockTransport(handle)
        self.api = Api(transport=transport)
        self.addAsyncCleanup(self.api.close)
        self.issuer = HttpCredentialIssuer(self.runtime, 42, transport=transport)
        self.config = {"auth": {"expected_student_id": 42}, "auth_check_interval": 60,
                       "wait": 0, "lat": 30.5, "lon": 114.4}
        provider = AuthProvider(self.api, self.config["auth"], self.runtime, issuer=self.issuer)
        self.worker = Worker(self.config, self.runtime, self.api, provider)

    async def test_all_modes_and_duplicate_poll(self):
        with patch("qr_feed.attendance_qr", return_value=self.qr_session) as scan:
            await self.worker.tick()
            await self.worker.tick()
        scan.assert_called_once_with(10, 3)
        posts = [body for path, body in self.calls if path.endswith("/student-sign-in")]
        self.assertEqual(posts, [{"courseId": 10, "signId": 1},
                                 {"courseId": 10, "signId": 2, "lat": 30.5, "lon": 114.4}])
        qr_posts = [body for path, body in self.calls if body and body.get("module") == "s_qr_sign"]
        self.assertEqual(len(qr_posts), 1)
        self.assertEqual(qr_posts[0]["extra"], "b" * 96)
        self.assertEqual(set(self.worker.signed), {"10:1", "10:2", "10:3"})
        events = [json.loads(line) for line in (self.runtime / "auth-events.jsonl").read_text().splitlines()]
        successes = [(e["mode"], e["sign_id"]) for e in events if e["event"] == "attendance_succeeded"]
        self.assertEqual(successes, [("normal", 1), ("gps", 2), ("qrcode", 3)])

    async def test_missing_qr_does_not_block_other_signs(self):
        self.signs = [self.signs[2], self.signs[0]]
        with patch("qr_feed.attendance_qr", side_effect=RuntimeError("二维码不可用")):
            await self.worker.tick()
        self.assertEqual(set(self.worker.signed), {"10:1"})

    async def test_qr_renews_once_after_success_and_reuses_new_credential(self):
        self.signs = [self.signs[2]]
        with patch("qr_feed.attendance_qr", return_value=self.qr_session):
            await self.worker.tick()
            renewed = self.worker.open_id
            for _ in range(3):
                await self.worker.tick()
        modules = [body["module"] for path, body in self.calls if path.endswith("/accounts/login")]
        self.assertEqual(modules, ["ssr_hub", "s_qr_sign", "ssr_hub"])
        self.assertEqual(renewed, "d" * 32)
        self.assertEqual(self.worker.open_id, renewed)
        self.assertIn(TOKEN, self.worker.provider.rejected)
        self.assertEqual(read_json(self.runtime / "credentials.json")["open_id"], renewed)

    async def test_qr_refresh_failure_does_not_resubmit_attendance(self):
        self.signs = [self.signs[2]]
        self.fail_qr_refresh = True
        with patch("qr_feed.attendance_qr", return_value=self.qr_session):
            with self.assertRaises(TransientApiError):
                await self.worker.tick()
            self.assertIn("10:3", self.worker.signed)
            self.assertEqual(self.worker.open_id, "")
            self.fail_qr_refresh = False
            await self.worker.tick()
        qr_posts = [body for path, body in self.calls if body and body.get("module") == "s_qr_sign"]
        self.assertEqual(len(qr_posts), 1)
        self.assertEqual(self.worker.open_id, "d" * 32)

    async def test_linux_worker_reads_qr_from_faye(self):
        self.signs = [self.signs[2]]
        with patch("main.sys.platform", "linux"), \
                patch("qr_feed.attendance_qr", return_value=self.qr_session) as read_qr:
            await self.worker.tick()
        read_qr.assert_called_once_with(10, 3)
        self.assertIn("10:3", self.worker.signed)

    async def test_expired_qr_is_retried(self):
        self.signs = [self.signs[2]]
        self.qr_result = {"openid": TOKEN, "success": "0", "message": "该二维码已过期"}
        with patch("qr_feed.attendance_qr", return_value=self.qr_session):
            await self.worker.tick()
            self.assertFalse(self.worker.signed)
            self.assertIn("该二维码已过期", self.worker.status["last_error"])
            self.qr_result["success"] = "1"
            await self.worker.tick()
        self.assertEqual(set(self.worker.signed), {"10:3"})
        self.assertIsNone(self.worker.status["last_error"])

    async def test_invalid_gps_never_submits(self):
        self.signs = [self.signs[1]]
        self.config["lat"] = 100
        await self.worker.tick()
        self.assertFalse(any(path.endswith("/student-sign-in") for path, _ in self.calls))
        self.assertFalse(self.worker.signed)

    async def test_repeat_success_and_business_failure(self):
        self.signs = [self.signs[0]]
        self.reply = {"errorCode": 306, "msgClient": "当前签到为二维码签到"}
        await self.worker.tick()
        self.assertFalse(self.worker.signed)
        self.reply = {"errorCode": 305}
        await self.worker.tick()
        self.assertEqual(set(self.worker.signed), {"10:1"})

    async def test_mismatched_qr_identity_never_submits(self):
        await self.worker.recover()
        self.signs = [self.signs[2]]
        self.role_id = 99
        with patch("qr_feed.attendance_qr", return_value=self.qr_session) as scan:
            await self.worker.tick()
        scan.assert_not_called()
        self.assertFalse(any(body and body.get("module") == "s_qr_sign" for _, body in self.calls))
        self.assertFalse(self.worker.signed)

    async def test_untrusted_qr_never_reaches_network(self):
        for value in [QR.replace("www.teachermate.com.cn", "example.com"), QR + "?x=1", None]:
            with self.assertRaises(RefreshError):
                await self.issuer.sign_in_qr(value)
        self.assertFalse(self.calls)

    async def test_bound_identity_mismatch_never_reaches_network(self):
        write_json(self.runtime / "web-identity.json", {
            "student_id": 99, "source": "authenticated_web_profile", "raw_wechat_openid": "c" * 28,
        })
        with self.assertRaises(IdentityMismatch):
            await self.issuer.sign_in_qr(QR)
        self.assertFalse(self.calls)

    async def test_rejects_invalid_result(self):
        for result in [{"openid": TOKEN, "success": "unexpected"},
                       {"openid": "invalid", "success": "1"}]:
            self.qr_result = result
            with self.assertRaises(RefreshError):
                await self.issuer.sign_in_qr(QR)

    async def test_qr_result_keeps_student_rank(self):
        _, response = await self.issuer.sign_in_qr(QR)
        self.assertEqual(response["studentRank"], "1")

    async def test_http_failure_keeps_error_code(self):
        await self.api.close()
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(403, json={"errorCode": 306}),
        ), base_url="https://v18.teachermate.cn") as client:
            self.api.client = client
            with self.assertRaisesRegex(ApiError, "403.*306"):
                await self.api.sign_in(TOKEN, {"courseId": 10, "signId": 3})

    async def test_notifications_are_deduplicated_and_transient_failures_are_quiet(self):
        self.config["push"] = {"enable": True, "service_ip": "121.40.120.90"}
        self.worker.notifier.config = self.config["push"]
        self.worker.notifier.send = AsyncMock()
        self.signs = [self.signs[0]]
        self.reply = {"errorCode": 503, "msgClient": "暂时不可用"}
        await self.worker.tick()
        await self.worker.tick()
        await self.worker.flush_notifications()
        self.assertEqual([call.args[0] for call in self.worker.notifier.send.await_args_list],
                         ["10开启签到（普通签到）"])
        found_content = self.worker.notifier.send.await_args_list[0].args[1]
        self.assertRegex(found_content, r"\n\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")
        self.assertNotIn("时间：", found_content)
        self.reply = {"studentRank": 1}
        await self.worker.tick()
        await self.worker.tick()
        await self.worker.flush_notifications()
        self.assertEqual([call.args[0] for call in self.worker.notifier.send.await_args_list],
                         ["10开启签到（普通签到）", "10签到成功（普通签到）"])
        content = self.worker.notifier.send.await_args_list[-1].args[1]
        self.assertRegex(content, r"\n\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")
        self.assertIn("10签到成功，您是第1位。", content)
        self.assertNotIn("时间：", content)
        self.assertIn("签到方式为普通签到。", content)
        self.assertIn("服务运行于 121.40.120.90。", content)
        restarted = Worker(self.config, self.runtime, self.api, self.worker.provider)
        restarted.notifier.send = AsyncMock()
        await restarted.tick()
        await restarted.flush_notifications()
        restarted.notifier.send.assert_not_awaited()

    async def test_qr_notification_uses_course_name_and_rank(self):
        self.config["push"] = {"enable": True, "service_ip": "121.40.120.90"}
        self.worker.notifier.config = self.config["push"]
        self.worker.notifier.send = AsyncMock()
        self.signs = [{**self.signs[2], "name": "test", "courseName": "计算机网络课程"}]
        self.qr_result = {"openid": TOKEN, "success": "1"}
        self.qr_session.rank = 1
        with patch("qr_feed.attendance_qr", return_value=self.qr_session):
            await self.worker.tick()
        await self.worker.flush_notifications()
        title, content = self.worker.notifier.send.await_args_list[-1].args
        self.assertEqual(title, "计算机网络签到成功（二维码签到）")
        self.assertIn("计算机网络签到成功，您是第1位。", content)
        self.assertIn("签到方式为二维码签到。", content)

    async def test_qr_success_without_rank_omits_rank_claim(self):
        self.config["push"] = {"enable": True, "service_ip": "121.40.120.90"}
        self.worker.notifier.config = self.config["push"]
        self.worker.notifier.send = AsyncMock()
        self.signs = [self.signs[2]]
        self.qr_result = {"openid": TOKEN, "success": "1"}
        with patch("qr_feed.attendance_qr", return_value=self.qr_session):
            await self.worker.tick()
        await self.worker.flush_notifications()
        title, content = self.worker.notifier.send.await_args_list[-1].args
        self.assertEqual(title, "10签到成功（二维码签到）")
        self.assertIn("10签到成功。", content)
        self.assertNotIn("名次", content)

    async def test_action_required_alerts_once_without_blocking_other_signs(self):
        self.config["push"] = {"enable": True}
        self.worker.notifier.config = self.config["push"]
        self.worker.notifier.send = AsyncMock()
        self.config["lat"] = 100
        self.signs = [self.signs[1], self.signs[0]]
        await self.worker.tick()
        await self.worker.tick()
        await self.worker.flush_notifications()
        titles = [call.args[0] for call in self.worker.notifier.send.await_args_list]
        self.assertEqual(titles.count("签到遇到问题，请检查"), 1)
        problem = next(call.args[1] for call in self.worker.notifier.send.await_args_list
                       if call.args[0] == "签到遇到问题，请检查")
        self.assertRegex(problem, r"\n\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")
        self.assertNotIn("时间：", problem)
        self.assertIn("10:1", self.worker.signed)

    async def test_slow_push_does_not_delay_attendance(self):
        self.config["push"] = {"enable": True}
        self.worker.notifier.config = self.config["push"]
        release = asyncio.Event()

        async def slow_send(*_):
            await release.wait()

        self.worker.notifier.send = slow_send
        self.signs = [self.signs[0]]
        await asyncio.wait_for(self.worker.tick(), timeout=0.5)
        self.assertIn("10:1", self.worker.signed)
        release.set()
        await self.worker.flush_notifications()


class NotificationTests(unittest.IsolatedAsyncioTestCase):
    def test_only_actionable_errors_alert(self):
        self.assertTrue(action_required(IdentityMismatch("学生不一致")))
        self.assertFalse(action_required(RefreshError("未收到本次签到二维码，稍后重试")))
        self.assertFalse(action_required(TransientApiError("API 网络异常")))

    async def test_ntfy_and_xizhi_payloads_and_retry(self):
        requests = []

        def handle(request):
            requests.append(request)
            if request.url.host == "xizhi.example" and len([r for r in requests if r.url.host == "xizhi.example"]) == 1:
                return httpx.Response(503)
            return httpx.Response(200)

        notifier = Notifier({"enable": True, "xizhi_api": "https://xizhi.example/send",
                             "ntfy": {"api": "https://ntfy.example", "topic": "wzj", "token": "secret"}},
                            transport=httpx.MockTransport(handle))
        await notifier.send("签到成功", "课程 10:1")
        self.assertEqual(len(requests), 3)
        ntfy = next(r for r in requests if r.url.host == "ntfy.example")
        self.assertEqual(ntfy.url.path, "/wzj")
        self.assertEqual(ntfy.headers["authorization"], "Bearer secret")
        self.assertEqual(ntfy.headers["title"], "签到成功")
        self.assertEqual(ntfy.content, "课程 10:1".encode())
        self.assertEqual(json.loads(requests[-1].content), {"title": "签到成功", "content": "课程 10:1"})


class QrFeedTests(unittest.IsolatedAsyncioTestCase):
    class Socket:
        def __init__(self, messages):
            self.messages = iter(messages)
            self.sent = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def send(self, message):
            self.sent.extend(json.loads(message))

        async def recv(self):
            return await self.__anext__()

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return json.dumps([next(self.messages)])
            except StopIteration:
                await asyncio.Event().wait()

    async def test_reads_only_subscribed_course_and_sign(self):
        channel = "/attendance/10/3/qr"
        socket = self.Socket([
            {"channel": "/meta/handshake", "successful": True, "clientId": "client"},
            {"channel": "/meta/subscribe", "successful": True, "subscription": channel},
            {"channel": "/attendance/11/3/qr", "data": {"type": 1, "qrUrl": "wrong"}},
            {"channel": channel, "data": {"type": 3, "student": {"id": 42, "rank": 1}}},
            {"channel": channel, "data": {"type": 1, "qrUrl": QR}},
        ])
        with patch("qr_feed.websockets.connect", return_value=socket) as connect:
            async with attendance_qr(10, 3) as qr:
                self.assertEqual(qr.url, QR)
                self.assertEqual(await qr.rank_for(42), 1)
        connect.assert_called_once_with("wss://www.teachermate.com.cn/faye")
        self.assertEqual([message["channel"] for message in socket.sent],
                         ["/meta/handshake", "/meta/connect", "/meta/subscribe"])
        self.assertEqual(socket.sent[-1]["subscription"], channel)

    async def test_rejected_subscription_and_no_qr(self):
        channel = "/attendance/10/3/qr"
        rejected = self.Socket([
            {"channel": "/meta/handshake", "successful": True, "clientId": "client"},
            {"channel": "/meta/subscribe", "successful": False, "subscription": channel},
        ])
        with patch("qr_feed.websockets.connect", return_value=rejected):
            with self.assertRaisesRegex(ApiError, "订阅被拒绝"):
                async with attendance_qr(10, 3):
                    pass
        silent = self.Socket([
            {"channel": "/meta/handshake", "successful": True, "clientId": "client"},
            {"channel": "/meta/subscribe", "successful": True, "subscription": channel},
        ])
        with patch("qr_feed.websockets.connect", return_value=silent):
            with self.assertRaisesRegex(RefreshError, "未收到本次签到二维码"):
                async with attendance_qr(10, 3, timeout=0.01):
                    pass

    async def test_rank_comes_only_from_matching_student(self):
        channel = "/attendance/10/3/qr"
        socket = self.Socket([
            {"channel": "/meta/handshake", "successful": True, "clientId": "client"},
            {"channel": "/meta/subscribe", "successful": True, "subscription": channel},
            {"channel": channel, "data": {"type": 1, "qrUrl": QR}},
            {"channel": channel, "data": {"type": 3, "student": {"id": 99, "rank": 2}}},
            {"channel": channel, "data": {"type": 3, "student": {"id": 42, "rank": 3}}},
        ])
        with patch("qr_feed.websockets.connect", return_value=socket):
            async with attendance_qr(10, 3) as qr:
                self.assertEqual(await qr.rank_for(42), 3)
                self.assertIsNone(await qr.rank_for(77, timeout=0.01))


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.runtime = Path(self.directory.name)
        self.clock = patch("auth_observation.time.time", return_value=100)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        start_acceptance(self.runtime, 42)
        self.observer = AuthObservation(self.runtime, 42)

    def report(self):
        return read_json(self.runtime / "acceptance-24h.json")

    def test_natural_recovery_and_diagnostic_are_separate(self):
        old = Credential(TOKEN, 100, "http_account_login")
        new = Credential("d" * 32, 110, "http_account_login")
        self.observer.applied(old, "automatic")
        self.observer.api_success(old.value)
        self.now.return_value = 110
        self.observer.expired(old, 401, "automatic")
        self.observer.expired(old, 401, "automatic")
        self.observer.applied(new, "automatic")
        self.observer.api_success(new.value, polling=False)
        self.assertFalse(self.report()["checks"]["automatic_recovery_observed"])
        self.observer.api_success(new.value)
        self.assertEqual(self.report()["automatic_expiries"], 1)
        self.assertEqual(self.report()["automatic_recoveries"], 1)
        self.assertTrue(self.report()["checks"]["automatic_recovery_observed"])
        self.observer.expired(new, 401, "diagnostic")
        self.observer.applied(old, "diagnostic")
        self.observer.api_success(old.value)
        self.observer.event("recovery_test_passed")
        self.assertEqual(self.report()["automatic_expiries"], 1)
        self.assertEqual(self.report()["automatic_recoveries"], 1)
        self.assertTrue(self.report()["checks"]["diagnostic_recovery_passed"])
        self.assertNotIn(TOKEN, (self.runtime / "acceptance-24h.json").read_text())

    def test_window_finishes_and_survives_worker_restart(self):
        self.observer.api_success(TOKEN)
        for index, mode in enumerate(("normal", "gps", "qrcode")):
            self.observer.event("attendance_succeeded", mode=mode, course_id=10, sign_id=index)
        self.now.return_value = 120
        self.observer = AuthObservation(self.runtime, 42)
        self.observer.api_success(TOKEN)
        self.assertEqual(self.report()["poll_count"], 2)
        self.assertEqual(self.report()["max_poll_gap_seconds"], 20)
        self.assertEqual(self.report()["sign_counts"], {"normal": 1, "gps": 1, "qrcode": 1})
        self.assertTrue(self.report()["checks"]["three_sign_modes_observed"])
        self.assertFalse(self.report()["checks"]["automatic_recovery_observed"])
        self.now.return_value = 86501
        self.observer.api_success(TOKEN)
        completed = self.report()
        self.assertEqual(completed["status"], "complete")
        self.assertEqual(completed["observed_seconds"], 86400)
        self.assertEqual(completed["poll_count"], 2)
        self.observer.event("attendance_succeeded", mode="normal", course_id=10, sign_id=99)
        self.assertEqual(self.report(), completed)

    def test_repeat_start_preserves_active_window(self):
        self.observer.api_success(TOKEN)
        before = self.report()
        self.now.return_value = 120
        self.assertEqual(start_acceptance(self.runtime, 42), before)
        self.assertEqual(self.report(), before)


if __name__ == "__main__":
    unittest.main()
