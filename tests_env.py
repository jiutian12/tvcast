"""测试脚本共用的环境配置。

所有跟个人环境相关的值一律走环境变量，默认值都是通用路径，
仓库里不出现任何具体机器的目录名 / 内网 IP / 个人媒体文件名：

  TVCAST_ROOT       媒体目录，默认 ~/Videos
  TVCAST_LOCAL_IP   本机 IP，默认自动探测
  TVCAST_TV_IP      电视 IP，默认留空（只有真机投屏用例才需要）
  TVCAST_EXE        打包后的 exe，默认 ./dist/TVCast.exe
  TVCAST_FFMPEG     ffmpeg 可执行文件，默认从 PATH 查找
  TVCAST_FFPROBE    ffprobe 可执行文件，默认从 PATH 查找

本机跑测试时先设好媒体目录，例如：

  set TVCAST_ROOT=D:\\Videos
  python selftest.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tvcast.util import MEDIA_EXT, default_media_dir, get_local_ip  # noqa: E402

ROOT = os.environ.get("TVCAST_ROOT", "").strip() or default_media_dir()
LOCAL_IP = os.environ.get("TVCAST_LOCAL_IP", "").strip() or get_local_ip()
TV_IP = os.environ.get("TVCAST_TV_IP", "").strip()

_HERE = os.path.dirname(os.path.abspath(__file__))
EXE = os.environ.get("TVCAST_EXE", "").strip() or os.path.join(_HERE, "dist", "TVCast.exe")
FFMPEG = os.environ.get("TVCAST_FFMPEG", "").strip() or shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = os.environ.get("TVCAST_FFPROBE", "").strip() or shutil.which("ffprobe") or "ffprobe"

MISSING_SAMPLE = (
    "媒体目录里没找到可用的视频样本。\n"
    "  当前媒体目录: %s\n"
    "  可以设环境变量指定，例如: set TVCAST_ROOT=D:\\Videos" % ROOT
)


def _walk_videos(base: str):
    """递归列出媒体目录下所有视频（跳过隐藏/下划线开头的目录）。"""
    if not base or not os.path.isdir(base):
        return
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if not d.startswith((".", "_"))]
        for f in filenames:
            if os.path.splitext(f)[1].lower() in MEDIA_EXT:
                p = os.path.join(dirpath, f)
                try:
                    yield os.path.getsize(p), p
                except OSError:
                    continue


def pick_video(min_bytes: int = 0, root: str = None):
    """挑一个视频文件，返回绝对路径；找不到返回 None。

    取「体积最小且不小于 min_bytes」的那个 —— 测试跑得快。
    """
    cands = [x for x in _walk_videos(root or ROOT) if x[0] >= min_bytes]
    if not cands:
        return None
    cands.sort()
    return cands[0][1]


def pick_incompatible(min_bytes: int = 0, root: str = None, scan_limit: int = 40):
    """挑一个「需要转码」的样本（复用项目自己的 media.probe 判定）。

    找不到（或全都兼容）时退化为 pick_video()。
    """
    try:
        from tvcast import media
    except Exception:
        return pick_video(min_bytes, root)

    cands = [x for x in _walk_videos(root or ROOT) if x[0] >= min_bytes]
    cands.sort()
    for _size, p in cands[:scan_limit]:
        try:
            info = media.probe(p)
        except Exception:
            continue
        if info.ok and not info.direct and info.has_video and info.duration > 0:
            return p
    return pick_video(min_bytes, root)


def require_ffprobe() -> str:
    """确认 ffprobe 可用（跑不了就早点失败，别在用例中途炸）。"""
    try:
        subprocess.run([FFPROBE, "-version"], capture_output=True, timeout=15)
    except Exception:
        raise SystemExit("找不到 ffprobe，请设 TVCAST_FFPROBE 或把它加进 PATH")
    return FFPROBE
