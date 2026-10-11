"""核心控制层：设备/片源/播放状态与全部遥控动作。

GUI 和网页版都只调用这一层，保证两端状态一致。
所有公开方法都做了加锁，可安全地从多线程（HTTP 请求线程、GUI 线程）调用。
"""

from __future__ import annotations

import os
import re
import threading
import time
from typing import Callable, Dict, List, Optional

import tvcast.dlna as dlna
from . import media
from .discovery import Device, discover_all
from .util import MEDIA_EXT, guess_mime, human_size, upnp_class_for

try:
    import tvcast.cast as castmod
except Exception:  # noqa: BLE001
    castmod = None


def _natural_key(name: str):
    """自然排序：第2集 排在 第10集 前面（纯字典序会反过来）。"""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name or "")]


class CastCore:
    def __init__(self, root_dir: str, local_ip: str, log: Optional[Callable[[str], None]] = None):
        # realpath 统一长路径形式，避免 Windows 8.3 短名（ADMINI~1）导致 relpath 算错
        self.root_dir = os.path.realpath(os.path.abspath(root_dir))
        self.local_ip = local_ip
        self._log = log or (lambda m: None)

        self.lock = threading.RLock()
        self.devices: List[Device] = []
        self.current_device: Optional[Device] = None
        self.current_file: Optional[str] = None
        self.volume: int = 30
        self.last_error: str = ""
        self._scanning = False
        self.server = None  # 由外部注入的 MediaServer

        # 转码策略
        self.auto_transcode: bool = True    # 遇到电视不支持的格式自动实时转码
        self.force_transcode: bool = False  # 强制转码（连可直连的也转）
        self.sub_mode: str = "auto"         # 字幕: auto|burn|off
        self.play_mode: str = ""            # 上次投屏用的方式: direct|cache|transcode
        self._job: Optional[media.TranscodeJob] = None

        # 播放队列 / 自动连播
        self.playlist: List[str] = []       # 当前队列（绝对路径，自然排序）
        self.playlist_dir: str = ""         # 队列来源目录（相对）
        self.playlist_index: int = -1
        self.autonext: bool = True
        self._last_transport: str = ""
        self._watched_playing: bool = False  # 本集是否真的播起来过
        self._max_pos: float = 0.0           # 本集到过的最大进度（判断"播完"用）
        self._autonext_busy: bool = False
        self._user_stopped: bool = False
        self._live_clock: Optional[float] = None  # 直播转码流的播放起点（墙钟）
        self._live_offset: float = 0.0           # 当前这条直播流的起始时间点（源片秒）

        # 后台格式预热（让徽标快速铺满，又不阻塞请求）
        self._warm_lock = threading.Lock()
        self._warm_pending: List[str] = []
        self._warm_threads: List[threading.Thread] = []

    def log(self, msg: str):
        try:
            self._log(msg)
        except Exception:
            pass

    # ==================================================================
    # 设备
    # ==================================================================
    def scan(self, timeout: float = 4.0) -> List[Device]:
        with self.lock:
            if self._scanning:
                return list(self.devices)
            self._scanning = True
        try:
            self.log("开始扫描局域网设备…")
            devs = discover_all(self.local_ip, log=self.log)
            with self.lock:
                # 释放上一轮的 Chromecast 连接
                for d in self.devices:
                    if d.kind == "chromecast" and castmod is not None:
                        try:
                            castmod.disconnect(d)
                        except Exception:
                            pass
                self.devices = devs
                # 尽量保留当前选中的设备
                if self.current_device is not None:
                    hit = next((d for d in devs if d.key == self.current_device.key), None)
                    self.current_device = hit
            self.log(f"扫描完成，发现 {len(devs)} 台设备")
            return devs
        except Exception as e:  # noqa: BLE001
            self.log(f"扫描失败: {e}")
            with self.lock:
                return list(self.devices)
        finally:
            with self.lock:
                self._scanning = False

    def select_device(self, key: str) -> bool:
        with self.lock:
            hit = next((d for d in self.devices if d.key == key), None)
            if hit is None:
                return False
            self.current_device = hit
            self.log(f"已选择设备: {hit}")
            return True

    def devices_json(self) -> List[Dict]:
        with self.lock:
            return [
                {
                    "key": d.key,
                    "name": d.name,
                    "kind": d.kind,
                    "host": d.host,
                    "model": d.model,
                    "selected": bool(self.current_device and d.key == self.current_device.key),
                    "has_volume": bool(d.render_control_url) if d.kind == "dlna" else True,
                }
                for d in self.devices
            ]

    # ==================================================================
    # 片源
    # ==================================================================
    def _abs(self, rel: str) -> Optional[str]:
        rel = (rel or "").replace("\\", "/").lstrip("/")
        full = os.path.realpath(os.path.join(self.root_dir, rel))
        root = os.path.realpath(self.root_dir)
        if full != root and not full.startswith(root + os.sep):
            return None
        return full

    def _rel(self, path: str) -> str:
        try:
            return os.path.relpath(path, self.root_dir).replace("\\", "/")
        except ValueError:
            return os.path.basename(path)

    def browse(self, rel_dir: str = "") -> Dict:
        """列出某个目录下的子目录与媒体文件。"""
        target = self._abs(rel_dir)
        if target is None or not os.path.isdir(target):
            return {"dir": "", "dirs": [], "files": [], "error": "目录不存在"}

        dirs, files = [], []
        try:
            for entry in sorted(os.scandir(target), key=lambda e: e.name.lower()):
                if entry.name.startswith("."):
                    continue
                if entry.is_dir():
                    dirs.append({"name": entry.name, "rel": self._rel(entry.path)})
                elif entry.is_file() and os.path.splitext(entry.name)[1].lower() in MEDIA_EXT:
                    try:
                        size = entry.stat().st_size
                    except OSError:
                        size = 0
                    files.append({
                        "name": entry.name,
                        "rel": self._rel(entry.path),
                        "size": size,
                        "size_text": human_size(size),
                    })
        except OSError as e:
            return {"dir": rel_dir, "dirs": [], "files": [], "error": str(e)}

        files.sort(key=lambda f: -f["size"])
        return {"dir": rel_dir, "dirs": dirs, "files": files}

    def search(self, keyword: str, limit: int = 300) -> List[Dict]:
        kw = (keyword or "").strip().lower()
        if not kw:
            return []
        out: List[Dict] = []
        for dirpath, dirnames, filenames in os.walk(self.root_dir):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for f in filenames:
                if os.path.splitext(f)[1].lower() not in MEDIA_EXT:
                    continue
                if kw in f.lower():
                    p = os.path.join(dirpath, f)
                    try:
                        size = os.path.getsize(p)
                    except OSError:
                        size = 0
                    out.append({
                        "name": f,
                        "rel": self._rel(p),
                        "size": size,
                        "size_text": human_size(size),
                    })
                    if len(out) >= limit:
                        out.sort(key=lambda x: -x["size"])
                        return out
        out.sort(key=lambda x: -x["size"])
        return out

    # ==================================================================
    # 投屏与遥控
    # ==================================================================
    def _resolve_file(self, rel_or_abs: str) -> Optional[str]:
        p = rel_or_abs
        if not os.path.isabs(p):
            p = self._abs(rel_or_abs) or ""
        return p if p and os.path.isfile(p) else None

    # ---------------------------------------------------------------- 播放队列
    def _build_queue(self, path: str) -> None:
        """用该文件所在目录生成播放队列（自然排序，只收媒体文件）。

        这样"投一集"就自动带上"整目录连播"的能力，不必让用户先手动建列表。
        """
        d = os.path.dirname(path)
        paths: List[str] = []
        try:
            for e in os.scandir(d):
                try:
                    if e.is_file() and not e.name.startswith(".") \
                            and os.path.splitext(e.name)[1].lower() in MEDIA_EXT:
                        paths.append(e.path)
                except OSError:
                    continue
        except OSError:
            paths = []

        if path not in paths:
            paths.append(path)
        paths.sort(key=lambda p: _natural_key(os.path.basename(p)))
        try:
            idx = paths.index(path)
        except ValueError:
            idx = 0

        self.playlist = paths
        self.playlist_dir = self._rel(d)
        self.playlist_index = idx
        self.log(f"播放队列：{os.path.basename(d)} 共 {len(paths)} 个，当前第 {idx + 1} 个")

    def _reset_watch(self, live_offset: float = 0.0) -> None:
        """换片（或转码流重推定位）时重置"播完检测"的观察状态。

        live_offset 是这条直播转码流在源片中的起始秒数：正常从头播是 0，
        拖动定位后是目标时间点。进度估算要用它做基准。
        """
        self._last_transport = ""
        self._watched_playing = False
        self._max_pos = 0.0
        self._autonext_busy = False
        self._user_stopped = False
        self._live_clock = None
        self._live_offset = max(0.0, float(live_offset or 0.0))

    def next_track(self) -> Dict:
        with self.lock:
            if len(self.playlist) < 2:
                return {"ok": False, "error": "没有播放队列"}
            i = self.playlist_index + 1
            if i >= len(self.playlist):
                return {"ok": False, "error": "已经是最后一个了"}
            self.playlist_index = i
            path = self.playlist[i]
        self.log(f"切到下一个（{i + 1}/{len(self.playlist)}）")
        return self.cast(path, queue=False)

    def prev_track(self) -> Dict:
        with self.lock:
            if len(self.playlist) < 2:
                return {"ok": False, "error": "没有播放队列"}
            i = self.playlist_index - 1
            if i < 0:
                return {"ok": False, "error": "已经是第一个了"}
            self.playlist_index = i
            path = self.playlist[i]
        self.log(f"切到上一个（{i + 1}/{len(self.playlist)}）")
        return self.cast(path, queue=False)

    def playlist_json(self, limit: int = 1000) -> Dict:
        with self.lock:
            items = [
                {"name": os.path.basename(p), "rel": self._rel(p), "current": i == self.playlist_index}
                for i, p in enumerate(self.playlist[:limit])
            ]
            return {
                "dir": self.playlist_dir,
                "index": self.playlist_index,
                "total": len(self.playlist),
                "items": items,
            }

    def cast(self, rel_or_abs: str, device_key: Optional[str] = None,
             queue: bool = True, start: float = 0.0) -> Dict:
        path = self._resolve_file(rel_or_abs)
        if not path:
            return {"ok": False, "error": "文件不存在"}
        with self.lock:
            if device_key:
                hit = next((d for d in self.devices if d.key == device_key), None)
                if hit is not None:
                    self.current_device = hit
            dev = self.current_device
        if dev is None:
            return {"ok": False, "error": "还没有选择电视，请先扫描并选择设备"}
        if self.server is None:
            return {"ok": False, "error": "媒体服务未启动"}

        url, mode, sub_filter, sub_note, variant = self._play_url(path, dev, start)
        title = os.path.basename(path)
        duration = 0.0
        try:
            duration = media.probe(path).duration
        except Exception:
            pass

        if mode == "cache":
            mime, upnp_cls = "video/mp4", "object.item.videoItem"
        elif mode == "transcode":
            # 直播流封装按设备选：DLNA 电视吃 MPEG-TS，Chromecast 吃 fMP4
            mime = "video/mp2t" if _live_container(dev) == "ts" else "video/mp4"
            upnp_cls = "object.item.videoItem"
        else:
            mime, upnp_cls = guess_mime(path), upnp_class_for(path)
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0

        try:
            if dev.kind == "chromecast":
                if castmod is None:
                    raise RuntimeError("未安装 pychromecast，无法投到 Chromecast 设备")
                castmod.cast_media(dev, url, title, mime)
            else:
                dlna.cast(dev, url, title, mime, upnp_cls,
                          0 if mode == "transcode" else size,
                          dlna.fmt_iso_duration(duration))
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)
            self.log(f"投屏失败: {e}")
            return {"ok": False, "error": str(e), "url": url, "mode": mode}

        with self.lock:
            self.current_file = path
            self.play_mode = mode
            self.last_error = ""
            # 转码流是"从某个时间点开始的一条新流"，进度基准要用 start；其它模式都是 0
            self._reset_watch(live_offset=(start if mode == "transcode" else 0.0))
            if queue:
                self._build_queue(path)
            idx, total = self.playlist_index + 1, len(self.playlist)
        tail = f"  [{_MODE_TEXT.get(mode, mode)}]"
        if sub_note:
            tail += f"  {sub_note}"
        self.log(f"已推送 → {dev.name}: {title}{tail}")
        return {
            "ok": True, "url": url, "title": title, "device": dev.name,
            "mode": mode, "sub_note": sub_note, "sub_filter_on": bool(sub_filter),
            "variant": variant,
            "playlist_index": idx, "playlist_total": total,
            "autonext": self.autonext,
        }

    # ---- 选路：直连 / 缓存 / 实时转码 ----
    def _play_url(self, path: str, dev: Optional[Device] = None, start: float = 0.0):
        """返回 (url, mode, sub_filter, sub_note, variant)。"""
        info = media.probe(path)
        sub_filter, sub_note = media.plan_subtitles(path, info, self.sub_mode)
        # 烧了字幕的产物和没烧的是两个不同的视频，缓存必须分开存
        variant = "sub" if sub_filter else ""

        cached = media.find_cached(self.root_dir, path, variant)
        # 拖动定位时不走缓存：缓存是从头开始的那一份，落点时间对不上
        if cached and not self.force_transcode and start <= 0.05:
            return self.server.url_for(cached, self.local_ip), "cache", sub_filter, sub_note, variant

        if info.direct and not self.force_transcode and start <= 0.05:
            return self.server.url_for(path, self.local_ip), "direct", sub_filter, sub_note, variant

        if not self.auto_transcode and not self.force_transcode:
            # 用户关掉了自动转码，照直推（可能播不了，但尊重用户选择）
            return self.server.url_for(path, self.local_ip), "direct", sub_filter, sub_note, variant

        rel = self._rel(path)
        return (self.server.live_url_for(rel, _live_container(dev), start),
                "transcode", sub_filter, sub_note, variant)

    # ---- 格式分析 ----
    def analyze(self, rel_or_abs: str) -> Dict:
        path = self._resolve_file(rel_or_abs)
        if not path:
            return {"error": "文件不存在"}
        info = media.probe(path)
        d = info.to_dict()
        d["cached"] = bool(media.find_cached(self.root_dir, path))
        d["cached_sub"] = bool(media.find_cached(self.root_dir, path, "sub"))
        d["name"] = os.path.basename(path)
        d["ffmpeg_ok"] = media.available()
        d["encoder"] = media.pick_encoder() if media.available() else ""
        # 字幕情况（外挂 + 可烧的内嵌轨）
        tracks = media.all_subtitles(path, info)
        d["subtitles"] = [{"kind": t.get("kind"), "label": t.get("label"),
                           "codec": t.get("codec"), "name": t.get("name")} for t in tracks]
        d["has_subtitle"] = bool(tracks)
        d["sub_mode"] = self.sub_mode
        d["sub_note"] = media.plan_subtitles(path, info, self.sub_mode)[1]
        return d

    def analyze_dir(self, rel_dir: str = "", budget: float = 1.2, limit: int = 500) -> Dict:
        """批量探测目录内文件的可播性，带时间预算；未完成的留给下次调用。"""
        data = self.browse(rel_dir)
        target = self._abs(rel_dir)
        # 一次 listdir 拿到整目录的字幕主名，避免每个文件各扫一遍目录
        with_sub = media.subtitle_stems(target) if target and os.path.isdir(target) else set()
        results: Dict[str, Dict] = {}
        pending: List[str] = []
        deadline = time.time() + budget
        for f in (data.get("files") or [])[:limit]:
            p = self._resolve_file(f["rel"])
            if not p:
                continue
            if not media.is_cached(p):
                pending.append(p)          # 没探过 → 交给后台预热，本次先不给结果
                continue
            try:
                info = media.probe(p)
            except Exception:
                continue
            results[f["rel"]] = {
                "direct": info.direct,
                "summary": info.summary,
                "cached": bool(media.find_cached(self.root_dir, p)),
                "has_sub": (os.path.splitext(f["name"])[0].lower() in with_sub
                            or any(t.get("text") for t in info.subs_embedded)),
            }
        if pending:
            self._start_warmer(pending)
        return {"dir": rel_dir, "results": results, "pending": len(pending)}

    def _start_warmer(self, paths: List[str], workers: int = 4):
        with self._warm_lock:
            self._warm_pending = list(paths)
            if any(t.is_alive() for t in self._warm_threads):
                return
            # ffprobe 单次约 400ms（大文件 moov 在尾部要寻道），并发几个快得多
            self._warm_threads = [
                threading.Thread(target=self._warm_loop, daemon=True, name=f"tvcast-warm{i}")
                for i in range(workers)
            ]
            for t in self._warm_threads:
                t.start()

    def _warm_loop(self):
        while True:
            with self._warm_lock:
                if not self._warm_pending:
                    return
                path = self._warm_pending.pop(0)
            try:
                if not media.is_cached(path):
                    media.probe(path)
            except Exception:
                pass

    # ---- 缓存转码（整片转成可拖进度的 MP4）----
    def transcode_start(self, rel_or_abs: str, then_cast: bool = False) -> Dict:
        path = self._resolve_file(rel_or_abs)
        if not path:
            return {"ok": False, "error": "文件不存在"}
        if not media.available():
            return {"ok": False, "error": "找不到 ffmpeg / ffprobe，无法转码"}
        with self.lock:
            if self._job and self._job.state == "running":
                return {"ok": False, "error": "已有转码任务在进行中"}
            info = media.probe(path)
            # 这是用户主动要求的整片转码，字幕有就烧（除非用户关了字幕）
            sub_filter, sub_note = media.plan_subtitles(
                path, info, self.sub_mode, force_burn=True)
            variant = "sub" if sub_filter else ""
            out = media.cache_path_for(self.root_dir, path, variant)
            job = media.TranscodeJob(path, out, info, log=self.log,
                                     sub_filter=sub_filter, sub_note=sub_note)
            self._job = job
        job.start()
        return {"ok": True, "src": os.path.basename(path), "out": out,
                "sub_note": sub_note, "variant": variant}

    def transcode_status(self) -> Dict:
        with self.lock:
            job = self._job
        if job is None:
            return {"state": "idle", "progress": 0, "percent": 0}
        st = job.status()
        st["ffmpeg_ok"] = media.available()
        return st

    def transcode_cancel(self) -> Dict:
        with self.lock:
            job = self._job
        if job is None:
            return {"ok": False, "error": "没有转码任务"}
        job.cancel()
        return {"ok": True}

    def seek_live(self, target: float) -> Dict:
        """转码播放时"拖动进度条"的实现 —— 重推定位。

        实时转码推出的是一条"活"的流（MPEG-TS / fMP4 分片），里面没有索引、
        也没有时长，播放器根本无处可定位；DLNA 的 Seek 动作对这种流一律失败
        （电视回 701/711 之类的错误），所以**绝不能**对转码流调 dlna.seek()。

        正确的做法是：让服务端从目标时间点重新起一条流（`?t=<秒>`，ffmpeg 的
        `-ss`），再把新地址推给电视。整条链路是：
            用户拖进度条 → 本方法 → cast(start=target) → 新 URL 带 ?t=
            → SetAVTransportURI + Play → 电视重新拉流，从该点开始播
        代价是每次定位要重新起流（NVENC 下约 0.5~2 秒），所以手机端要防抖。
        """
        try:
            target = max(0.0, float(target))
        except (TypeError, ValueError):
            return {"ok": False, "error": "定位时间无效"}
        with self.lock:
            cur = self.current_file
        if not cur:
            return {"ok": False, "error": "没有正在播放的内容"}
        # 注意：不用手改 _max_pos / _user_stopped ——下面的 cast() 内部会走
        # _reset_watch(live_offset=target)，把"上一集已播到末尾"的判断连同进度基准
        # 一起重置；新流的进度会从 target 重新累积，不会在起点误触发自动连播。
        m, s = divmod(int(target), 60)
        self.log(f"转码流定位 → {m}:{s:02d}（从该点重新起流）")
        res = self.cast(cur, queue=False, start=target)
        if res.get("ok"):
            res["seeked"] = int(target)
        return res

    def action(self, name: str, value: Optional[int] = None) -> Dict:
        with self.lock:
            dev = self.current_device
            live = (self.play_mode == "transcode" and self.current_file is not None)
        if dev is None:
            return {"ok": False, "error": "还没有选择电视"}
        # 转码流没有可定位的索引：走"重推定位"，而不是把 Seek 丢给电视
        if name == "seek" and live:
            return self.seek_live(value if value is not None else 0)
        try:
            if dev.kind == "chromecast":
                if castmod is None:
                    raise RuntimeError("缺少 pychromecast")
                if name == "play":
                    castmod.play(dev)
                elif name == "pause":
                    castmod.pause(dev)
                elif name == "stop":
                    castmod.stop(dev)
                elif name == "seek":
                    castmod.seek(dev, int(value or 0))
                elif name == "volume":
                    self.volume = max(0, min(100, int(value or 0)))
                    castmod.set_volume(dev, self.volume)
                else:
                    return {"ok": False, "error": f"未知动作 {name}"}
            else:
                if name == "play":
                    dlna.play(dev)
                elif name == "pause":
                    dlna.pause(dev)
                elif name == "stop":
                    dlna.stop(dev)
                elif name == "seek":
                    dlna.seek(dev, int(value or 0))
                elif name == "volume":
                    self.volume = max(0, min(100, int(value or 0)))
                    dlna.set_volume(dev, self.volume)
                else:
                    return {"ok": False, "error": f"未知动作 {name}"}
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)
            self.log(f"遥控 {name} 失败: {e}")
            return {"ok": False, "error": str(e)}
        if name == "stop":
            # 用户主动停止 ≠ 播完，别在这里触发自动连播
            with self.lock:
                self._user_stopped = True
        self.log(f"遥控 {name}" + (f" = {value}" if value is not None else ""))
        return {"ok": True}

    def set_options(self, **kw) -> Dict:
        """批量改设置（连播 / 字幕模式 / 转码策略）。"""
        with self.lock:
            if "autonext" in kw:
                self.autonext = bool(kw["autonext"])
            if "sub_mode" in kw and kw["sub_mode"] in ("auto", "burn", "off"):
                self.sub_mode = str(kw["sub_mode"])
            if "auto_transcode" in kw:
                self.auto_transcode = bool(kw["auto_transcode"])
            if "force_transcode" in kw:
                self.force_transcode = bool(kw["force_transcode"])
            out = {
                "ok": True,
                "autonext": self.autonext,
                "sub_mode": self.sub_mode,
                "auto_transcode": self.auto_transcode,
                "force_transcode": self.force_transcode,
            }
        self.log(f"设置: 连播={out['autonext']} 字幕={out['sub_mode']} "
                 f"自动转码={out['auto_transcode']} 强制转码={out['force_transcode']}")
        return out

    # ==================================================================
    # 自动连播
    # ==================================================================
    def _check_autonext(self, transport: str, position: float, duration: float,
                        idle_reason: Optional[str] = None) -> bool:
        """跟踪播放状态跃迁，判断「本集播完了」并异步切下一集。

        DLNA 没有明确的"播放结束"事件：多数渲染器播完和用户按停止都只报 STOPPED。
        所以只在「曾经真的播起来过」且「进度已经推到接近片尾」时才认定是播完。
        Chromecast 有 idle_reason=FINISHED，可以直接采信。
        """
        with self.lock:
            if not self.autonext or self.current_file is None or len(self.playlist) < 2:
                self._last_transport = transport
                return False
            if self._autonext_busy or self._user_stopped:
                self._last_transport = transport
                return False

            prev = self._last_transport
            self._last_transport = transport
            if position and position > self._max_pos:
                self._max_pos = float(position)
            if transport in ("PLAYING", "PAUSED"):
                self._watched_playing = True

            ended = False
            if idle_reason and str(idle_reason).upper() in ("FINISHED", "END_OF_STREAM"):
                ended = True
            elif (transport == "STOPPED" and prev in ("PLAYING", "PAUSED", "TRANSITIONING")
                  and self._watched_playing):
                if duration and duration > 30:
                    ended = self._max_pos >= float(duration) * 0.9
                else:
                    ended = self._max_pos >= 60
            if not ended:
                return False
            self._autonext_busy = True
            idx, total = self.playlist_index, len(self.playlist)

        self.log(f"本集播放结束，自动播下一个（{idx + 2}/{total}）")
        threading.Thread(target=self._auto_advance, daemon=True,
                         name="tvcast-autonext").start()
        return True

    def _auto_advance(self):
        try:
            res = self.next_track()
        except Exception as e:  # noqa: BLE001
            res = {"ok": False, "error": str(e)}
        if not res.get("ok"):
            self.log(f"没有下一集可播：{res.get('error', '')}")
            with self.lock:
                self._autonext_busy = False

    # ==================================================================
    # 状态
    # ==================================================================
    def status(self) -> Dict:
        with self.lock:
            dev = self.current_device
            cur = self.current_file
            job = self._job
            mode = self.play_mode
            pl_total = len(self.playlist)
            pl_index = self.playlist_index
            pl_dir = self.playlist_dir
            autonext = self.autonext
            sub_mode = self.sub_mode
            next_name = (os.path.basename(self.playlist[pl_index + 1])
                         if 0 <= pl_index < pl_total - 1 else None)
            prev_name = (os.path.basename(self.playlist[pl_index - 1])
                         if pl_index > 0 else None)

        needs_tc, has_cache, has_sub, sub_note = False, False, False, ""
        src_duration = 0.0
        if cur:
            try:
                info = media.probe(cur)
                needs_tc = not info.direct
                src_duration = info.duration
                has_cache = bool(media.find_cached(self.root_dir, cur)
                                 or media.find_cached(self.root_dir, cur, "sub"))
                has_sub = bool(media.all_subtitles(cur, info))
                sub_note = media.plan_subtitles(cur, info, sub_mode)[1]
            except Exception:
                pass

        state = {
            "device": dev.name if dev else None,
            "device_key": dev.key if dev else None,
            "device_online": bool(dev),
            "file": os.path.basename(cur) if cur else None,
            "file_rel": self._rel(cur) if cur else None,
            "position": 0,
            "duration": 0,
            "transport": "UNKNOWN",
            "volume": self.volume,
            "last_error": self.last_error,
            # 格式兼容 / 转码
            "play_mode": mode,
            # 进度条能不能拖：native = 播放器自己就能定位；restream = 由服务端重推流定位
            "seek_mode": "restream" if mode == "transcode" else "native",
            "live_offset": int(self._live_offset),
            "needs_transcode": needs_tc,
            "has_cache": has_cache,
            "auto_transcode": self.auto_transcode,
            "force_transcode": self.force_transcode,
            "ffmpeg_ok": media.available(),
            "encoder": media.pick_encoder() if media.available() else "",
            "transcode": job.status() if job else {"state": "idle", "progress": 0, "percent": 0},
            # 边播边缓存：正在播的这条流是否也在落盘、落盘后是否封装成功
            "caching": bool(media.finalize_status().get("current")),
            "cache_finalize": media.finalize_status(),
            # 字幕
            "has_subtitle": has_sub,
            "sub_mode": sub_mode,
            "sub_note": sub_note,
            # 播放队列 / 自动连播
            "autonext": autonext,
            "playlist_len": pl_total,
            "playlist_index": pl_index + 1 if pl_index >= 0 else 0,
            "playlist_dir": pl_dir,
            "next_name": next_name,
            "prev_name": prev_name,
        }
        if dev is None:
            return state
        idle_reason = None
        try:
            if dev.kind == "chromecast":
                if castmod is None:
                    return state
                st = castmod.status(dev)
                if st:
                    state["position"] = int(st.get("current_time") or 0)
                    state["duration"] = int(st.get("duration") or 0)
                    state["transport"] = _map_cast_state(st.get("player_state"))
                    idle_reason = st.get("idle_reason")
                    v = st.get("volume")
                    if isinstance(v, int) and v >= 0:
                        state["volume"] = v
            else:
                r = dlna.get_position(dev)
                if r:
                    pos, dur, tstate = r
                    state["position"] = pos
                    state["duration"] = dur
                    state["transport"] = tstate
                v = dlna.get_volume(dev)
                if isinstance(v, int):
                    state["volume"] = v
        except Exception:
            pass

        # 直播转码流（TS / fMP4 分片）本身不含时长，播放器也不会回报进度 —— 界面就会
        # 一直是 0:00 / 0:00，看不出到底播没播、更没法拖。这里用源片时长补总长，
        # 用「流起始点 + 开始播放以来的墙钟时间」估算已播时长（电视按 1 倍速播，
        # 两者基本一致）。因为定位会重新起流并改写 _live_offset，估算照样连续。
        if mode == "transcode" and dev is not None:
            dev_dur = int(state.get("duration") or 0)
            if dev.kind == "chromecast" and dev_dur > 0:
                state["seek_mode"] = "native"      # 设备自己报得出时长/进度，采信它
            else:
                if state["transport"] == "PLAYING":
                    with self.lock:
                        if self._live_clock is None:
                            self._live_clock = time.time()
                        est = int(self._live_offset + (time.time() - self._live_clock))
                    state["position"] = max(0, est)
                    if src_duration > 0:
                        state["duration"] = int(src_duration)
                        state["position"] = min(state["position"], max(0, int(src_duration) - 1))
                    state["position_estimated"] = True
                else:
                    with self.lock:
                        self._live_clock = None
                        # 还没播起来（或刚重推完在缓冲）时，就停在这次流的起点上，
                        # 否则界面会先跳到 0 再跳回来，看着像定位失败
                        state["position"] = int(self._live_offset)
                state["seek_mode"] = "restream"

        # 播完自动切下一集（内部会看状态跃迁，不会重复触发）
        try:
            self._check_autonext(state["transport"], state["position"],
                                 state["duration"], idle_reason)
        except Exception:
            pass
        return state


_MODE_TEXT = {
    "direct": "直连",
    "cache": "用缓存",
    "transcode": "实时转码",
}

# 直播流的容器按目标设备选：
#   DLNA 电视（实测 Android stagefright）→ MPEG-TS，流式容器无需索引，即点即播
#   Chromecast 的默认接收器 → fragmented MP4 更稳
# 反过来的组合都实测失败过：fMP4 推给这台电视，它 2 秒内就断开。
_CONTAINER_TS = "ts"
_CONTAINER_MP4 = "mp4"


def _live_container(dev: Optional[Device]) -> str:
    if dev is not None and getattr(dev, "kind", "") == "chromecast":
        return _CONTAINER_MP4
    return _CONTAINER_TS


def _map_cast_state(s) -> str:
    s = str(s or "")
    if "PLAYING" in s:
        return "PLAYING"
    if "PAUSED" in s:
        return "PAUSED"
    if "BUFFERING" in s:
        return "TRANSITIONING"
    if "IDLE" in s:
        return "STOPPED"
    return s or "UNKNOWN"
