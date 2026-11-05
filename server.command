#!/usr/bin/env python3
"""Run the local server independently of the terminal that starts it."""

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
PID_FILE = DATA / "server.pid"
LOG_FILE = DATA / "server.log"
URL = "http://127.0.0.1:8765"
COMMAND = [
    str(ROOT / ".venv/bin/python"),
    "-m",
    "uvicorn",
    "app.main:create_app",
    "--factory",
    "--app-dir",
    str(ROOT),
    "--host",
    "127.0.0.1",
    "--port",
    "8765",
]


def server_pid():
    if not PID_FILE.exists():
        return None
    pid = int(PID_FILE.read_text().strip())
    result = subprocess.run(["ps", "-p", str(pid), "-o", "args="], capture_output=True, text=True)
    if result.returncode != 0:
        return None
    if result.stdout.strip() != " ".join(COMMAND):
        raise RuntimeError("PID 已被其他进程使用，未操作该进程。请检查 data/server.pid。")
    return pid


def ready():
    try:
        with urllib.request.urlopen(URL + "/api/auth/status", timeout=1) as response:
            status = json.load(response)
            return isinstance(status.get("initialized"), bool)
    except (urllib.error.URLError, TimeoutError, ValueError):
        return False


def start():
    pid = server_pid()
    if pid:
        if not ready():
            raise RuntimeError("服务进程存在但没有响应，请检查 " + str(LOG_FILE))
        print("后台已在运行：" + URL)
        return
    if not Path(COMMAND[0]).is_file():
        raise RuntimeError("缺少项目 Python 环境，请先在项目目录运行 uv sync --locked。")
    listener = subprocess.run(["lsof", "-nP", "-iTCP:8765", "-sTCP:LISTEN"], capture_output=True)
    if listener.returncode == 0:
        raise RuntimeError("8765 端口被其他进程占用，未启动或停止任何进程。")
    with LOG_FILE.open("ab") as log:
        log.write(b"\n--- Starting local QQ bot server ---\n")
        log.flush()
        child = subprocess.Popen(
            COMMAND,
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            close_fds=True,
        )
    PID_FILE.write_text(str(child.pid))
    for _ in range(40):
        if child.poll() is not None:
            raise RuntimeError("服务启动后退出，请检查 " + str(LOG_FILE))
        if ready():
            print("后台已启动：" + URL)
            print("日志：" + str(LOG_FILE))
            return
        time.sleep(0.25)
    raise RuntimeError("服务未在预期时间内就绪，请检查 " + str(LOG_FILE))


def stop():
    pid = server_pid()
    if pid is None:
        print("后台未运行。")
        return
    os.kill(pid, signal.SIGTERM)
    for _ in range(40):
        if server_pid() is None:
            PID_FILE.unlink(missing_ok=True)
            print("后台已停止。")
            return
        time.sleep(0.25)
    raise RuntimeError("进程尚未退出，请检查日志；没有强制终止。")


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "start"
    if action not in {"start", "stop", "status"}:
        raise RuntimeError("用法：./server.command [start|stop|status]")
    DATA.mkdir(exist_ok=True)
    with (DATA / "server.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if action == "start":
            start()
        elif action == "stop":
            stop()
        elif server_pid() and ready():
            print("后台运行正常：" + URL)
        else:
            raise RuntimeError("后台未运行或没有响应，请检查 " + str(LOG_FILE))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
