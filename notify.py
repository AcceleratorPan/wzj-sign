"""Best-effort ntfy and Xizhi notifications."""

import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)


class Notifier:
    def __init__(self, config, *, transport=None):
        self.config = config
        self.transport = transport

    async def send(self, title, content):
        if not self.config.get("enable", False):
            return
        requests = []
        xizhi_api = self.config.get("xizhi_api")
        if xizhi_api:
            requests.append(("息知", xizhi_api, {"json": {"title": title, "content": content}}))
        ntfy = self.config.get("ntfy", {})
        if ntfy.get("api"):
            url = ntfy["api"].rstrip("/")
            if ntfy.get("topic"):
                url += "/" + ntfy["topic"]
            headers = {"Title": title.encode("utf-8")}
            if ntfy.get("token"):
                headers["Authorization"] = "Bearer " + ntfy["token"]
            requests.append(("ntfy", url, {"content": content.encode("utf-8"), "headers": headers}))
        if not requests:
            return

        async with httpx.AsyncClient(timeout=10, trust_env=False, transport=self.transport) as client:
            async def post(channel, url, kwargs):
                for attempt in range(2):
                    try:
                        response = await client.post(url, **kwargs)
                        response.raise_for_status()
                        return
                    except Exception as exc:
                        if attempt:
                            logger.warning("%s 推送失败：%s", channel, type(exc).__name__)
                        else:
                            await asyncio.sleep(1)

            await asyncio.gather(*(post(*request) for request in requests))
