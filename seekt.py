"""验证「转码播放也能拖进度条」—— 重推定位链路。

分四层验证：
  L1 选路：start>0 时必须走实时转码，且 URL 带 ?t=N（绝不能命中缓存/直连）
  L2 命令：?t=N 真的变成了 ffmpeg 的 -ss N
  L3 动作：play_mode=transcode 时 action("seek", N) 触发重推（cast(start=N)），
          而不是把 Seek 丢给电视（那条路对无索引的直播流必然失败）
  L4 画面：从 ?t=0 与 ?t=60 两条真实流各抓第一帧，与源片对应时刻的参考帧比 PSNR，
          确认"定位到 60s"出来的确实是 60s 的画面
"""
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import types
import urllib.request

import tests_env as ENV
from tvcast import media
from tvcast.core import CastCore
from tvcast.httpserver import TvCastServer

ROOT = ENV.ROOT
LOCAL_IP = ENV.LOCAL_IP
PORT = 18791
SRC = ENV.pick_incompatible(min_bytes=20 << 20)
if not SRC:
    raise SystemExit(ENV.MISSING_SAMPLE)
REL = os.path.relpath(SRC, ROOT).replace("\\", "/")
TMP = tempfile.mkdtemp(prefix="tvcast_seek_")
LOG = []
FF = media.FFMPEG

assert os.path.isfile(SRC), f"测试源片不存在: {SRC}"
info = media.probe(SRC)
print(f"源片: {os.path.basename(SRC)}")
print(f"      {info.summary}  时长 {info.duration:.1f}s  direct={info.direct}")
assert info.duration > 120, f"源片太短({info.duration}s)，无法验证 60s 定位"

# ==========================================================================
print("\n" + "=" * 62)
print("L1  选路：定位时必须走转码，且 URL 带 ?t=")
print("=" * 62)
core = CastCore(ROOT, LOCAL_IP, log=LOG.append)
srv = TvCastServer(core, ROOT, port=PORT, host="0.0.0.0")
core.server = srv
srv.start()
print("服务端口:", srv.port)

# 反例：哪怕缓存/直连都命中，start>0 也必须走转码
u0 = srv.live_url_for(REL, "ts", 0)
u60 = srv.live_url_for(REL, "ts", 60)
print("t=0  :", u0)
print("t=60 :", u60)
assert u60.endswith(".ts?t=60"), u60
assert "?t=" not in u0, u0

real_find_cached = media.find_cached
real_direct = info.direct
media.find_cached = lambda *a, **k: os.path.join(TMP, "fake-cache.mp4")   # 假装有缓存
open(os.path.join(TMP, "fake-cache.mp4"), "wb").write(b"x" * 1024)
try:
    url, mode, _, _, _ = core._play_url(SRC, None, 0.0)
    print(f"start=0  → mode={mode}")
    assert mode == "cache", f"start=0 应优先用缓存，实际 {mode}"
    url, mode, _, _, _ = core._play_url(SRC, None, 30.0)
    print(f"start=30 → mode={mode}  url={url}")
    assert mode == "transcode", f"start>0 竟然走了 {mode}（会播错时间点）"
    assert url.endswith("?t=30"), url
finally:
    media.find_cached = real_find_cached

# ==========================================================================
print("\n" + "=" * 62)
print("L2  命令：?t=N 变成 ffmpeg 的 -ss N")
print("=" * 62)
for t in (0.0, 30.0, 60.0, 125.5):
    cmd = media.build_live_cmd(SRC, info, t, sub_filter=None, container="ts")
    joined = " ".join(cmd)
    if t > 0:
        assert f"-ss {t:.3f}" in joined, f"缺 -ss {t}: {joined[:160]}"
        assert joined.index("-ss") < joined.index("-i"), "-ss 必须在 -i 之前（快速定位）"
    else:
        assert "-ss" not in cmd, "-ss 0 不该出现"
    print(f"  t={t:<6} → {'-ss ' + format(t, '.3f') if t else '(无 -ss)'}   ✓ 且在 -i 之前")

# ==========================================================================
print("\n" + "=" * 62)
print("L3  动作：transcode 模式下 seek 走重推，而不是丢给电视")
print("=" * 62)
core.current_device = types.SimpleNamespace(kind="dlna", name="fake-tv")
core.current_file = SRC
captured = {}


def fake_cast(rel_or_abs, device_key=None, queue=True, start=0.0):
    captured.update(rel=rel_or_abs, queue=queue, start=start)
    return {"ok": True, "mode": "transcode"}


real_cast = core.cast
core.cast = fake_cast
try:
    core.play_mode = "transcode"
    captured.clear()
    r = core.action("seek", 42)
    print(f"  play_mode=transcode → action(seek,42) → cast(start={captured.get('start')}, queue={captured.get('queue')})")
    assert captured.get("start") == 42.0, f"没按目标时间重推: {captured}"
    assert captured.get("queue") is False, "重推定位不该重建播放队列"
    assert r.get("ok") and r.get("seeked") == 42
    assert core.play_mode == "transcode"

    # 反例：direct 模式下必须走电视原生 Seek，不能误触发重推
    hit = {}
    import tvcast.dlna as dlna_mod
    real_seek = dlna_mod.seek
    dlna_mod.seek = lambda dev, sec: hit.update(sec=sec)
    try:
        core.play_mode = "direct"
        captured.clear()
        core.action("seek", 42)
        print(f"  play_mode=direct    → action(seek,42) → dlna.seek({hit.get('sec')})  cast被调用={bool(captured)}")
        assert hit.get("sec") == 42, "直连模式应走电视原生定位"
        assert not captured, "直连模式不该重推"
    finally:
        dlna_mod.seek = real_seek
finally:
    core.cast = real_cast
    core.play_mode = ""

# ==========================================================================
print("\n" + "=" * 62)
print("L4  画面：?t=60 出来的确实是 60s 的画面")
print("=" * 62)
core.current_device = None
core.current_file = None


def pull(url, dst, seconds=6.0, max_bytes=12 << 20):
    """从直播流里抓一段原始 TS 落盘（够解码出画面即可）。"""
    got = 0
    t0 = time.time()
    with urllib.request.urlopen(url, timeout=15) as resp:
        print(f"    HTTP {resp.status}  {resp.headers.get('Content-Type')}")
        with open(dst, "wb") as f:
            while got < max_bytes and time.time() - t0 < seconds:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
    print(f"    {os.path.basename(dst)}: {got / 1048576:.1f} MB")
    return got


def first_frame(src, dst, t=None):
    cmd = [FF, "-hide_banner", "-loglevel", "error", "-y"]
    if t is not None:
        cmd += ["-ss", f"{t:.3f}"]
    cmd += ["-i", src, "-frames:v", "1", "-f", "image2", dst]
    r = subprocess.run(cmd, capture_output=True, timeout=180)
    return r.returncode == 0 and os.path.isfile(dst) and os.path.getsize(dst) > 0


def psnr(a, b):
    r = subprocess.run([FF, "-hide_banner", "-loglevel", "info", "-i", a, "-i", b,
                        "-lavfi", "psnr", "-f", "null", "-"],
                       capture_output=True, timeout=120)
    txt = r.stderr.decode("utf-8", "ignore")
    m = re.search(r"average:([0-9.]+|inf)", txt)
    if not m:
        return None
    return float("inf") if m.group(1) == "inf" else float(m.group(1))


live0 = os.path.join(TMP, "live0.ts")
live60 = os.path.join(TMP, "live60.ts")
pull(srv.live_url_for(REL, "ts", 0), live0)
pull(srv.live_url_for(REL, "ts", 60), live60)

f_live0 = os.path.join(TMP, "f_live0.png")
f_live60 = os.path.join(TMP, "f_live60.png")
assert first_frame(live0, f_live0), "t=0 流解不出画面"
assert first_frame(live60, f_live60), "t=60 流解不出画面"
print("  两条流都能解出画面 ✓")

ref0 = os.path.join(TMP, "ref0.png")
ref60 = os.path.join(TMP, "ref60.png")
first_frame(SRC, ref0, t=0)
first_frame(SRC, ref60, t=60)

p_live60_ref60 = psnr(f_live60, ref60)
p_live60_ref0 = psnr(f_live60, ref0)
p_live0_ref0 = psnr(f_live0, ref0)
print(f"  PSNR(?t=60 首帧, 源片60s) = {p_live60_ref60}")
print(f"  PSNR(?t=60 首帧, 源片 0s) = {p_live60_ref0}")
print(f"  PSNR(?t=0  首帧, 源片 0s) = {p_live0_ref0}")
assert p_live60_ref60 is not None and p_live60_ref0 is not None
assert p_live60_ref60 > p_live60_ref0, (
    f"?t=60 的画面更像片头(0s)而不是 60s，定位没生效：{p_live60_ref60} vs {p_live60_ref0}")
assert p_live0_ref0 is not None and p_live0_ref0 > p_live60_ref0, (
    "?t=0 的首帧应更接近片头")
print("  ✅ 定位到 60s 出来的就是 60s 的画面")

# ==========================================================================
print("\n" + "=" * 62)
print("L5  状态：定位后进度按新起点估算、且标记可拖")
print("=" * 62)
core.current_device = types.SimpleNamespace(kind="dlna", name="fake-tv", key="k1",
                                            host="127.0.0.1", render_control_url="")
core.current_file = SRC
core.play_mode = "transcode"
core._reset_watch(live_offset=60.0)
core._live_clock = time.time() - 5          # 假装已经播了 5 秒
core._watched_playing = True
import tvcast.dlna as _dlna
_rp = _dlna.get_position
_dlna.get_position = lambda dev: (0, 0, "PLAYING")   # 直播流：电视只会回报 0/0
try:
    st = core.status()
finally:
    _dlna.get_position = _rp
print(f"  seek_mode={st['seek_mode']}  live_offset={st['live_offset']}  "
      f"position={st['position']}  duration={st['duration']}  estimated={st.get('position_estimated')}")
assert st["seek_mode"] == "restream", "转码模式必须标记为需要重推定位"
assert st["live_offset"] == 60, st["live_offset"]
assert 63 <= st["position"] <= 70, f"进度估算没带上起点: {st['position']}"
assert st["duration"] == int(info.duration), st["duration"]

core.play_mode = "direct"
core._reset_watch()
st = core.status()
assert st["seek_mode"] == "native", st["seek_mode"]
print(f"  直连模式 seek_mode={st['seek_mode']} ✓")

srv.stop()
print("\n=== 转码进度条定位：全部通过 ===")
print("临时目录:", TMP)
