"""Install and manage this project's per-user macOS LaunchAgent."""

import argparse
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from auth import private_directory, read_json
from auth_observation import start_acceptance

ROOT = Path(__file__).resolve().parent
DEPLOY_ROOT = Path.home() / "Library" / "Application Support" / "wzj"
RUNTIME = DEPLOY_ROOT / ".runtime"
LABEL = "cn.teachermate.wzj"
DOMAIN = f"gui/{os.getuid()}"
TARGET = DOMAIN + "/" + LABEL
PLIST = Path.home() / "Library" / "LaunchAgents" / (LABEL + ".plist")
LAUNCHER_LOG = Path.home() / "Library" / "Logs" / "wzj" / "launcher.log"


def launchctl(*args, check=True):
    result = subprocess.run(["/bin/launchctl", *args], capture_output=True, text=True, timeout=30)
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or "launchctl 操作失败")
    return result


def installed():
    return launchctl("print", TARGET, check=False).returncode == 0


def bootstrap():
    # bootout can return while the previous process is still draining.
    for attempt in range(15):
        result = launchctl("bootstrap", DOMAIN, str(PLIST), check=False)
        if result.returncode == 0:
            return
        if attempt < 14:
            time.sleep(1)
    raise RuntimeError("后台守护加载失败：" + result.stderr.strip())


def wait_for_worker():
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        agent = launchctl("print", TARGET, check=False)
        pid = re.search(r"\bpid = (\d+)", agent.stdout)
        status = read_json(RUNTIME / "status.json", {})
        if pid and status.get("pid") == int(pid.group(1)) and status.get("phase") in (
            "running", "ready", "refreshing", "retrying",
        ):
            return
        time.sleep(0.5)
    raise RuntimeError("后台进程尚未就绪，检查 ~/Library/Logs/wzj/launcher.log")


def install(*, start=True):
    if installed():
        launchctl("bootout", TARGET)
    private_directory(DEPLOY_ROOT)
    private_directory(RUNTIME)
    source_runtime = ROOT / ".runtime"
    if source_runtime.exists() and not source_runtime.is_symlink():
        shutil.copytree(source_runtime, RUNTIME, dirs_exist_ok=True)
        backup = ROOT / ".runtime.original"
        if backup.exists():
            backup = ROOT / (".runtime." + str(int(time.time())) + ".original")
        source_runtime.rename(backup)
    if not source_runtime.exists():
        source_runtime.symlink_to(RUNTIME, target_is_directory=True)
    if source_runtime.resolve() != RUNTIME.resolve():
        raise RuntimeError("项目 .runtime 已指向另一目录，请先检查")
    for filename in ("main.py", "notify.py", "log_ui.py", "auth.py", "http_auth.py", "qr_feed.py", "auth_observation.py", "web_session.py", "wechat_ui.py",
                     "config.json", "requirements.txt"):
        shutil.copy2(ROOT / filename, DEPLOY_ROOT / filename)
    interpreter = DEPLOY_ROOT / ".venv" / "bin" / "python"
    venv_config = DEPLOY_ROOT / ".venv" / "pyvenv.cfg"
    needs_venv = not interpreter.exists() or (
        venv_config.exists() and str(ROOT) in venv_config.read_text()
    )
    if needs_venv:
        # Python 3.9 can report the calling virtualenv as _base_executable.
        # Use the real base interpreter so the deployment survives moving the
        # source project out of Downloads.
        base_python = Path(sys.base_prefix) / "bin" / f"python{sys.version_info.major}.{sys.version_info.minor}"
        subprocess.run([str(base_python), "-m", "venv", "--clear", str(DEPLOY_ROOT / ".venv")], check=True)
    subprocess.run([str(interpreter), "-m", "pip", "install", "-r", str(DEPLOY_ROOT / "requirements.txt")], check=True)
    private_directory(LAUNCHER_LOG.parent)
    LAUNCHER_LOG.touch(mode=0o600, exist_ok=True)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "Label": LABEL,
        "ProgramArguments": [str(interpreter), "-u", str(DEPLOY_ROOT / "main.py")],
        "WorkingDirectory": str(DEPLOY_ROOT),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "ExitTimeOut": 30,
        "LimitLoadToSessionType": "Aqua",
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"},
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": str(LAUNCHER_LOG),
    }
    PLIST.write_bytes(plistlib.dumps(data))
    PLIST.chmod(0o600)
    if start:
        launchctl("enable", TARGET)
        bootstrap()
        wait_for_worker()
        print("后台守护已安装并启动：" + LABEL)
    else:
        launchctl("disable", TARGET)
        print("代码和后台守护已部署，尚未启动：" + LABEL)


def show_status():
    agent = launchctl("print", TARGET, check=False)
    active = agent.returncode == 0
    status = read_json(RUNTIME / "status.json", {"phase": "not_started"})
    status["launch_agent_loaded"] = active
    status["process_running"] = active and bool(re.search(r"\bstate = running\b", agent.stdout))
    if not active:
        status["last_worker_phase"] = status.get("phase")
        status["phase"] = "stopped" if PLIST.exists() else "not_installed"
    status["deployment_path"] = str(DEPLOY_ROOT)
    config = read_json(DEPLOY_ROOT / "config.json", {})
    status["configured_auth_mode"] = config.get("auth", {}).get("mode", "http")
    heartbeat = status.get("heartbeat_at", 0)
    status["heartbeat_age_seconds"] = round(time.time() - heartbeat, 1) if heartbeat else None
    print(json.dumps(status, ensure_ascii=False, indent=2))


def verify_recovery():
    if not installed():
        raise RuntimeError("后台进程尚未启动")
    wait_for_worker()
    before = read_json(RUNTIME / "recovery-test.json", {}).get("checked_at", 0)
    requested_at = time.time()
    launchctl("kill", "SIGUSR2", TARGET)
    print("后台进程正在执行恢复测试（测试不提交签到）...", flush=True)
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        result = read_json(RUNTIME / "recovery-test.json", {})
        if result.get("checked_at", 0) > before:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        status = read_json(RUNTIME / "status.json", {})
        if (status.get("last_recovery_test_started_at", 0) >= requested_at
                and status.get("phase") in ("retrying", "crashed", "stopped")
                and status.get("last_error")):
            raise RuntimeError("后台恢复测试未完成：" + status["last_error"])
        time.sleep(1)
    raise RuntimeError("等待恢复测试超时；查看 .runtime/worker.log")


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "start", "stop", "restart", "status", "refresh", "verify-recovery", "observe-24h", "uninstall"))
    parser.add_argument("--no-start", action="store_true", help="install/restart 只部署代码，留待手动启动")
    args = parser.parse_args()
    if args.no_start and args.action not in ("install", "restart"):
        parser.error("--no-start 仅适用于 install/restart")
    if args.action == "install":
        install(start=not args.no_start)
    elif args.action == "start":
        if installed():
            launchctl("kickstart", TARGET)
        else:
            launchctl("enable", TARGET)
            bootstrap()
        wait_for_worker()
        print("后台进程已启动")
    elif args.action in ("stop", "uninstall"):
        launchctl("disable", TARGET)
        if installed():
            launchctl("bootout", TARGET)
        if args.action == "uninstall" and PLIST.exists():
            PLIST.unlink()
        print("后台进程已停止" if args.action == "stop" else "后台守护已卸载")
    elif args.action == "restart":
        install(start=not args.no_start)
    elif args.action == "refresh":
        wait_for_worker()
        launchctl("kill", "SIGUSR1", TARGET)
        print("已通知后台进程重新获取凭证")
    elif args.action == "verify-recovery":
        verify_recovery()
    elif args.action == "observe-24h":
        config = read_json(ROOT / "config.json", {})
        report = start_acceptance(RUNTIME, config["auth"]["expected_student_id"])
        install()
        print("24 小时验收记录：" + str(RUNTIME / "acceptance-24h.json"))
        print("截止时间：" + time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(report["ends_at"])))
    else:
        show_status()


if __name__ == "__main__":
    try:
        cli()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
