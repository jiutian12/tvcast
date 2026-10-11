"""一体化 HTTP 服务：手机网页 UI + 控制 API + 媒体流。

一个端口干三件事：
  GET  /                     手机遥控网页
  GET  /media/<相对路径>      媒体流（支持 Range，供电视回连拉流）
  /api/*                     控制接口（JSON）

电视靠 /media/* 拉流，手机靠 / 和 /api/* 遥控，两者共用一个端口。
"""

from __future__ import annotations

import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import media
from .util import guess_mime
from .webui import INDEX_HTML

DEFAULT_PORT = 18632

# 正在推的转码流：同一条片子的上一轮进程要在新一轮开始前掐掉。
# 拖动定位会重起一条流，若电视没马上断开旧连接，就会有两个 ffmpeg 抢编码器。
_live_lock = threading.Lock()
_live_procs: dict = {}


class _Handler(BaseHTTPRequestHandler):
    server_version = "TVCast/2.0"
    protocol_version = "HTTP/1.1"
    media_root: str = ""

    def log_message(self, fmt, *args):
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    # ---------------- 基础响应 ----------------
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, HEAD, OPTIONS")

    def _json(self, obj, status: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _html(self, text: str):
        body = text.encode("utf-8")
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _body_json(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    # ---------------- 路由 ----------------
    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self):
        if self.path.startswith("/media/"):
            return self._serve_media(body=False)
        if self.path.startswith("/live/"):
            # 直播 TS 流没有可信长度，但绝不能回 Content-Length: 0 ——
            # 播放器先探一下看到 0 就当成空文件，直接放弃播放。
            self.close_connection = True
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Connection", "close")
            self.end_headers()
            return
        if self.path.startswith("/stream/"):
            self.close_connection = True
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Connection", "close")
            self.end_headers()
            return
        self.send_response(200)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        try:
            self._do_get()
        except Exception as e:  # noqa: BLE001
            self._fail(e)

    def do_POST(self):
        try:
            self._do_post()
        except Exception as e:  # noqa: BLE001
            self._fail(e)

    def _fail(self, e: Exception):
        import traceback
        traceback.print_exc()
        try:
            self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
        except Exception:
            pass

    def _do_get(self):
        u = urlparse(self.path)
        path = u.path
        core = getattr(self.server, "core", None)

        if path in ("/", "/index.html"):
            return self._html(INDEX_HTML)
        if path.startswith("/media/"):
            return self._serve_media(body=True)
        if path.startswith("/live/"):
            return self._serve_stream(u, container="ts")
        if path.startswith("/stream/"):
            return self._serve_stream(u, container="mp4")
        if path == "/api/state":
            return self._json(core.status() if core else {})
        if path == "/api/analyze":
            q = parse_qs(u.query).get("rel", [""])[0]
            return self._json(core.analyze(q) if core else {})
        if path == "/api/analyze_dir":
            q = parse_qs(u.query).get("dir", [""])[0]
            return self._json(core.analyze_dir(q) if core else {})
        if path == "/api/transcode/status":
            return self._json(core.transcode_status() if core else {})
        if path == "/api/browse":
            q = parse_qs(u.query).get("dir", [""])[0]
            return self._json(core.browse(q) if core else {})
        if path == "/api/search":
            q = parse_qs(u.query).get("q", [""])[0]
            return self._json({"files": core.search(q) if core else []})
        if path == "/api/ping":
            return self._json({"ok": True, "app": "TVCast"})
        return self._json({"error": "not found"}, 404)

    def _do_post(self):
        u = urlparse(self.path)
        path = u.path
        core = getattr(self.server, "core", None)
        if core is None:
            return self._json({"ok": False, "error": "服务未就绪"}, 503)

        if path == "/api/scan":
            core.scan()
            return self._json({"ok": True, "devices": core.devices_json()})
        if path == "/api/select":
            key = self._body_json().get("key", "")
            ok = core.select_device(key)
            return self._json({"ok": ok, "error": "" if ok else "设备不存在"})
        if path == "/api/cast":
            body = self._body_json()
            return self._json(core.cast(body.get("rel", ""), body.get("device_key")))
        if path == "/api/action":
            body = self._body_json()
            return self._json(core.action(body.get("action", ""), body.get("value")))
        if path == "/api/transcode":
            body = self._body_json()
            return self._json(core.transcode_start(body.get("rel", "")))
        if path == "/api/transcode/cancel":
            return self._json(core.transcode_cancel())
        if path == "/api/config":
            body = self._body_json()
            if "auto_transcode" in body:
                core.auto_transcode = bool(body["auto_transcode"])
            if "force_transcode" in body:
                core.force_transcode = bool(body["force_transcode"])
            core.log(f"转码设置: 自动={core.auto_transcode} 强制={core.force_transcode}")
            return self._json({"ok": True, "auto_transcode": core.auto_transcode,
                               "force_transcode": core.force_transcode})
        return self._json({"error": "not found"}, 404)

    # ---------------- 实时转码推流 ----------------
    def _serve_stream(self, u, container: str = "ts"):
        core = getattr(self.server, "core", None)
        if core is None:
            return self._json({"error": "服务未就绪"}, 503)

        prefix = "/live/" if container == "ts" else "/stream/"
        rel = unquote(u.path)[len(prefix):]
        # 为了让播放器按扩展名判断容器，URL 尾部带了 .ts；解析时再摘掉
        if container == "ts" and rel.lower().endswith(".ts"):
            stripped = rel[:-3]
            if core._resolve_file(stripped):  # noqa: SLF001
                rel = stripped
        qs = parse_qs(u.query)
        try:
            start = float(qs.get("t", ["0"])[0])
        except (TypeError, ValueError):
            start = 0.0

        path = core._resolve_file(rel)  # noqa: SLF001
        if not path:
            return self._json({"error": "文件不存在"}, 404)

        # 重推定位：先把同一条片子的上一轮转码掐掉，避免两个 ffmpeg 并行占编码器
        key = f"{container}|{os.path.realpath(path)}"
        with _live_lock:
            old = _live_procs.get(key)
        if old is not None and old.poll() is None:
            try:
                old.kill()
            except Exception:
                pass
        if start > 0.05:
            core.log(f"重推转码流，从 {int(start // 60)}:{int(start % 60):02d} 开始")

        info = media.probe(path)
        proc = media.start_live(path, info, start, log=core.log, container=container)
        if proc is None:
            return self._json({"error": "转码启动失败（检查 ffmpeg）"}, 500)
        with _live_lock:
            _live_procs[key] = proc

        # ---- 边播边缓存：同一条流顺手落一份盘（从中途续播的流不缓存）----
        partial: Optional[str] = None
        fh = None
        if start <= 0.05:
            cand = media.partial_path_for(core.root_dir, path, container=container)
            if media.claim_partial(cand):
                try:
                    os.makedirs(os.path.dirname(cand), exist_ok=True)
                    fh = open(cand, "wb")
                    partial = cand
                    core.log("本次边播边缓存，播完即可拖动进度")
                except Exception as e:  # noqa: BLE001
                    media.release_partial(cand)
                    fh, partial = None, None
                    core.log(f"无法创建缓存文件（本次只播不缓存）: {e}")

        # 直播流：无 Content-Length，读到连接关闭为止
        self.close_connection = True
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "video/mp2t" if container == "ts" else "video/mp4")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

        total = 0
        natural_end = False
        try:
            assert proc.stdout is not None
            while True:
                chunk = proc.stdout.read(64 * 1024)
                if not chunk:
                    natural_end = True
                    break
                self.wfile.write(chunk)
                total += len(chunk)
                if fh is not None:
                    try:
                        fh.write(chunk)
                    except Exception:  # noqa: BLE001
                        # 落盘失败不影响正在播的流，只是本次不缓存
                        try:
                            fh.close()
                        except Exception:
                            pass
                        fh = None
                        if partial:
                            media.discard_partial(partial)
                            partial = None
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception:
            pass
        finally:
            if fh is not None:
                try:
                    fh.close()
                except Exception:
                    pass
                fh = None
            try:
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
            with _live_lock:
                if _live_procs.get(key) is proc:
                    _live_procs.pop(key, None)

            tail = getattr(proc, "_stderr_tail", b"")
            if total < 64 * 1024:
                msg = tail.decode("utf-8", "ignore").strip()[:300]
                core.log(f"转码流异常结束（仅输出 {total} 字节）{('：' + msg) if msg else ''}")
            else:
                core.log(f"转码流结束（已输出 {total / 1048576:.1f} MB）")

            if partial:
                if natural_end and proc.returncode == 0:
                    media.finalize_in_background(
                        partial, media.cache_path_for(core.root_dir, path), core.log)
                else:
                    # 中途停掉/被电视断开：半截片不缓存，免得下次误当成完整文件
                    media.discard_partial(partial)
                    core.log("转码未跑完，本次不写入缓存")

    # ---------------- 媒体流 ----------------
    def _resolve_media(self) -> Optional[str]:
        path = unquote(urlparse(self.path).path)
        rel = path[len("/media/"):] if path.startswith("/media/") else path.lstrip("/")
        rel = rel.replace("\\", "/")
        root = os.path.realpath(self.media_root)
        full = os.path.realpath(os.path.join(root, rel))
        if full != root and not full.startswith(root + os.sep):
            return None
        return full if os.path.isfile(full) else None

    def _serve_media(self, body: bool):
        full = self._resolve_media()
        if full is None:
            self.send_response(404)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        size = os.path.getsize(full)
        ctype = guess_mime(full)
        range_header = self.headers.get("Range")
        start, end, partial = 0, size - 1, False

        if range_header and range_header.startswith("bytes="):
            spec = range_header[len("bytes="):].split(",")[0].strip()
            if "-" in spec:
                a, b = spec.split("-", 1)
                try:
                    if a == "":
                        start = max(0, size - int(b))
                        end = size - 1
                    else:
                        start = int(a)
                        end = int(b) if b else size - 1
                    end = min(end, size - 1)
                    if start > end or start >= size:
                        raise ValueError
                    partial = True
                except ValueError:
                    self.send_response(416)
                    self._cors()
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self._cors()
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()

        if not body:
            return
        try:
            remaining = length
            with open(full, "rb") as f:
                f.seek(start)
                while remaining > 0:
                    chunk = f.read(min(256 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


class TvCastServer:
    """一体化服务封装：网页 + API + 媒体流。"""

    def __init__(self, core, root: str, port: int = DEFAULT_PORT,
                 host: str = "0.0.0.0", verbose: bool = False):
        self.core = core
        self.root = os.path.realpath(os.path.abspath(root))
        self.host = host
        self.verbose = verbose
        self.requested_port = port
        self.actual_port = port
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def port(self) -> int:
        if self._httpd is not None:
            return self._httpd.server_address[1]
        return self.actual_port

    def start(self):
        if self._httpd is not None:
            return
        handler = type("BoundHandler", (_Handler,), {"media_root": self.root})
        ThreadingHTTPServer.allow_reuse_address = True
        ThreadingHTTPServer.daemon_threads = True

        # Windows 上 SO_REUSEADDR 会允许"抢占"已被占用的端口，
        # 于是 bind 成功、请求却全被老进程接走 —— 必须先显式探测端口是否空闲。
        candidates = [p for p in (self.requested_port, 0) if p == 0 or _port_free(self.host, p)]
        if not candidates:
            candidates = [0]

        httpd = None
        last_err: Optional[Exception] = None
        for port in candidates:
            try:
                httpd = ThreadingHTTPServer((self.host, port), handler)
                break
            except OSError as e:
                last_err = e
        if httpd is None:
            raise last_err or OSError("无法绑定端口")

        httpd.core = self.core            # type: ignore[attr-defined]
        httpd.verbose = self.verbose      # type: ignore[attr-defined]
        self._httpd = httpd
        self.actual_port = httpd.server_address[1]
        self._thread = threading.Thread(target=httpd.serve_forever, name="tvcast-http", daemon=True)
        self._thread.start()

    def stop(self):
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            try:
                httpd.shutdown()
                httpd.server_close()
            except Exception:
                pass

    def _rel_of(self, local_path: str) -> str:
        root = os.path.realpath(self.root)
        full = os.path.realpath(os.path.abspath(local_path))
        try:
            rel = os.path.relpath(full, root)
        except ValueError:
            rel = os.path.basename(full)
        return rel.replace("\\", "/")

    def url_for(self, local_path: str, local_ip: str) -> str:
        return f"http://{local_ip}:{self.port}/media/{quote(self._rel_of(local_path))}"

    def live_url_for(self, rel: str, container: str = "ts", start: float = 0.0) -> str:
        """实时转码流的地址。

        TS 走 /live/，并且**路径结尾必须带 .ts** —— 播放器会按扩展名判断容器，
        实测本机电视就是靠这个后缀把流当 TS 解。

        start > 0 时带上 `?t=<秒>`：这条流会从该时间点重新开始转码，用于
        "拖动进度条"（转码流没有索引，只能靠重起一条流来实现定位）。
        """
        rel = rel.replace("\\", "/")
        q = f"?t={int(start)}" if start and start > 0 else ""
        if container == "ts":
            tail = rel if rel.lower().endswith(".ts") else rel + ".ts"
            return f"http://{self.core.local_ip}:{self.port}/live/{quote(tail)}{q}"
        return f"http://{self.core.local_ip}:{self.port}/stream/{quote(rel)}{q}"

    def web_url(self, local_ip: str) -> str:
        return f"http://{local_ip}:{self.port}/"


# 兼容旧名字（CLI 里还在用）
MediaServer = TvCastServer


def _port_free(host: str, port: int) -> bool:
    """不带 SO_REUSEADDR 地试绑一次：Windows 下这才是可靠的"端口是否被占"判定。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("", 0))
    p = s.getsockname()[1]
    s.close()
    return p
