"""图形界面（tkinter）。

本机用这个窗口操作；手机扫右侧二维码即可打开网页版遥控器，
两边共用同一个 CastCore，状态实时同步。
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Dict, List, Optional

from . import __app_name__, __version__
from .core import CastCore
from .discovery import Device
from .httpserver import DEFAULT_PORT, TvCastServer
from .util import (MEDIA_EXT, config_dir, default_media_dir, get_local_ip,
                   human_size, human_time, save_config)

DEFAULT_MEDIA_DIR = default_media_dir()
POLL_MS = 1500


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.local_ip = get_local_ip()
        self.log_queue: queue.Queue = queue.Queue()
        self.core = CastCore(DEFAULT_MEDIA_DIR, self.local_ip, log=self.log)
        self.server = TvCastServer(self.core, DEFAULT_MEDIA_DIR, port=DEFAULT_PORT)
        self.core.server = self.server

        self.file_map: Dict[str, str] = {}
        self.device_map: Dict[str, Device] = {}
        self.busy = False
        self._duration = 0
        self._seeking = False
        self._vol_job = None
        self._closing = False

        self._build_ui()
        self._start_server()
        self._load_files(DEFAULT_MEDIA_DIR)
        self._poll_log()
        self._poll_status()

    # ==================================================================
    # UI
    # ==================================================================
    def _build_ui(self):
        root = self.root
        root.title(f"{__app_name__} v{__version__}  ·  本机 {self.local_ip}")
        root.geometry("1060x720")
        root.minsize(940, 640)

        try:
            style = ttk.Style()
            if "vista" in style.theme_names():
                style.theme_use("vista")
        except Exception:
            pass

        outer = ttk.Frame(root, padding=10)
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 8))
        ttk.Label(header, text="📺  电视投屏", font=("Microsoft YaHei UI", 16, "bold")).pack(side="left")
        self.lbl_srv = ttk.Label(header, text="服务启动中…", foreground="#666")
        self.lbl_srv.pack(side="right")

        body = ttk.Frame(outer)
        body.pack(fill="both", expand=True)

        # ---------------- 左：片源 ----------------
        left = ttk.LabelFrame(body, text="① 选择片源", padding=8)
        left.pack(side="left", fill="both", expand=True, padx=(0, 6))

        row = ttk.Frame(left); row.pack(fill="x", pady=(0, 6))
        self.var_dir = tk.StringVar(value=DEFAULT_MEDIA_DIR)
        ttk.Entry(row, textvariable=self.var_dir).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="浏览", width=6, command=self._choose_dir).pack(side="left", padx=(4, 0))
        ttk.Button(row, text="刷新", width=6, command=self._refresh_files).pack(side="left", padx=(4, 0))

        listwrap = ttk.Frame(left); listwrap.pack(fill="both", expand=True)
        self.lst_files = tk.Listbox(listwrap, activestyle="none", exportselection=False)
        fs = ttk.Scrollbar(listwrap, orient="vertical", command=self.lst_files.yview)
        self.lst_files.configure(yscrollcommand=fs.set)
        self.lst_files.pack(side="left", fill="both", expand=True)
        fs.pack(side="right", fill="y")
        self.lst_files.bind("<Double-Button-1>", lambda e: self._do_cast())

        self.lbl_file = ttk.Label(left, text="未选择（双击列表可直接投屏）", foreground="#888", wraplength=400)
        self.lbl_file.pack(fill="x", pady=(6, 0))

        # ---------------- 右：设备 + 手机遥控 ----------------
        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True, padx=(6, 0))

        devbox = ttk.LabelFrame(right, text="② 选择电视", padding=8)
        devbox.pack(fill="both", expand=True)

        row2 = ttk.Frame(devbox); row2.pack(fill="x", pady=(0, 6))
        self.btn_scan = ttk.Button(row2, text="🔍 扫描局域网电视", command=self._scan_devices)
        self.btn_scan.pack(side="left")
        self.lbl_scan = ttk.Label(row2, text="", foreground="#888")
        self.lbl_scan.pack(side="left", padx=8)

        listwrap2 = ttk.Frame(devbox); listwrap2.pack(fill="both", expand=True)
        self.lst_devices = tk.Listbox(listwrap2, activestyle="none", exportselection=False)
        fs2 = ttk.Scrollbar(listwrap2, orient="vertical", command=self.lst_devices.yview)
        self.lst_devices.configure(yscrollcommand=fs2.set)
        self.lst_devices.pack(side="left", fill="both", expand=True)
        fs2.pack(side="right", fill="y")
        self.lst_devices.bind("<<ListboxSelect>>", lambda e: self._on_device_select())

        # 手机遥控
        phone = ttk.LabelFrame(right, text="📱 手机遥控（同一 Wi-Fi 打开）", padding=8)
        phone.pack(fill="x", pady=(8, 0))

        pr = ttk.Frame(phone); pr.pack(fill="x")
        self.qr_canvas = tk.Canvas(pr, width=140, height=140, highlightthickness=0, bg="#ffffff")
        self.qr_canvas.pack(side="left", padx=(0, 10))

        pc = ttk.Frame(pr); pc.pack(side="left", fill="both", expand=True)
        ttk.Label(pc, text="手机在浏览器打开：", foreground="#666").pack(anchor="w")
        self.var_url = tk.StringVar(value="—")
        entry = ttk.Entry(pc, textvariable=self.var_url, state="readonly", font=("Consolas", 10))
        entry.pack(fill="x", pady=(4, 6))
        ttk.Button(pc, text="📋 复制链接", command=self._copy_url).pack(anchor="w")
        ttk.Label(pc, text="电视与本机需同一局域网", foreground="#888", font=("Microsoft YaHei UI", 8)).pack(anchor="w", pady=(6, 0))

        # ---------------- 操作 ----------------
        ops = ttk.Frame(outer)
        ops.pack(fill="x", pady=10)
        self.btn_cast = ttk.Button(ops, text="▶  开始投屏", command=self._do_cast, width=16)
        self.btn_cast.pack(side="left")
        self.lbl_now = ttk.Label(ops, text="尚未投屏", foreground="#333")
        self.lbl_now.pack(side="left", padx=12)

        self.var_auto = tk.BooleanVar(value=True)
        ttk.Checkbutton(ops, text="兼容模式(自动转码)", variable=self.var_auto,
                        command=self._on_auto_toggle).pack(side="right")
        self.btn_tc = ttk.Button(ops, text="⚡ 转码成 MP4", command=self._start_transcode)
        self.btn_tc.pack(side="right", padx=8)

        # ---------------- 遥控 ----------------
        rc = ttk.LabelFrame(outer, text="③ 遥控", padding=8)
        rc.pack(fill="x", pady=(0, 8))

        line = ttk.Frame(rc); line.pack(fill="x")
        ttk.Button(line, text="⏸ 暂停", width=8, command=lambda: self._rc("pause")).pack(side="left")
        ttk.Button(line, text="▶ 播放", width=8, command=lambda: self._rc("play")).pack(side="left", padx=4)
        ttk.Button(line, text="⏹ 停止", width=8, command=lambda: self._rc("stop")).pack(side="left", padx=4)

        ttk.Label(line, text="   音量").pack(side="left")
        self.var_vol = tk.IntVar(value=30)
        self.scl_vol = ttk.Scale(line, from_=0, to=100, variable=self.var_vol,
                                 orient="horizontal", length=170, command=self._on_vol_change)
        self.scl_vol.pack(side="left", padx=6)
        self.lbl_vol = ttk.Label(line, text="30", width=3)
        self.lbl_vol.pack(side="left")

        prog = ttk.Frame(rc); prog.pack(fill="x", pady=(8, 0))
        ttk.Label(prog, text="进度").pack(side="left")
        self.var_pos = tk.DoubleVar(value=0)
        self.scl_pos = ttk.Scale(prog, from_=0, to=100, variable=self.var_pos, orient="horizontal")
        self.scl_pos.pack(side="left", fill="x", expand=True, padx=6)
        self.scl_pos.bind("<ButtonPress-1>", self._on_seek_press)
        self.scl_pos.bind("<ButtonRelease-1>", self._on_seek_release)
        self.lbl_time = ttk.Label(prog, text="--:-- / --:--", width=16)
        self.lbl_time.pack(side="left")

        tcr = ttk.Frame(rc); tcr.pack(fill="x", pady=(6, 0))
        ttk.Label(tcr, text="转码").pack(side="left")
        self.pb_tc = ttk.Progressbar(tcr, mode="determinate", maximum=100, length=220)
        self.pb_tc.pack(side="left", padx=6)
        self.lbl_tc = ttk.Label(tcr, text="空闲", foreground="#888")
        self.lbl_tc.pack(side="left")

        # ---------------- 日志 ----------------
        lf = ttk.LabelFrame(outer, text="日志", padding=4)
        lf.pack(fill="both", expand=False)
        self.txt_log = tk.Text(lf, height=6, wrap="word", state="disabled",
                               background="#1e1e1e", foreground="#d4d4d4", insertbackground="#fff",
                               font=("Consolas", 9))
        lsb = ttk.Scrollbar(lf, orient="vertical", command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=lsb.set)
        self.txt_log.pack(side="left", fill="both", expand=True)
        lsb.pack(side="right", fill="y")

        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.log(f"就绪 · 本机 IP {self.local_ip}")

    # ==================================================================
    # 日志
    # ==================================================================
    def log(self, msg: str):
        self.log_queue.put(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def _poll_log(self):
        try:
            while True:
                line = self.log_queue.get_nowait()
                self.txt_log.configure(state="normal")
                self.txt_log.insert("end", line + "\n")
                self.txt_log.see("end")
                self.txt_log.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(200, self._poll_log)

    # ==================================================================
    # 服务
    # ==================================================================
    def _start_server(self):
        try:
            self.server.start()
            url = self.server.web_url(self.local_ip)
            self.var_url.set(url)
            self.lbl_srv.configure(text=f"本机 {self.local_ip} · 端口 {self.server.port}")
            self.log(f"服务已启动：{url}")
            if self.server.port != DEFAULT_PORT:
                self.log(f"⚠ 端口 {DEFAULT_PORT} 被占用（可能已有另一个实例在运行），"
                         f"本次改用 {self.server.port}，手机请用新地址")
            self.log(f"  网页遥控 {url}")
            self.log(f"  媒体流   http://{self.local_ip}:{self.server.port}/media/…")
            self._draw_qr(url)
        except Exception as e:  # noqa: BLE001
            self.log(f"服务启动失败: {e}")
            messagebox.showerror("错误", f"服务启动失败：{e}")

    def _draw_qr(self, url: str):
        try:
            import qrcode
            qr = qrcode.QRCode(box_size=1, border=1)
            qr.add_data(url)
            qr.make(fit=True)
            matrix = qr.get_matrix()
        except Exception as e:  # noqa: BLE001
            self.log(f"二维码生成失败: {e}")
            return
        n = len(matrix)
        canvas_w = 140
        cell = max(2, canvas_w // (n + 2))
        px = (canvas_w - cell * n) // 2
        self.qr_canvas.delete("all")
        self.qr_canvas.create_rectangle(0, 0, canvas_w, canvas_w, fill="#ffffff", outline="")
        for y, row in enumerate(matrix):
            for x, val in enumerate(row):
                if val:
                    x0, y0 = px + x * cell, px + y * cell
                    self.qr_canvas.create_rectangle(x0, y0, x0 + cell, y0 + cell,
                                                    fill="#000000", outline="")

    def _copy_url(self):
        url = self.var_url.get()
        if not url or url == "—":
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(url)
            self.log(f"已复制：{url}")
        except Exception as e:  # noqa: BLE001
            self.log(f"复制失败: {e}")

    # ==================================================================
    # 片源
    # ==================================================================
    def _choose_dir(self):
        d = filedialog.askdirectory(initialdir=self.var_dir.get() or DEFAULT_MEDIA_DIR)
        if d:
            self.var_dir.set(d)
            save_config(root=d)   # 记住这次选的目录，下次启动直接用它
            self._refresh_files()

    def _refresh_files(self):
        d = self.var_dir.get().strip()
        if not os.path.isdir(d):
            self.log(f"目录不存在: {d}")
            return
        # 换目录要重建服务，因为媒体根目录变了
        try:
            self.server.stop()
        except Exception:
            pass
        self.server = TvCastServer(self.core, d, port=self.server.port or DEFAULT_PORT)
        self.core.server = self.server
        self.core.root_dir = os.path.abspath(d)
        self._start_server()
        self._load_files(d)

    def _load_files(self, root_dir: str, limit: int = 3000):
        self.lst_files.delete(0, "end")
        self.file_map.clear()
        if not os.path.isdir(root_dir):
            return

        found: List[str] = []
        for dirpath, dirs, files in os.walk(root_dir):
            dirs[:] = [x for x in dirs if not x.startswith(".")]
            for f in files:
                if os.path.splitext(f)[1].lower() in MEDIA_EXT:
                    found.append(os.path.join(dirpath, f))
                    if len(found) >= limit:
                        break
            if len(found) >= limit:
                break

        found.sort(key=lambda p: (-os.path.getsize(p) if os.path.exists(p) else 0))
        for p in found:
            try:
                size = human_size(os.path.getsize(p))
            except OSError:
                size = "?"
            label = f"{size:>9}  {os.path.relpath(p, root_dir)}"
            self.lst_files.insert("end", label)
            self.file_map[label] = p
        self.log(f"载入 {len(found)} 个媒体文件")

    def _on_file_select(self):
        sel = self.lst_files.curselection()
        if not sel:
            return
        path = self.file_map.get(self.lst_files.get(sel[0]), "")
        self.lbl_file.configure(text=path, foreground="#333")
        if path:
            threading.Thread(target=self._probe_selected, args=(path,), daemon=True).start()

    def _probe_selected(self, path: str):
        r = self.core.analyze(path)
        self._ui(lambda: self._show_probe(r, path))

    def _show_probe(self, r: dict, path: str):
        if not r or r.get("error"):
            return
        if r.get("cached"):
            tag = "📦 已有转码缓存（可拖进度）"
        elif r.get("direct"):
            tag = "✅ 可直连播放"
        else:
            tag = "⚡ 电视不支持，需转码"
        txt = f"{path}\n格式 {r.get('summary','?')} · {tag}"
        if not r.get("direct") and r.get("reasons"):
            txt += "（" + "；".join(r["reasons"][:2]) + "）"
        if not r.get("ffmpeg_ok"):
            txt += "\n⚠ 未找到 ffmpeg，无法转码"
        self.lbl_file.configure(text=txt, foreground="#333")

    # ------------------------------------------------------------------
    def _on_auto_toggle(self):
        self.core.auto_transcode = bool(self.var_auto.get())
        self.log("兼容模式: " + ("已开启（不支持的格式自动转码）" if self.core.auto_transcode else "已关闭（只推原始文件）"))

    def _start_transcode(self):
        path = self._selected_file()
        if not path:
            messagebox.showwarning("提示", "请先在左侧选择一个片源")
            return
        if not self.core.devices and not self.core.current_device:
            self.log("提示：还没选电视，但转码可以先跑")

        def work():
            r = self.core.transcode_start(path)
            self._ui(lambda: self._tc_started(r))

        threading.Thread(target=work, daemon=True).start()

    def _tc_started(self, r: dict):
        if not r.get("ok"):
            messagebox.showerror("转码失败", r.get("error", ""))
            return
        self.log(f"开始转码: {r.get('src')} → {os.path.basename(r.get('out',''))}")
        self.lbl_tc.configure(text="启动中…")

    def _selected_file(self) -> Optional[str]:
        sel = self.lst_files.curselection()
        return self.file_map.get(self.lst_files.get(sel[0])) if sel else None

    # ==================================================================
    # 设备
    # ==================================================================
    def _scan_devices(self):
        if self.busy:
            return
        self.busy = True
        self.btn_scan.configure(state="disabled")
        self.lbl_scan.configure(text="扫描中…")

        def work():
            devs = self.core.scan()
            self._ui(lambda: self._scan_done(devs))

        threading.Thread(target=work, daemon=True).start()

    def _ui(self, fn):
        """从工作线程安全地回到 UI 线程；窗口已关闭时静默丢弃。"""
        if self._closing:
            return
        try:
            self.root.after(0, fn)
        except Exception:
            pass

    def _scan_done(self, devs: List[Device]):
        self.device_map.clear()
        self.lst_devices.delete(0, "end")
        for d in devs:
            label = str(d)
            self.lst_devices.insert("end", label)
            self.device_map[label] = d
        self.busy = False
        self.btn_scan.configure(state="normal")
        if devs:
            self.lbl_scan.configure(text=f"发现 {len(devs)} 台")
        else:
            self.lbl_scan.configure(text="未发现设备")
            self.log("未发现设备：检查电视与本机是否同网段、电视端投屏开关、防火墙。")

    def _on_device_select(self):
        sel = self.lst_devices.curselection()
        if not sel:
            return
        d = self.device_map.get(self.lst_devices.get(sel[0]))
        if d:
            self.core.select_device(d.key)

    # ==================================================================
    # 投屏 / 遥控
    # ==================================================================
    def _do_cast(self):
        path = self._selected_file()
        if not path:
            messagebox.showwarning("提示", "请先在左侧选择一个片源")
            return
        dev = self.core.current_device
        if not dev:
            messagebox.showwarning("提示", "请先在右侧扫描并选择一台电视")
            return

        self.btn_cast.configure(state="disabled")
        self.log(f"投屏 → {dev}")

        def work():
            r = self.core.cast(path)
            self._ui(lambda: self._cast_done(r, os.path.basename(path)))

        threading.Thread(target=work, daemon=True).start()

    def _cast_done(self, r: dict, title: str):
        self.btn_cast.configure(state="normal")
        if not r.get("ok"):
            self.log(f"❌ 投屏失败: {r.get('error')}")
            messagebox.showerror("投屏失败",
                                 f"{r.get('error')}\n\n常见原因：\n· 电视不支持该格式/协议\n"
                                 f"· 电视端投屏功能未开启\n· 防火墙拦截了 {self.server.port} 端口")
        else:
            mode = r.get("mode")
            extra = {"transcode": " · 实时转码播放", "cache": " · 用转码缓存",
                     "direct": ""}.get(mode, "")
            self.lbl_now.configure(text=f"正在播放: {title}{extra}", foreground="#0a7d00")

    def _rc(self, action: str):
        if not self.core.current_device:
            messagebox.showinfo("提示", "还没选择电视")
            return

        def work():
            self.core.action(action)

        threading.Thread(target=work, daemon=True).start()

    def _on_vol_change(self, _val=None):
        v = int(float(self.var_vol.get()))
        self.lbl_vol.configure(text=str(v))
        if self._vol_job:
            self.root.after_cancel(self._vol_job)
        self._vol_job = self.root.after(300, lambda: self._send_volume(v))

    def _send_volume(self, v: int):
        if not self.core.current_device:
            return
        threading.Thread(target=lambda: self.core.action("volume", v), daemon=True).start()

    def _on_seek_press(self, _evt=None):
        self._seeking = True

    def _on_seek_release(self, _evt=None):
        if self.core.current_device and self._duration > 0:
            target = int(self.var_pos.get())
            threading.Thread(target=lambda: self.core.action("seek", target), daemon=True).start()
        self._seeking = False

    # ==================================================================
    # 状态轮询
    # ==================================================================
    def _poll_status(self):
        def work():
            if self._seeking or self._closing:
                return
            s = self.core.status()
            self._ui(lambda: self._apply_status(s))

        threading.Thread(target=work, daemon=True).start()
        if not self._closing:
            try:
                self.root.after(POLL_MS, self._poll_status)
            except Exception:
                pass

    def _apply_status(self, s: dict):
        if self._seeking:
            return
        pos, dur = s.get("position", 0), s.get("duration", 0)
        if dur > 0:
            self._duration = dur
            self.scl_pos.configure(to=dur)
            self.var_pos.set(min(pos, dur))
        est = bool(s.get("position_estimated"))
        live = s.get("play_mode") == "transcode"
        suffix = "  转码定位" if live else ("  直播估算" if est else "")
        self.lbl_time.configure(text=f"{human_time(pos)} / {human_time(dur)}{suffix}")
        if s.get("file"):
            self.lbl_now.configure(text=f"正在播放: {s['file']}", foreground="#0a7d00")
        vol = s.get("volume")
        if isinstance(vol, int) and self.root.focus_get() is not self.scl_vol:
            self.var_vol.set(vol)
            self.lbl_vol.configure(text=str(vol))

        # 转码进度
        tc = s.get("transcode") or {}
        state = tc.get("state")
        if state == "running":
            pct = tc.get("percent", 0) or 0
            self.pb_tc.configure(value=pct)
            eta = tc.get("eta")
            self.lbl_tc.configure(
                text=f"转码中 {pct:.0f}%" + (f" · 剩余约 {human_time(eta)}" if eta else ""))
        elif state == "done":
            self.pb_tc.configure(value=100)
            self.lbl_tc.configure(text="转码完成 ✓")
        elif state == "error":
            self.lbl_tc.configure(text="转码失败 ✗")
        elif state == "cancelled":
            self.lbl_tc.configure(text="已取消")
        else:
            self.pb_tc.configure(value=0)
            self.lbl_tc.configure(text="空闲")

        auto = s.get("auto_transcode")
        if isinstance(auto, bool) and self.var_auto.get() != auto:
            self.var_auto.set(auto)

        # 转码播放时进度条照样能拖：拖动后由服务端从目标时间点重新起一条流转码
        want_disabled = not s.get("file")
        if bool(self.scl_pos.instate(["disabled"])) != want_disabled:
            self.scl_pos.state(["disabled"] if want_disabled else ["!disabled"])
        if live and s.get("file") and state not in ("running", "done", "error", "cancelled"):
            self.lbl_tc.configure(text="转码播放中 · 可拖进度（拖动后重新起流）")
        if s.get("file") and not s.get("ffmpeg_ok"):
            self.lbl_tc.configure(text="未找到 ffmpeg")

    # ==================================================================
    def _on_close(self):
        self._closing = True
        try:
            try:
                import tvcast.cast as castmod
                for d in self.core.devices:
                    if d.kind == "chromecast":
                        castmod.disconnect(d)
            except Exception:
                pass
            self.server.stop()
        finally:
            self.root.destroy()


def _enable_hidpi():
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _install_crash_guard():
    import traceback

    def hook(exc_type, exc, tb):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        try:
            log_dir = config_dir()
            os.makedirs(log_dir, exist_ok=True)
            with open(os.path.join(log_dir, "error.log"), "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n" + text + "\n")
        except Exception:
            pass
        try:
            from tkinter import messagebox
            messagebox.showerror("TV投屏 出错了", text[-1500:])
        except Exception:
            pass

    sys.excepthook = hook


def main():
    _install_crash_guard()
    _enable_hidpi()
    root = tk.Tk()
    App(root)
    root.mainloop()
