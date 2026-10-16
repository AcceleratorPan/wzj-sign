"""Export the current user's TeacherMate web login without exposing secrets."""

import argparse
import asyncio
import base64
import json
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx

from auth import (
    ApiError, AuthExpired, IdentityMismatch,
    fingerprint, read_json, write_json,
)


RUNTIME = Path(__file__).resolve().parent / ".runtime"


def trusted_origin(url):
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not any(
        host == domain or host.endswith("." + domain)
        for domain in ("teachermate.cn", "teachermate.com.cn")
    ):
        raise RuntimeError("只允许读取微助教官网标签页")
    return "https://" + host


def chrome_tabs():
    script = '''tell application "Google Chrome"
set resultText to ""
repeat with w in windows
repeat with t in tabs of w
set resultText to resultText & (id of w as text) & "|" & (id of t as text) & "|" & (URL of t as text) & linefeed
end repeat
end repeat
return resultText
end tell'''
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("无法读取 Chrome 标签页")
    tabs = []
    for line in result.stdout.splitlines():
        parts = line.split("|", 2)
        if len(parts) != 3:
            continue
        try:
            origin = trusted_origin(parts[2])
        except RuntimeError:
            continue
        tabs.append({"window": int(parts[0]), "tab": int(parts[1]), "origin": origin})
    return tabs


def browser_js(tab, javascript):
    # The script is passed as an argv item, never interpolated into a shell.
    script = (
        'tell application "Google Chrome" to execute tab id '
        + str(tab["tab"]) + " of window id " + str(tab["window"])
        + " javascript " + json.dumps(javascript, ensure_ascii=False)
    )
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("Chrome JavaScript 读取失败；检查允许 Apple 事件中的 JavaScript")
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise RuntimeError("Chrome 返回格式异常") from exc


def page_js(tab, javascript):
    # Chrome Apple Events run in an isolated world. A temporary script enters
    # the page's world so that its Faye instance and callbacks are accessible.
    source = (
        "try {document.documentElement.dataset.wzjPageResult=(" + javascript
        + ");} catch (_) {document.documentElement.dataset.wzjPageResult="
        + "JSON.stringify({error:'page_script_failed'});}"
    )
    wrapper = (
        "(()=>{if(location.protocol!=='https:'||!/(^|\\.)teachermate\\.(cn|com\\.cn)$/.test(location.hostname))"
        + "return JSON.stringify({error:'not_teachermate_page'});"
        + "const s=document.createElement('script');s.textContent=" + json.dumps(source)
        + ";document.documentElement.appendChild(s);s.remove();"
        + "const value=document.documentElement.dataset.wzjPageResult;"
        + "delete document.documentElement.dataset.wzjPageResult;"
        + "return value||JSON.stringify({error:'page_script_blocked'});})()"
    )
    return browser_js(tab, wrapper)


def decode_cookie(value):
    value = unquote(value)
    try:
        decoded = json.loads(value)
        return decoded if isinstance(decoded, str) else value
    except ValueError:
        return value


def snapshot(tab):
    javascript = r'''(() => {
      if(location.protocol !== "https:" || !/(^|\.)teachermate\.(cn|com\.cn)$/.test(location.hostname))
        return JSON.stringify({error:"not_teachermate_page"});
      const readStorage = storage => Object.fromEntries(Object.keys(storage)
        .filter(key => /^(token|authorization|CIAuthorization|open_?id|session|loginStatus|ifAutoLogin)$/i.test(key))
        .map(key => [key, storage.getItem(key)]));
      let capture = null;
      try { capture = JSON.parse(sessionStorage.getItem("wzj.webLoginCapture")); } catch (_) {}
      const monitor = JSON.parse(sessionStorage.getItem("wzj.webMonitorStatus") || "{}");
      return JSON.stringify({origin:location.origin, path:location.pathname,
        cookie:document.cookie, local_storage:readStorage(localStorage),
        session_storage:readStorage(sessionStorage), login_capture:capture,
        monitor_ready:!!monitor.ready, monitor_failed:!!monitor.failed});
    })()'''
    data = browser_js(tab, javascript)
    if data.get("error"):
        raise RuntimeError("标签页已离开微助教官网，停止读取")
    trusted_origin(data["origin"])
    cookies = {}
    for item in data.pop("cookie", "").split(";"):
        name, separator, value = item.strip().partition("=")
        if separator:
            cookies[name] = value
    data["cookies"] = cookies
    data["captured_at"] = time.time()
    return data


def credential_fields(data):
    values = []
    for section in (data.get("cookies", {}), data.get("local_storage", {}), data.get("session_storage", {})):
        for name, value in section.items():
            if name.lower() in ("token", "authorization", "ciauthorization", "openid", "open_id") and value:
                values.append((name, decode_cookie(value)))
    capture = data.get("login_capture") or {}
    payload = capture.get("data") or {}
    if isinstance(payload, dict):
        for name in ("token", "authorization", "CIAuthorization", "openid", "openId"):
            if isinstance(payload.get(name), str) and payload[name]:
                values.append((name, payload[name]))
    return values


def safe_status(data):
    capture = data.get("login_capture") or {}
    payload = capture.get("data") or {}
    return {
        "origin": data["origin"], "path": data["path"],
        "cookie_names": sorted(data.get("cookies", {})),
        "credential_fields": [{"name": key, "length": len(value), "fingerprint": fingerprint(value)}
                              for key, value in credential_fields(data)],
        "scan_received": bool(capture),
        "scan_success": payload.get("success") if isinstance(payload, dict) else None,
        "teacher_enabled": payload.get("teacherEnable") if isinstance(payload, dict) else None,
        "monitor_ready": data.get("monitor_ready", False),
        "monitor_failed": data.get("monitor_failed", False),
    }


def capture_session(tab, path):
    data = snapshot(tab)
    status = safe_status(data)
    if credential_fields(data):
        write_json(path, data)
        status["saved"] = True
    else:
        status["saved"] = False
    return status


def install_monitor(tab):
    javascript = r'''(() => {
      if (typeof Faye === "undefined") return JSON.stringify({installed:false,reason:"faye_library_missing"});
      if (!localStorage.uuid) return JSON.stringify({installed:false,reason:"login_uuid_missing"});
      if (!window.__wzjOriginalFayeReceive) {
        const original = Faye.Client.prototype.receiveMessage;
        if (typeof original === "function") {
          window.__wzjOriginalFayeReceive = original;
          Faye.Client.prototype.receiveMessage = function(message) {
            try {
              if (message.channel === "/login/" + localStorage.uuid && message.data)
                sessionStorage.setItem("wzj.webLoginCapture", JSON.stringify({captured_at:Date.now()/1000,data:message.data}));
            } catch (_) {}
            return original.apply(this, arguments);
          };
        }
      }
      if (window.__wzjLoginSubscription && window.__wzjMonitoredUuid === localStorage.uuid)
        return JSON.stringify({installed:true,existing:true});
      if (window.__wzjLoginSubscription) window.__wzjLoginSubscription.cancel();
      const client = window.__wzjLoginMonitor || new Faye.Client(location.origin + "/faye", {timeout:15});
      window.__wzjLoginMonitor = client;
      window.__wzjMonitoredUuid = localStorage.uuid;
      sessionStorage.setItem("wzj.webMonitorStatus", JSON.stringify({ready:false,failed:false}));
      const subscription = client.subscribe("/login/" + localStorage.uuid, data => {
        sessionStorage.setItem("wzj.webLoginCapture", JSON.stringify({captured_at:Date.now()/1000,data}));
      });
      window.__wzjLoginSubscription = subscription;
      subscription.then(() => {sessionStorage.setItem("wzj.webMonitorStatus", JSON.stringify({ready:true,failed:false}));},
        () => {sessionStorage.setItem("wzj.webMonitorStatus", JSON.stringify({ready:false,failed:true}));});
      return JSON.stringify({installed:true});
    })()'''
    return page_js(tab, javascript)


def web_token_claims(token):
    """Decode for identity selection; the server must validate the token first."""
    bare = re.sub(r"^(?:JWT|Bearer)\s+", "", token, flags=re.I)
    try:
        header, payload, signature = bare.split(".")
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, UnicodeError) as exc:
        raise RuntimeError("网页登录 token 不是可解析的 JWT") from exc
    if not isinstance(claims, dict):
        raise RuntimeError("网页登录 JWT 的内容格式异常")
    return claims


async def inspect_session(path, expected_student_id, *, transport=None, runtime=RUNTIME):
    """Read the bound WeChat identifier over HTTP, never launching a client."""
    data = read_json(path)
    if not isinstance(data, dict):
        raise RuntimeError("尚未保存网页登录凭证")
    origin = trusted_origin(data["origin"])
    token = dict(credential_fields(data)).get("token")
    if not token:
        raise RuntimeError("保存的会话缺少网页登录 token")
    async with httpx.AsyncClient(
        base_url=origin, headers={"authorization": token, "Accept": "application/json"},
        trust_env=False, timeout=15,
        transport=transport or httpx.AsyncHTTPTransport(retries=2, trust_env=False),
    ) as client:
        try:
            response = await client.get("/api/v1/authentications")
            if response.status_code == 401:
                raise AuthExpired("网页登录凭证已失效 (HTTP 401)", http_status=401)
            if response.status_code != 200:
                raise ApiError(f"网页登录验证失败 (HTTP {response.status_code})")
            claims = web_token_claims(token)
            if str(claims.get("studentId")) != str(expected_student_id):
                raise IdentityMismatch("网页登录对应的学生与指定学生不一致，拒绝读取或保存身份")
            teacher_id = claims.get("id")
            if not isinstance(teacher_id, (int, str)) or not str(teacher_id).isdigit():
                raise ApiError("网页登录凭证缺少有效的账号身份")
            response = await client.get("/api/v1/teachers/" + str(teacher_id))
            if response.status_code != 200:
                raise ApiError(f"账号资料读取失败 (HTTP {response.status_code})")
            try:
                profile = response.json().get("teacher")
            except (ValueError, AttributeError) as exc:
                raise ApiError("账号资料返回格式异常") from exc
            if not isinstance(profile, dict) or str(profile.get("id")) != str(teacher_id):
                raise IdentityMismatch("账号资料与已验证的网页登录身份不一致")
            raw_openid = profile.get("openid")
            if not isinstance(raw_openid, str) or not raw_openid:
                raise ApiError("账号资料未返回绑定的微信 OpenID")
            write_json(Path(runtime) / "web-identity.json", {
                "raw_wechat_openid": raw_openid, "student_id": expected_student_id,
                "origin": origin, "captured_at": time.time(),
                "source": "authenticated_web_profile",
            })
            return {
                "web_login_valid": True, "student_id": expected_student_id,
                "web_role": "teacher_management",
                "desktop_courses": profile.get("courseNum"),
                "raw_wechat_openid_saved": True,
                "raw_wechat_openid_length": len(raw_openid),
                "raw_wechat_openid_fingerprint": fingerprint(raw_openid),
                "has_wechat_binding": profile.get("hasBindWithWeChat") is True,
                "has_verified_email": profile.get("mailVerify") == 1,
            }
        except httpx.RequestError as exc:
            raise ApiError("网页版网络请求失败：" + type(exc).__name__) from exc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "monitor", "capture", "watch", "inspect"))
    parser.add_argument("--tab-id", type=int)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--session", type=Path, default=RUNTIME / "web-session.json")
    args = parser.parse_args()
    try:
        if args.action == "inspect":
            config = read_json(Path(__file__).resolve().parent / "config.json")
            print(json.dumps(asyncio.run(inspect_session(args.session, config["auth"]["expected_student_id"])), ensure_ascii=False))
            return
        tabs = chrome_tabs()
        if args.tab_id is not None:
            tabs = [t for t in tabs if t["tab"] == args.tab_id]
        if not tabs:
            raise RuntimeError("Chrome 中未找到微助教官网标签页")
        tab = tabs[-1]
        if args.action == "monitor":
            print(json.dumps(install_monitor(tab), ensure_ascii=False))
        elif args.action == "status":
            print(json.dumps(safe_status(snapshot(tab)), ensure_ascii=False))
        else:
            deadline = time.monotonic() + args.timeout
            while True:
                status = capture_session(tab, args.session)
                if status["saved"] or args.action != "watch" or time.monotonic() >= deadline:
                    print(json.dumps(status, ensure_ascii=False))
                    break
                time.sleep(2)
    except (RuntimeError, ApiError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
