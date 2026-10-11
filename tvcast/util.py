"""通用工具：本地 IP 探测、MIME 猜测、文件大小格式化。"""

from __future__ import annotations

import socket

# 视频/音频扩展名 -> MIME
VIDEO_EXT = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".avi": "video/x-msvideo",
    ".ts": "video/mp2t",
    ".m2ts": "video/mp2t",
    ".flv": "video/x-flv",
    ".wmv": "video/x-ms-wmv",
    ".mpg": "video/mpeg",
    ".mpeg": "video/mpeg",
    ".rmvb": "application/vnd.rn-realmedia-vbr",
    ".3gp": "video/3gpp",
}

AUDIO_EXT = {
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".aac": "audio/aac",
    ".m4a": "audio/mp4",
    ".ogg": "audio/ogg",
    ".ape": "audio/x-ape",
    ".wma": "audio/x-ms-wma",
}

IMAGE_EXT = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".webp": "image/webp",
}

MEDIA_EXT = {}
MEDIA_EXT.update(VIDEO_EXT)
MEDIA_EXT.update(AUDIO_EXT)
MEDIA_EXT.update(IMAGE_EXT)


def config_dir() -> str:
    import os
    return os.path.join(os.path.expanduser("~"), ".tvcast")


def config_path() -> str:
    import os
    return os.path.join(config_dir(), "config.json")


def load_config() -> dict:
    """读取 ~/.tvcast/config.json（不存在或损坏都返回空字典）。"""
    import json
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_config(**kv) -> None:
    """把键值合并写回配置文件；失败就算了，不要影响主流程。"""
    import json
    import os
    try:
        data = load_config()
        data.update(kv)
        os.makedirs(config_dir(), exist_ok=True)
        with open(config_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def default_media_dir() -> str:
    """默认媒体目录。

    优先级：环境变量 TVCAST_ROOT > 上次用过的目录 > 当前用户的「视频」目录。
    刻意不硬编码任何个人路径 —— 换机器、换用户都能直接跑。
    """
    import os
    env = os.environ.get("TVCAST_ROOT", "").strip()
    if env:
        return env
    remembered = str(load_config().get("root", "")).strip()
    if remembered and os.path.isdir(remembered):
        return remembered
    return os.path.join(os.path.expanduser("~"), "Videos")


def get_local_ip() -> str:
    """取得本机在局域网中的 IP（用于电视回连）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # 不会真的发包，只是让内核选一条出口路由
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def guess_mime(path: str) -> str:
    import os
    ext = os.path.splitext(path)[1].lower()
    return MEDIA_EXT.get(ext, "application/octet-stream")


def is_media(path: str) -> bool:
    import os
    return os.path.splitext(path)[1].lower() in MEDIA_EXT


def upnp_class_for(path: str) -> str:
    import os
    ext = os.path.splitext(path)[1].lower()
    if ext in VIDEO_EXT:
        return "object.item.videoItem"
    if ext in AUDIO_EXT:
        return "object.item.audioItem.musicTrack"
    if ext in IMAGE_EXT:
        return "object.item.imageItem.photo"
    return "object.item"


def human_size(num: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024:
            return f"{num:.1f}{unit}" if unit != "B" else f"{num}B"
        num /= 1024.0
    return f"{num:.1f}PB"


def human_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"
