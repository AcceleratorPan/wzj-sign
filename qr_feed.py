"""Read attendance QR URLs and student ranks from TeacherMate's Faye channel."""

import asyncio
import json
from contextlib import asynccontextmanager

import websockets

from auth import ApiError, RefreshError, TransientApiError


ENDPOINT = "wss://www.teachermate.com.cn/faye"


class QrSession:
    def __init__(self, ws, channel):
        self.ws = ws
        self.channel = channel
        self.sequence = 0
        self.subscribed = False
        self.ranks = {}
        self.url = None

    async def send(self, message):
        self.sequence += 1
        await self.ws.send(json.dumps([{**message, "id": str(self.sequence)}]))

    async def receive(self):
        try:
            messages = json.loads(await self.ws.recv())
        except ValueError:
            raise ApiError("二维码频道返回结构异常") from None
        if not isinstance(messages, list):
            raise ApiError("二维码频道返回结构异常")
        qr_url = None
        for message in messages:
            if not isinstance(message, dict):
                raise ApiError("二维码频道返回结构异常")
            name = message.get("channel")
            if name == "/meta/handshake":
                if not message.get("successful") or not message.get("clientId"):
                    raise ApiError("二维码频道握手被拒绝")
                client_id = message["clientId"]
                await self.send({"channel": "/meta/connect", "clientId": client_id,
                                 "connectionType": "websocket"})
                await self.send({"channel": "/meta/subscribe", "clientId": client_id,
                                 "subscription": self.channel})
            elif name == "/meta/subscribe" and message.get("subscription") == self.channel:
                if not message.get("successful"):
                    raise ApiError("二维码频道订阅被拒绝")
                self.subscribed = True
            elif name == self.channel and self.subscribed:
                data = message.get("data")
                if not isinstance(data, dict):
                    continue
                if data.get("type") == 1 and isinstance(data.get("qrUrl"), str):
                    qr_url = data["qrUrl"]
                elif data.get("type") == 3 and isinstance(data.get("student"), dict):
                    student = data["student"]
                    rank = student.get("rank")
                    if student.get("id") is not None and str(rank).isdigit() and int(rank) > 0:
                        self.ranks[str(student["id"])] = int(rank)
        return qr_url

    async def wait_for_qr(self):
        while not self.url:
            self.url = await self.receive()
        return self.url

    async def rank_for(self, student_id, *, timeout=4):
        student_id = str(student_id)

        async def wait():
            while student_id not in self.ranks:
                await self.receive()
            return self.ranks[student_id]

        try:
            return await asyncio.wait_for(wait(), timeout)
        except (asyncio.TimeoutError, OSError, websockets.exceptions.WebSocketException, ApiError):
            return None


@asynccontextmanager
async def attendance_qr(course_id, sign_id, *, timeout=35):
    channel = f"/attendance/{course_id}/{sign_id}/qr"
    try:
        async with websockets.connect(ENDPOINT) as ws:
            session = QrSession(ws, channel)
            await session.send({"channel": "/meta/handshake", "version": "1.0",
                                "supportedConnectionTypes": ["websocket"]})
            try:
                await asyncio.wait_for(session.wait_for_qr(), timeout)
            except asyncio.TimeoutError:
                raise RefreshError("未收到本次签到二维码，稍后重试") from None
            yield session
    except (OSError, websockets.exceptions.WebSocketException) as exc:
        raise TransientApiError("二维码频道网络异常：" + type(exc).__name__) from None
