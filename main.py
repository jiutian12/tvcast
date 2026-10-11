"""TV投屏 入口。

用法：
  python main.py                     # 图形界面（同时开网页遥控服务）
  python main.py --web               # 只跑服务端，不开窗口（适合丢后台/开机自启）
  python main.py --root "D:\\Videos"  # 指定媒体目录（默认 ~/Videos）
  python main.py --port 18632        # 指定端口（默认 18632，被占用则随机）
  python main.py --scan              # 命令行扫设备
  python main.py --cast "文件" --device 1   # 命令行直接推
"""

from __future__ import annotations

import argparse
import os
import sys
import time

if getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(sys.executable))

from tvcast.core import CastCore          # noqa: E402
from tvcast.httpserver import DEFAULT_PORT, TvCastServer  # noqa: E402
from tvcast.util import default_media_dir, get_local_ip  # noqa: E402

DEFAULT_ROOT = default_media_dir()


def _log(msg: str):
    print(msg, flush=True)


def cmd_scan():
    from tvcast.discovery import discover_all
    ip = get_local_ip()
    print(f"本机 IP: {ip}")
    print("正在扫描（DLNA + Chromecast）…")
    devs = discover_all(ip, log=_log)
    if not devs:
        print("未发现可投屏设备。")
        return 1
    print(f"\n发现 {len(devs)} 台：")
    for i, d in enumerate(devs, 1):
        print(f"  {i}. {d}")
    return 0


def cmd_web(root: str, port: int, do_scan: bool = True):
    ip = get_local_ip()
    core = CastCore(root, ip, log=_log)
    srv = TvCastServer(core, root, port=port)
    core.server = srv
    srv.start()

    print("=" * 56)
    print(f"  网页遥控（手机浏览器打开）: {srv.web_url(ip)}")
    print(f"  媒体目录: {root}")
    print(f"  监听端口: {srv.port}")
    print("=" * 56)
    print("保持本窗口打开，Ctrl+C 退出\n")

    if do_scan:
        threading_scan = __import__("threading").Thread(target=core.scan, daemon=True)
        threading_scan.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n退出中…")
    finally:
        srv.stop()
    return 0


def cmd_cast(path: str, index: int, port: int):
    if not os.path.isfile(path):
        print(f"文件不存在: {path}")
        return 2
    ip = get_local_ip()
    folder = os.path.dirname(os.path.abspath(path))
    core = CastCore(folder, ip, log=_log)
    srv = TvCastServer(core, folder, port=port)
    core.server = srv
    srv.start()
    print(f"服务: {srv.web_url(ip)}")

    devs = core.scan()
    if not devs:
        print("未发现设备")
        return 1
    if not (1 <= index <= len(devs)):
        print(f"设备序号应在 1..{len(devs)}")
        return 2
    core.select_device(devs[index - 1].key)
    r = core.cast(path)
    print("结果:", r)
    if not r.get("ok"):
        return 1
    print("已推送。保持本窗口打开，Ctrl+C 退出")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        srv.stop()
    return 0


def main():
    p = argparse.ArgumentParser(description="TV投屏 - 局域网电视投屏 / 手机网页遥控")
    p.add_argument("--web", action="store_true", help="只跑服务端（无界面）")
    p.add_argument("--root", default=DEFAULT_ROOT, help=f"媒体目录，默认 {DEFAULT_ROOT}")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"端口，默认 {DEFAULT_PORT}")
    p.add_argument("--scan", action="store_true", help="仅扫描局域网设备")
    p.add_argument("--cast", metavar="FILE", help="要投屏的文件")
    p.add_argument("--device", type=int, default=1, metavar="N", help="设备序号（配合 --cast）")
    args = p.parse_args()

    if args.scan:
        sys.exit(cmd_scan())
    if args.cast:
        sys.exit(cmd_cast(args.cast, args.device, args.port))
    if args.web:
        sys.exit(cmd_web(args.root, args.port))

    from tvcast.app import main as gui_main
    gui_main()


if __name__ == "__main__":
    main()
