"""格式兼容与转码引擎。

电视（尤其 DLNA）能直接播的格式非常窄，基本就是：
    MP4 容器 + H.264 (8bit, yuv420p) + AAC-LC 立体声

只要不满足，就由 ffmpeg 转码。两种模式：
  * 实时转码：ffmpeg 输出 fragmented MP4 直接推给电视 —— 秒开，但不能拖进度
  * 缓存转码：整片转成 MP4 存到 <媒体根>/.tvcast_cache/ —— 可拖进度，之后秒开

Windows 要点：本程序是无控制台的窗口程序，拉起 ffmpeg 必须带 CREATE_NO_WINDOW，
否则每个 ffmpeg 子进程都会弹出一个黑框。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

CACHE_DIRNAME = ".tvcast_cache"

# ---------------------------------------------------------------- 子进程静默
_CREATE_NO_WINDOW = 0x08000000
_startupinfo = None


def _popen_kwargs() -> dict:
    """Windows 下保证子进程不开控制台窗口。"""
    global _startupinfo
    kw: dict = {}
    if os.name == "nt":
        if _startupinfo is None:
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0  # SW_HIDE
            _startupinfo = si
        kw["creationflags"] = _CREATE_NO_WINDOW
        kw["startupinfo"] = _startupinfo
    return kw


# ---------------------------------------------------------------- 定位 ffmpeg
def _config_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".tvcast", "config.json")


def load_config() -> dict:
    try:
        with open(_config_path(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg: dict):
    try:
        d = os.path.dirname(_config_path())
        os.makedirs(d, exist_ok=True)
        with open(_config_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _find_tool(name: str) -> Optional[str]:
    cfg = load_config()
    custom = cfg.get(name)
    if custom and os.path.isfile(custom):
        return custom
    hit = shutil.which(name)
    if hit:
        return hit
    exe = name + (".exe" if os.name == "nt" else "")
    for d in (
        os.path.join(os.path.expanduser("~"), ".tvcast", "bin"),
        r"C:\ffmpeg\bin",
        r"C:\Program Files\ffmpeg\bin",
        os.path.dirname(shutil.which("ffmpeg") or ""),
    ):
        if d:
            p = os.path.join(d, exe)
            if os.path.isfile(p):
                return p
    return None


FFMPEG = _find_tool("ffmpeg")
FFPROBE = _find_tool("ffprobe")


def available() -> bool:
    return bool(FFMPEG and FFPROBE)


def refresh_tools():
    global FFMPEG, FFPROBE
    FFMPEG = _find_tool("ffmpeg")
    FFPROBE = _find_tool("ffprobe")


# ---------------------------------------------------------------- 判定规则
# 电视"友好"的格式
GOOD_VIDEO_CODECS = {"h264"}
GOOD_PIX_FMTS = {"yuv420p", "yuvj420p"}
BAD_VIDEO_PROFILES = {"high 10", "high 4:4:4 predictive", "main 10", "high 10 intra"}
GOOD_AUDIO_CODECS = {"aac"}
MAX_AUDIO_CHANNELS = 2
MAX_AUDIO_RATE = 48000
MP4_FORMATS = ("mp4", "mov", "m4a", "3gp")


@dataclass
class FileInfo:
    path: str
    ok: bool = False                    # 探测是否成功
    container: str = ""
    duration: float = 0.0
    vcodec: str = ""
    vprofile: str = ""
    pix_fmt: str = ""
    width: int = 0
    height: int = 0
    acodec: str = ""
    channels: int = 0
    sample_rate: int = 0
    has_video: bool = False
    has_audio: bool = False
    size: int = 0

    # 内嵌字幕轨（外挂字幕不入缓存，因为用户随时可能新增）
    subs_embedded: List[Dict] = field(default_factory=list)

    # 结论
    direct: bool = False                # 能否直接推给电视
    need_video_transcode: bool = False
    need_audio_transcode: bool = False
    reasons: List[str] = field(default_factory=list)
    error: str = ""

    @property
    def summary(self) -> str:
        v = (self.vcodec or "?").upper()
        a = (self.acodec or "?").upper()
        return f"{v} + {a}"

    def to_dict(self) -> Dict:
        return {
            "direct": self.direct,
            "summary": self.summary,
            "reasons": self.reasons,
            "duration": self.duration,
            "width": self.width,
            "height": self.height,
            "vcodec": self.vcodec,
            "acodec": self.acodec,
            "need_video_transcode": self.need_video_transcode,
            "need_audio_transcode": self.need_audio_transcode,
            "error": self.error,
            "probe_ok": self.ok,
            "embedded_subs": [t for t in self.subs_embedded if t.get("text")],
        }


_probe_cache: Dict[str, FileInfo] = {}
_probe_lock = threading.Lock()


def _s(v, default=0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _i(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def probe(path: str) -> FileInfo:
    """用 ffprobe 读流信息并判定兼容性（带缓存）。"""
    try:
        st = os.stat(path)
        key = f"{path}|{st.st_mtime_ns}|{st.st_size}"
    except OSError:
        key = path

    with _probe_lock:
        hit = _probe_cache.get(key)
        if hit is not None:
            return hit

    info = FileInfo(path=path)
    try:
        info.size = os.path.getsize(path)
    except OSError:
        pass

    if not FFPROBE:
        info.error = "找不到 ffprobe"
        return _store(key, info)

    try:
        r = subprocess.run(
            [FFPROBE, "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", path],
            capture_output=True, timeout=30, **_popen_kwargs(),
        )
        data = json.loads(r.stdout.decode("utf-8", "ignore") or "{}")
    except Exception as e:  # noqa: BLE001
        info.error = f"ffprobe 失败: {e}"
        return _store(key, info)

    fmt = data.get("format") or {}
    info.container = fmt.get("format_name", "") or ""
    info.duration = _s(fmt.get("duration"))
    streams = data.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if v:
        info.has_video = True
        info.vcodec = (v.get("codec_name") or "").lower()
        info.vprofile = (v.get("profile") or "").lower()
        info.pix_fmt = (v.get("pix_fmt") or "").lower()
        info.width = _i(v.get("width"))
        info.height = _i(v.get("height"))
    if a:
        info.has_audio = True
        info.acodec = (a.get("codec_name") or "").lower()
        info.channels = _i(a.get("channels"))
        info.sample_rate = _i(a.get("sample_rate"))

    # 内嵌字幕轨：区分文本字幕（能烧）与图像字幕（PGS/DVD，需另一套处理）
    for s in streams:
        if s.get("codec_type") != "subtitle":
            continue
        codec = (s.get("codec_name") or "").lower()
        tags = s.get("tags") or {}
        info.subs_embedded.append({
            "kind": "embedded",
            "index": _i(s.get("index")),
            "codec": codec,
            "lang": (tags.get("language") or ""),
            "title": (tags.get("title") or ""),
            "text": codec in TEXT_SUB_CODECS,
        })
    info.ok = True

    # ---- 判定 ----
    reasons: List[str] = []
    if not info.has_video:
        reasons.append("没有视频轨")

    if info.has_video:
        if info.vcodec not in GOOD_VIDEO_CODECS:
            info.need_video_transcode = True
            reasons.append(f"视频编码 {info.vcodec.upper() or '未知'} 电视不支持，需转 H.264")
        if info.pix_fmt and info.pix_fmt not in GOOD_PIX_FMTS:
            info.need_video_transcode = True
            reasons.append(f"色彩格式 {info.pix_fmt}（10bit/4:4:4）电视不支持")
        if info.vprofile in BAD_VIDEO_PROFILES:
            info.need_video_transcode = True
            reasons.append(f"编码档次 {info.vprofile} 电视不支持")

    if info.has_audio:
        if info.acodec not in GOOD_AUDIO_CODECS:
            info.need_audio_transcode = True
            reasons.append(f"音频编码 {info.acodec.upper() or '未知'} 需转 AAC")
        if info.channels > MAX_AUDIO_CHANNELS:
            info.need_audio_transcode = True
            reasons.append(f"{info.channels} 声道需下混立体声")
        if info.sample_rate > MAX_AUDIO_RATE:
            info.need_audio_transcode = True
            reasons.append(f"采样率 {info.sample_rate} 过高")
    elif info.has_video:
        reasons.append("无音轨（多数电视会拒绝）")

    container_ok = any(k in info.container for k in MP4_FORMATS)
    if not container_ok:
        reasons.append(f"容器 {info.container or '未知'} 非 MP4，需封装转换")

    info.direct = (
        info.ok and info.has_video and container_ok
        and not info.need_video_transcode and not info.need_audio_transcode
    )
    info.reasons = reasons
    return _store(key, info)


def is_cached(path: str) -> bool:
    """该文件是否已经探测过（避免为了显示徽标而阻塞）。"""
    try:
        st = os.stat(path)
        key = f"{path}|{st.st_mtime_ns}|{st.st_size}"
    except OSError:
        return False
    with _probe_lock:
        return key in _probe_cache


def _store(key: str, info: FileInfo) -> FileInfo:
    with _probe_lock:
        if len(_probe_cache) > 4000:
            _probe_cache.clear()
        _probe_cache[key] = info
    return info


# ---------------------------------------------------------------- 编码器选择
_enc_lock = threading.Lock()
_picked_encoder: Optional[str] = None


def pick_encoder(log: Optional[Callable[[str], None]] = None) -> str:
    """优先硬件编码：NVENC > QSV > libx264。"""
    global _picked_encoder
    with _enc_lock:
        if _picked_encoder:
            return _picked_encoder
        if not FFMPEG:
            _picked_encoder = "libx264"
            return _picked_encoder
        for enc in ("h264_nvenc", "h264_qsv", "libx264"):
            try:
                r = subprocess.run(
                    [FFMPEG, "-v", "error", "-f", "lavfi",
                     "-i", "testsrc=size=320x240:rate=25:duration=1",
                     "-c:v", enc, "-f", "null", "-"],
                    capture_output=True, timeout=40, **_popen_kwargs(),
                )
                if r.returncode == 0:
                    _picked_encoder = enc
                    if log:
                        log(f"转码编码器: {enc}")
                    return enc
            except Exception:
                continue
        _picked_encoder = "libx264"
        return _picked_encoder


def video_encode_args(enc: str) -> List[str]:
    if enc == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-tune", "hq",
                "-rc", "vbr", "-cq", "23", "-b:v", "0",
                "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.1",
                "-g", "50", "-bf", "0"]
    if enc == "h264_qsv":
        return ["-c:v", "h264_qsv", "-preset", "veryfast", "-global_quality", "23",
                "-pix_fmt", "nv12", "-profile:v", "high", "-level", "4.1", "-g", "50"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.1",
            "-g", "50", "-threads", "0"]


def audio_encode_args() -> List[str]:
    return ["-c:a", "aac", "-b:a", "192k", "-ac", "2", "-ar", "48000"]


# ---------------------------------------------------------------- 字幕
# 为什么是"烧录"而不是"外挂字幕轨"：
# DLNA 没有标准的外挂字幕通道，电视只会去拉视频本身；Chromecast 的默认接收器
# 也不认 sidecar 字幕。所以想让字幕在电视上出现，唯一通用的办法是把字幕
# 画进画面（burn-in）——这也正是 Go2TV 的做法。
#
# ⚠️ Windows 上 subtitles 滤镜的路径是经典大坑：
#    filter 参数里 ':' 会被当作"选项分隔符"，盘符 C: 直接被吃掉。
#    实测结论（见 subtest_escape.py）：**必须把 ':' 转义成 '\:' 且同时用单引号包裹**，
#    只转义或只加引号都会失败。中文/空格/全角括号路径都没问题。

SUB_EXT = (".srt", ".ass", ".ssa", ".vtt")
TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text", "vtt"}
_CN_HINTS = ("zh", "chs", "cht", "chi", "zho", "sc", "tc", "中", "简体", "繁体")


def _subs_tmp_dir() -> str:
    d = os.path.join(tempfile.gettempdir(), "tvcast_subs")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return tempfile.gettempdir()
    _subs_gc(d)
    return d


_subs_gc_done = False


def _subs_gc(d: str, max_age: float = 7 * 86400.0):
    """顺手清掉超过 7 天的字幕临时文件（进程内只做一次）。"""
    global _subs_gc_done
    if _subs_gc_done:
        return
    _subs_gc_done = True
    now = time.time()
    try:
        for name in os.listdir(d):
            p = os.path.join(d, name)
            try:
                if os.path.isfile(p) and now - os.path.getmtime(p) > max_age:
                    _silent_remove(p)
            except OSError:
                pass
    except OSError:
        pass


def _decode_text(raw: bytes) -> str:
    """字幕文件编码探测：UTF-8(BOM) → UTF-16(BOM) → UTF-8 → GB18030 → Big5。

    中文外挂字幕大量是 GBK 编码，不转换的话 libass 会整片乱码。
    """
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", "replace")
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8", "gb18030", "big5"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _is_cn(label: str) -> bool:
    low = (label or "").lower()
    return any(h in low for h in _CN_HINTS)


def external_subtitles(src: str) -> List[Dict]:
    """找同目录的外挂字幕：`影片.srt` / `影片.zh.srt` / `影片.简体.srt` 都算。

    排序：带中文语言标记的优先（对中文用户最可能想要中文），其次是"与影片完全同名"
    的默认字幕，最后才是其他语言。前两者都满足不了时按名字短的、文件名的字典序。
    """
    d = os.path.dirname(src)
    stem = os.path.splitext(os.path.basename(src))[0]
    try:
        names = os.listdir(d)
    except OSError:
        return []

    ranked: List[tuple] = []
    low_stem = stem.lower()
    for name in names:
        base, ext = os.path.splitext(name)
        if ext.lower() not in SUB_EXT:
            continue
        exact = False
        label = None
        if base == stem:
            label, exact = "默认", True
        elif base.lower().startswith(low_stem):
            rest = base[len(stem):]
            if rest and rest[0] in "._-— 　":
                label = rest.strip("._-— 　") or "默认"
        if label is None:
            continue
        key = (0 if _is_cn(label) else 1, 0 if exact else 1, len(label), name)
        ranked.append((key, {
            "kind": "external",
            "path": os.path.join(d, name),
            "name": name,
            "label": label,
            "codec": ext.lstrip(".").lower(),
            "text": True,
        }))
    ranked.sort(key=lambda kv: kv[0])
    return [kv[1] for kv in ranked]


_SUB_SEP_RE = re.compile(r"[._\-—][^._\-—]*$")


def subtitle_stems(directory: str) -> set:
    """该目录下所有外挂字幕的"主名"集合（逐层剥掉语言后缀，全部小写）。

    调用方只需判断 `os.path.splitext(文件名)[0].lower() in stems`。
    例：`影片.zh.srt` 会同时贡献 `影片.zh` 和 `影片`，所以 `影片.mp4` 能命中。

    ⚠️ 字符类里的 `-` 必须转义：`[._-—]` 会被正则解释成 U+005F→U+2014 的**区间**，
    而这个区间把中日韩汉字整段吞掉 —— `re.split` 会切出一串空串。踩过。
    """
    out: set = set()
    try:
        names = os.listdir(directory)
    except OSError:
        return out
    for name in names:
        base, ext = os.path.splitext(name)
        if ext.lower() not in SUB_EXT:
            continue
        low = base.lower()
        for _ in range(4):                      # 最多剥 3 层语言后缀
            if not low or low in out:
                break
            out.add(low)
            trimmed = _SUB_SEP_RE.sub("", low)
            if trimmed == low:
                break
            low = trimmed
    return out


def all_subtitles(src: str, info: Optional[FileInfo] = None) -> List[Dict]:
    """外挂字幕（优先）+ 可烧录的内嵌文本字幕轨。"""
    out = external_subtitles(src)
    info = info or probe(src)
    for t in info.subs_embedded:
        if t.get("text"):
            label = t.get("title") or t.get("lang") or ("内嵌 #%s" % t.get("index"))
            out.append(dict(t, label=label))
    return out


def _utf8_copy(path: str) -> str:
    """把字幕统一成 UTF-8 副本（libass 只认 UTF-8）。已经是 UTF-8 就返回原路径。"""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return path
    try:
        raw.decode("utf-8")
        if not raw.startswith(b"\xef\xbb\xbf"):
            return path                      # 本来就是纯 UTF-8，直接用
    except UnicodeDecodeError:
        pass

    text = _decode_text(raw)
    key = hashlib.md5(
        (os.path.abspath(path) + "|" + str(len(raw)) + "|" + str(os.path.getmtime(path))).encode("utf-8")
    ).hexdigest()[:16]
    out = os.path.join(_subs_tmp_dir(), key + os.path.splitext(path)[1].lower())
    try:
        with open(out, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        return out
    except OSError:
        return path


def extract_embedded_sub(src: str, index: int) -> Optional[str]:
    """把内嵌字幕轨抽成独立的 UTF-8 SRT 再烧 —— 比直接让滤镜读容器稳得多。"""
    if not FFMPEG:
        return None
    try:
        st = os.stat(src)
        stamp = f"{st.st_mtime_ns}|{st.st_size}"
    except OSError:
        stamp = "0"
    key = hashlib.md5(f"{os.path.abspath(src)}|{index}|{stamp}".encode("utf-8")).hexdigest()[:16]
    out = os.path.join(_subs_tmp_dir(), key + ".srt")
    if os.path.isfile(out) and os.path.getsize(out) > 0:
        return out
    tmp = out + ".part.srt"
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
           "-i", src, "-map", f"0:{int(index)}", "-c:s", "srt", "-f", "srt", tmp]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=600, **_popen_kwargs())
    except Exception:
        return None
    if r.returncode != 0 or not os.path.isfile(tmp) or os.path.getsize(tmp) == 0:
        _silent_remove(tmp)
        return None
    try:
        os.replace(tmp, out)
    except OSError:
        _silent_remove(tmp)
        return None
    return out


def resolve_subtitle_file(src: str, track: Dict) -> Optional[str]:
    if track.get("kind") == "external":
        return _utf8_copy(track["path"])
    return extract_embedded_sub(src, track.get("index", 0))


def subtitle_filter(sub_path: str) -> str:
    """生成 subtitles 滤镜参数。

    实测（Windows）：必须 ':' → '\\:' 且整体用单引号包裹，两者缺一不可。
    """
    p = os.path.abspath(sub_path).replace("\\", "/")
    p = p.replace("'", "\\'").replace(":", "\\:")
    return f"subtitles=filename='{p}'"


def plan_subtitles(src: str, info: FileInfo, mode: str = "auto",
                   prefer: Optional[str] = None,
                   force_burn: bool = False):
    """决定这次播放/转码要不要烧字幕。

    返回 (filter 字符串 | None, 说明文字)
      mode = off   → 永不烧
      mode = auto  → 只有"本来就要重编码视频"时才顺手烧；纯直连的画质不为了字幕牺牲
      mode = burn  → 有字幕就烧（会让本来可直连的文件也重编码）
    force_burn 供"显式转码成 MP4"这类用户主动动作使用，绕过 auto 的跳过逻辑。
    """
    if mode == "off":
        return None, ""
    tracks = all_subtitles(src, info)
    if not tracks:
        return None, ""

    track = tracks[0]
    if prefer:
        hit = next((t for t in tracks
                    if prefer in (t.get("name"), t.get("label"), t.get("lang"))), None)
        if hit:
            track = hit

    label = track.get("label") or track.get("title") or track.get("lang") or "字幕"
    will_reencode = info.need_video_transcode
    if mode == "auto" and not will_reencode and not force_burn:
        return None, f"检测到{label}，但画面可直连；烧字幕需重编码（把字幕模式设为「总是烧录」即可）"

    sub_file = resolve_subtitle_file(src, track)
    if not sub_file:
        return None, f"{label} 准备失败（可能是图像字幕 PGS/DVD，暂不支持）"
    return subtitle_filter(sub_file), f"已烧录{label}"


# ---------------------------------------------------------------- 命令构造
def _common_input(path: str, start: float = 0.0) -> List[str]:
    cmd = [FFMPEG or "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    if start and start > 0:
        cmd += ["-ss", f"{float(start):.3f}"]
    cmd += ["-i", path]
    return cmd


def build_live_cmd(path: str, info: FileInfo, start: float = 0.0, enc: Optional[str] = None,
                   sub_filter: Optional[str] = None, container: str = "ts") -> List[str]:
    """实时转码命令：边转边推。

    container="ts"  → MPEG-TS（DLNA 电视走这条）
    container="mp4" → fragmented MP4（Chromecast 等走这条）

    为什么默认是 TS 而不是 fMP4：实测这台电视（Android stagefright）拿到
    empty_moov 的分片 MP4 会立刻断开 —— 时长 0、没有 moov 索引，它按"可随机读"
    去访问却拿不到对应字节。MPEG-TS 天生是流式容器，无需索引，它吃得很好。
    """
    enc = enc or pick_encoder()
    cmd = _common_input(path, start)
    cmd += ["-map", "0:v:0?", "-map", "0:a:0?"]
    # 烧字幕必须重编码视频（对已编码帧无法再叠加文字）
    need_encode = info.need_video_transcode or not info.has_video or bool(sub_filter)
    if need_encode:
        cmd += video_encode_args(enc)
    else:
        cmd += ["-c:v", "copy"]
    if sub_filter:
        cmd += ["-vf", sub_filter]
    if info.need_audio_transcode or (info.has_audio and info.acodec not in GOOD_AUDIO_CODECS):
        cmd += audio_encode_args()
    elif info.has_audio:
        cmd += ["-c:a", "copy"]
    else:
        cmd += ["-an"]
    if container == "ts":
        # TS 不支持某些编码，复制视频进 TS 时确保是 H.264（不兼容的已在上面重编码）
        cmd += ["-f", "mpegts", "pipe:1"]
    else:
        cmd += [
            "-movflags", "frag_keyframe+empty_moov+default_base_moof",
            "-f", "mp4", "pipe:1",
        ]
    return cmd


def build_file_cmd(path: str, out_path: str, info: FileInfo, enc: Optional[str] = None,
                   sub_filter: Optional[str] = None) -> List[str]:
    """缓存转码命令：输出可拖进度的普通 MP4（faststart）。"""
    enc = enc or pick_encoder()
    cmd = [FFMPEG or "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
           "-i", path]
    cmd += ["-map", "0:v:0?", "-map", "0:a:0?"]
    need_encode = info.need_video_transcode or not info.has_video or bool(sub_filter)
    if need_encode:
        cmd += video_encode_args(enc)
    else:
        cmd += ["-c:v", "copy"]
    if sub_filter:
        cmd += ["-vf", sub_filter]
    if info.has_audio:
        cmd += audio_encode_args() if info.need_audio_transcode else ["-c:a", "copy"]
    else:
        cmd += ["-an"]
    # 显式指定封装格式：输出文件名可能带 .part 后缀，ffmpeg 无法据此推断
    cmd += ["-f", "mp4", "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", out_path]
    return cmd


def start_live(path: str, info: FileInfo, start: float = 0.0, log=None,
               sub_filter: Optional[str] = None,
               container: str = "ts") -> Optional[subprocess.Popen]:
    """启动实时转码进程，返回 Popen（stdout 即为可直接推给播放器的媒体流）。"""
    if not FFMPEG:
        if log:
            log("找不到 ffmpeg，无法转码")
        return None
    cmd = build_live_cmd(path, info, start, sub_filter=sub_filter, container=container)
    if log:
        log(f"实时转码启动（{container}）: " + " ".join(cmd[:6]) + " …")
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=0, **_popen_kwargs(),
        )
    except Exception as e:  # noqa: BLE001
        if log:
            log(f"启动 ffmpeg 失败: {e}")
        return None
    # 排空 stderr，避免管道写满导致 ffmpeg 卡死；保留最后几行用于报错
    def drain():
        tail: List[bytes] = []
        try:
            assert proc.stderr is not None
            for line in proc.stderr:
                tail.append(line)
                if len(tail) > 12:
                    tail.pop(0)
        except Exception:
            pass
        finally:
            proc._stderr_tail = b"".join(tail)  # type: ignore[attr-defined]
    threading.Thread(target=drain, daemon=True).start()
    return proc


def cache_path_for(root: str, src_path: str, variant: str = "") -> str:
    """缓存文件路径：<root>/.tvcast_cache/<相对路径>[.变体].mp4

    variant 用来区分"烧了字幕"和"没烧字幕"两种产物 —— 它们是不同的视频内容，
    绝不能共用同一个缓存文件，否则用户下次关掉字幕会看到烙在画面上的旧字幕。

    注意：Windows 上 mkdtemp 之类会给出 8.3 短名（ADMINI~1），而 realpath 会还原成长名。
    两边必须先统一规范化，否则 relpath 算不出公共前缀，会生成一串 ../..
    """
    root = os.path.realpath(os.path.abspath(root))
    src = os.path.realpath(os.path.abspath(src_path))
    try:
        rel = os.path.relpath(src, root).replace("\\", "/")
    except ValueError:
        rel = os.path.basename(src)
    if rel.startswith(".."):
        rel = os.path.basename(src)
    rel = re.sub(r"\.(mp4|mkv|webm|avi|mov|ts|m2ts|flv|wmv|mpg|mpeg|rmvb|m4v|3gp)$", "", rel, flags=re.I)
    if variant:
        rel = f"{rel}.{variant}"
    return os.path.join(root, CACHE_DIRNAME, rel + ".mp4")


def find_cached(root: str, src_path: str, variant: str = "") -> Optional[str]:
    p = cache_path_for(root, src_path, variant)
    return p if os.path.isfile(p) and os.path.getsize(p) > 0 else None


# ---------------------------------------------------------------- 边播边缓存
# 思路：实时转码的那条 fMP4 流在推给电视的同时，顺手落一份到磁盘；
# 播完后再用 -c copy 重新封装成 faststart MP4（不重新编码，几秒钟），
# 于是"这一次"是秒开，"下一次"就变成秒开 + 能拖进度。
_partial_lock = threading.Lock()
_active_partials: set = set()
_finalize_jobs: List[Dict] = []


def partial_path_for(root: str, src_path: str, variant: str = "",
                     container: str = "mp4") -> str:
    """落盘半成品的路径。扩展名必须跟真实容器一致，否则封装时无从判断。"""
    ext = "ts" if container == "ts" else "mp4"
    return cache_path_for(root, src_path, variant) + f".partial.{ext}"


def claim_partial(p: str) -> bool:
    """同一路临时文件只允许一个写入者；已被占用则本次不缓存。"""
    with _partial_lock:
        if p in _active_partials:
            return False
        _active_partials.add(p)
        return True


def release_partial(p: str):
    with _partial_lock:
        _active_partials.discard(p)


def discard_partial(p: str):
    release_partial(p)
    _silent_remove(p)


def finalize_status() -> Dict:
    with _partial_lock:
        running = [j for j in _finalize_jobs if j["state"] == "running"]
        last = _finalize_jobs[-1] if _finalize_jobs else None
    return {
        "running": len(running),
        "current": running[-1]["src"] if running else "",
        "last": dict(last) if last else None,
    }


def finalize_cache(partial: str, out: str, log: Optional[Callable[[str], None]] = None) -> Dict:
    """把流式转码落下的 fMP4 重新封装成可拖进度的 faststart MP4（仅封装，不重编码）。"""
    if not FFMPEG:
        return {"ok": False, "error": "找不到 ffmpeg"}
    try:
        if not os.path.isfile(partial) or os.path.getsize(partial) < 64 * 1024:
            return {"ok": False, "error": "落盘数据过少，跳过缓存"}
    except OSError as e:
        return {"ok": False, "error": str(e)}

    try:
        os.makedirs(os.path.dirname(out), exist_ok=True)
    except Exception:
        pass
    tmp = os.path.join(os.path.dirname(out), "." + os.path.basename(out) + ".tmp.mp4")
    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", partial,
        "-map", "0:v:0?", "-map", "0:a:0?",
        "-c", "copy", "-f", "mp4", "-movflags", "+faststart",
        tmp,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=1800, **_popen_kwargs())
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"封装失败: {e}"}
    if r.returncode != 0:
        _silent_remove(tmp)
        msg = r.stderr.decode("utf-8", "ignore").strip()[:300]
        return {"ok": False, "error": msg or "ffmpeg 封装返回非零"}
    try:
        os.replace(tmp, out)
    except OSError as e:
        _silent_remove(tmp)
        return {"ok": False, "error": f"写入缓存失败: {e}"}
    _silent_remove(partial)
    return {"ok": True, "out": out}


def finalize_in_background(partial: str, out: str,
                           log: Optional[Callable[[str], None]] = None) -> threading.Thread:
    log = log or (lambda m: None)
    rec = {"src": os.path.basename(out), "state": "running", "error": ""}
    with _partial_lock:
        _finalize_jobs.append(rec)
        if len(_finalize_jobs) > 50:
            del _finalize_jobs[:-50]

    def run():
        try:
            res = finalize_cache(partial, out, log)
        except Exception as e:  # noqa: BLE001
            res = {"ok": False, "error": str(e)}
        with _partial_lock:
            rec["state"] = "done" if res.get("ok") else "error"
            rec["error"] = str(res.get("error", ""))[:300]
        release_partial(partial)
        if res.get("ok"):
            log(f"已缓存，下次可拖进度：{os.path.basename(out)}")
        else:
            log(f"缓存封装失败：{rec['error'][:160]}")

    t = threading.Thread(target=run, daemon=True, name="tvcast-finalize")
    t.start()
    return t


# ---------------------------------------------------------------- 后台转码任务
class TranscodeJob:
    """整片转码到缓存文件，带进度。"""

    def __init__(self, src: str, out: str, info: FileInfo, log=None,
                 sub_filter: Optional[str] = None, sub_note: str = ""):
        self.src = src
        self.out = out
        self.info = info
        self.log = log or (lambda m: None)
        self.sub_filter = sub_filter
        self.sub_note = sub_note
        self.progress = 0.0
        self.state = "idle"        # idle|running|done|error|cancelled
        self.error = ""
        self.started_at = 0.0
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._cancel = threading.Event()

    # ---- 对外 ----
    def start(self):
        self.state = "running"
        self.started_at = time.time()
        self._thread = threading.Thread(target=self._run, daemon=True, name="tvcast-transcode")
        self._thread.start()

    def cancel(self):
        self._cancel.set()
        p, self._proc = self._proc, None
        if p and p.poll() is None:
            try:
                p.kill()
            except Exception:
                pass

    def status(self) -> Dict:
        eta = None
        if self.state == "running" and self.progress and self.progress > 0.005:
            el = time.time() - self.started_at
            eta = max(0, el / self.progress - el)
        return {
            "state": self.state,
            "progress": round(min(1.0, self.progress), 4),
            "percent": round(min(100.0, self.progress * 100), 1),
            "error": self.error,
            "src": os.path.basename(self.src),
            "out": self.out,
            "eta": int(eta) if eta else None,
            "sub_note": self.sub_note,
        }

    # ---- 内部 ----
    def _run(self):
        if not FFMPEG:
            self.state, self.error = "error", "找不到 ffmpeg"
            return
        try:
            os.makedirs(os.path.dirname(self.out), exist_ok=True)
        except Exception:
            pass
        base, ext = os.path.splitext(self.out)
        tmp = f"{base}.part{ext or '.mp4'}"
        cmd = build_file_cmd(self.src, tmp, self.info, sub_filter=self.sub_filter)
        self.log(f"开始转码: {os.path.basename(self.src)}"
                 + (f"（{self.sub_note}）" if self.sub_note else ""))
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=0, **_popen_kwargs(),
            )
        except Exception as e:  # noqa: BLE001
            self.state, self.error = "error", f"启动 ffmpeg 失败: {e}"
            return
        self._proc = proc

        dur = self.info.duration or 0
        tail: List[bytes] = []
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.decode("utf-8", "ignore").strip()
                if line.startswith("out_time_ms=") or line.startswith("out_time_us="):
                    try:
                        us = float(line.split("=", 1)[1])
                        sec = us / 1_000_000.0
                        if dur > 0:
                            self.progress = min(1.0, sec / dur)
                    except ValueError:
                        pass
                elif line.startswith("progress=") and line.endswith("end"):
                    self.progress = 1.0
                if self._cancel.is_set():
                    break
        except Exception:
            pass
        finally:
            try:
                if proc.stderr:
                    tail = proc.stderr.read().splitlines()[-8:]
            except Exception:
                pass
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()

        if self._cancel.is_set():
            self.state = "cancelled"
            _silent_remove(tmp)
            self.log("转码已取消")
            return
        if proc.returncode != 0:
            self.state = "error"
            self.error = (b"\n".join(tail).decode("utf-8", "ignore") or "ffmpeg 返回非零").strip()[:600]
            _silent_remove(tmp)
            self.log(f"转码失败: {self.error[:200]}")
            return
        try:
            os.replace(tmp, self.out)
        except OSError as e:
            self.state, self.error = "error", f"写入缓存失败: {e}"
            return
        self.progress = 1.0
        self.state = "done"
        self.log(f"转码完成: {os.path.basename(self.out)}")


def _silent_remove(p: str):
    try:
        if p and os.path.isfile(p):
            os.remove(p)
    except OSError:
        pass
