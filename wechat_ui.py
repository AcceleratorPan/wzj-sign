"""Open the existing WeChat official-account OAuth entry without sending messages."""

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import AppKit
import Foundation
import Quartz
import Vision


class UIError(RuntimeError):
    pass


@dataclass
class Window:
    name: str
    x: int
    y: int
    width: int
    height: int


@dataclass
class Text:
    value: str
    x: float
    y: float


def applescript(script):
    result = subprocess.run(
        ["/usr/bin/osascript", "-e", script], capture_output=True,
        text=True, timeout=15,
    )
    if result.returncode:
        raise UIError("微信界面不可访问；请检查登录状态及辅助功能权限")
    return result.stdout.strip()


def windows():
    output = applescript('''
tell application "System Events"
    tell process "WeChat"
        set resultRows to {}
        repeat with w in windows
            set windowName to name of w as text
            set {wx, wy} to position of w
            set {ww, wh} to size of w
            set end of resultRows to windowName & "|" & (wx as text) & "|" & (wy as text) & "|" & (ww as text) & "|" & (wh as text)
        end repeat
        set AppleScript's text item delimiters to linefeed
        return resultRows as text
    end tell
end tell
''')
    found = []
    for row in output.splitlines():
        parts = row.rsplit("|", 4)
        if len(parts) == 5:
            found.append(Window(parts[0], *(int(x) for x in parts[1:])))
    return found


def raise_window(window):
    name = json.dumps(window.name, ensure_ascii=False)
    applescript('tell application "WeChat" to activate')
    applescript('tell application "System Events" to tell process "WeChat" to set frontmost to true')
    applescript(
        'tell application "System Events" to tell process "WeChat" '
        'to perform action "AXRaise" of window ' + name
    )
    time.sleep(0.5)


def key(code=None, character=None):
    command = (
        "key code " + str(code) if code is not None else
        "keystroke " + json.dumps(character) + " using command down"
    )
    applescript('tell application "System Events" to tell process "WeChat" to ' + command)


def click(window, text):
    point = (window.x + text.x * window.width, window.y + text.y * window.height)
    for event in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
        Quartz.CGEventPost(
            Quartz.kCGHIDEventTap,
            Quartz.CGEventCreateMouseEvent(None, event, point, Quartz.kCGMouseButtonLeft),
        )
    time.sleep(0.35)


def recognize(window):
    if not Quartz.CGPreflightScreenCaptureAccess():
        Quartz.CGRequestScreenCaptureAccess()
        raise UIError("后台 Python 缺少屏幕录制权限；在系统设置中允许后自动重试")
    # Use the OS screenshot utility: WeChat's web content exposes no AX text.
    with tempfile.TemporaryDirectory(prefix="wzj-ui-") as tmp:
        screenshot = Path(tmp) / "window.png"
        region = f"{window.x},{window.y},{window.width},{window.height}"
        result = subprocess.run(
            ["/usr/sbin/screencapture", "-x", "-R" + region, str(screenshot)],
            capture_output=True, timeout=10,
        )
        if result.returncode or not screenshot.exists():
            raise UIError("无法读取微信窗口；请检查屏幕录制权限及锁屏状态")
        if os.environ.get("WZJ_UI_DEBUG"):
            diagnostic = Path(os.environ["WZJ_UI_DEBUG"]) / "ui-last.png"
            shutil.copyfile(screenshot, diagnostic)
            diagnostic.chmod(0o600)
        request = Vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        request.setRecognitionLanguages_(["zh-Hans", "en-US"])
        handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(
            Foundation.NSURL.fileURLWithPath_(str(screenshot)), {},
        )
        ok, error = handler.performRequests_error_([request], None)
        if not ok:
            raise UIError("微信窗口文字识别失败")
        found = []
        for observation in request.results() or []:
            box = observation.boundingBox()
            found.append(Text(
                str(observation.topCandidates_(1)[0].string()),
                box.origin.x + box.size.width / 2,
                1 - box.origin.y - box.size.height / 2,
            ))
        return found


@contextmanager
def preserved_desktop():
    app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    mouse = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    try:
        yield
    finally:
        Quartz.CGEventPost(
            Quartz.kCGHIDEventTap,
            Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, mouse,
                                         Quartz.kCGMouseButtonLeft),
        )
        if app is not None:
            app.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)


def paste_search(value):
    clipboard = AppKit.NSPasteboard.generalPasteboard()
    saved = [
        {str(t): bytes(item.dataForType_(t)) for t in item.types()
         if item.dataForType_(t) is not None}
        for item in clipboard.pasteboardItems() or []
    ]
    clipboard.clearContents()
    clipboard.setString_forType_(value, AppKit.NSPasteboardTypeString)
    written_change = clipboard.changeCount()
    try:
        key(character="v")
        time.sleep(0.5)
    finally:
        # Respect a concurrent clipboard change made by the user.
        if clipboard.changeCount() == written_change:
            clipboard.clearContents()
            items = []
            for values in saved:
                item = AppKit.NSPasteboardItem.alloc().init()
                for kind, data in values.items():
                    item.setData_forType_(Foundation.NSData.dataWithBytes_length_(data, len(data)), kind)
                items.append(item)
            if items:
                clipboard.writeObjects_(items)


def find_account(account):
    existing = next((w for w in windows() if w.name == account), None)
    if existing:
        raise_window(existing)
        key(code=53)
        return existing
    main = next((w for w in windows() if w.name == "微信"), None)
    if not main:
        raise UIError("找不到微信主窗口；微信必须保持登录")
    raise_window(main)
    key(code=53)
    # Cmd+F belongs to the embedded web page when a web pane is open. The
    # magnifier opens WeChat's local search regardless of that keyboard focus.
    click(main, Text("search", 160 / main.width, 30 / main.height))
    time.sleep(0.5)
    search = Window("local search", main.x + 135, main.y,
                    min(485, main.width - 135), main.height)
    search_confirmed = False
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if any((t.value.replace(" ", "").endswith("搜索") or
                account in t.value.replace(" ", "")) and t.y < 0.12
               for t in recognize(search)):
            search_confirmed = True
            break
        time.sleep(0.5)
    if not search_confirmed:
        raise UIError("未确认微信搜索框，停止本次自动操作")
    key(character="a")
    paste_search(account)
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        texts = recognize(search)
        headings = [t.y for t in texts if t.value.replace(" ", "") == "服务号"]
        network = [t.y for t in texts if "搜索网络结果" in t.value.replace(" ", "")]
        # OCR may append one or two glyphs for the blue verification badge.
        # Only select the service-account category above the network results.
        matches = [t for t in texts if t.value.replace(" ", "").startswith(account)
                   and len(t.value.replace(" ", "")) <= len(account) + 2
                   and any(y < t.y for y in headings)
                   and network and t.y < min(network)]
        if len(matches) == 1:
            click(search, matches[0])
            break
        time.sleep(0.5)
    else:
        key(code=53)
        raise UIError("微信搜索未找到已关注的微助教服务号")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        window = next((w for w in windows() if w.name == account), None)
        if window:
            raise_window(window)
            return window
        time.sleep(0.5)
    raise UIError("微助教服务号窗口未打开")


def refresh(account="微助教服务号"):
    with preserved_desktop():
        applescript('tell application "WeChat" to activate')
        time.sleep(0.5)
        account_window = find_account(account)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            matches = [t for t in recognize(account_window)
                       if "学生" in t.value and t.y > 0.85 and 0.25 < t.x < 0.65]
            if matches:
                click(account_window, matches[0])
                break
            time.sleep(0.5)
        else:
            raise UIError("服务号学生菜单不可用")
        choices = [t for t in recognize(account_window)
                   if t.value.startswith("全部") and t.y > 0.5 and 0.25 < t.x < 0.7]
        if len(choices) != 1:
            key(code=53)
            raise UIError("未找到唯一的学生全部入口")
        click(account_window, choices[0])
        # The caller waits for a NEW server-issued token and verifies student_id.
        return {"oauth_started": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", default="微助教服务号")
    args = parser.parse_args()
    try:
        print(json.dumps(refresh(args.account)))
    except UIError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
    except subprocess.TimeoutExpired:
        print(json.dumps({"error": "微信界面操作超时；检查系统授权弹窗、锁屏及微信状态"}, ensure_ascii=False))
        raise SystemExit(1)
