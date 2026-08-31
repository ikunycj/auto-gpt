# -*- coding: utf-8 -*-
"""
WebUI 启动入口。

用法：
    uv run --locked python web.py                 # 后端 http://127.0.0.1:6666，仅本地访问
    uv run --locked python web.py --open-browser  # 前端已运行时打开 http://127.0.0.1:5555
    前端固定为 http://127.0.0.1:5555；请使用 ./webui.sh 同时管理前后端。

与 CLI（uv run --locked python main.py）完全平行，互不影响。
"""
import argparse
import logging
import os
import tempfile
import webbrowser
from pathlib import Path
from threading import Timer

from apps.web.app import create_app
from core import sqlite_store


_PROJECT_ROOT = Path(__file__).resolve().parent
_WEBUI_LOG_PATH = _PROJECT_ROOT / "data" / "runtime" / "webui.log"
_WEBUI_LOG_CATEGORY = "webui_process_logs"
WEBUI_HOST = "127.0.0.1"
FRONTEND_PORT = 5555
BACKEND_PORT = 6666
FRONTEND_URL = f"http://{WEBUI_HOST}:{FRONTEND_PORT}"
BACKEND_URL = f"http://{WEBUI_HOST}:{BACKEND_PORT}"


def _acquire_single_instance(port: int):
    """持有跨进程文件锁，防止同一端口启动多个 WebUI 实例。"""
    lock_path = Path(tempfile.gettempdir()) / f"turb-gpt-free-register-web-{int(port)}.lock"
    handle = lock_path.open("a+", encoding="utf-8")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write("0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, IOError) as exc:
        handle.close()
        raise RuntimeError(f"端口 {port} 的 WebUI 已在运行") from exc
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def _release_single_instance(handle) -> None:
    if handle is None:
        return
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (OSError, IOError):
        pass
    handle.close()


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    # Process logs are authoritative in SQLite; the path is a logical key and
    # ``mirror=False`` prevents a second physical log store.
    sqlite_handler = sqlite_store.SQLiteFileHandler(
        _WEBUI_LOG_PATH,
        category=_WEBUI_LOG_CATEGORY,
        mirror=False,
    )
    sqlite_handler.setLevel(level)
    sqlite_handler.setFormatter(formatter)
    root.addHandler(sqlite_handler)

    # Keep foreground ``uv run --locked python web.py`` useful without making stdout the
    # background service's storage mechanism.
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(level)
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="GPT 注册 WebUI 控制台")
    parser.add_argument("--host", default=WEBUI_HOST, help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=BACKEND_PORT, help=argparse.SUPPRESS)
    parser.add_argument("--open-browser", action="store_true", help="前端已运行时打开浏览器")
    parser.add_argument("--verbose", action="store_true", help="详细日志")
    args = parser.parse_args()

    if args.host != WEBUI_HOST or args.port != BACKEND_PORT:
        parser.error(f"WebUI 后端仅支持 {BACKEND_URL}")

    _setup_logging(args.verbose)
    logger = logging.getLogger(__name__)

    try:
        instance_lock = _acquire_single_instance(args.port)
    except RuntimeError as exc:
        logger.error(str(exc))
        raise SystemExit(2) from exc

    app = create_app()
    logger.info("WebUI 后端已启动：%s", BACKEND_URL)
    # 默认不自动打开浏览器；需要时显式传 --open-browser
    if args.open_browser:
        Timer(1.0, lambda: webbrowser.open(FRONTEND_URL)).start()

    # debug=False：避免 reloader 双进程导致线程池/定时器重复
    try:
        app.run(host=args.host, port=args.port, debug=False, threaded=True)
    finally:
        _release_single_instance(instance_lock)


if __name__ == "__main__":
    main()
