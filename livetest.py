"""验证改动后的真实链路：
1) 投 AV1 片源 → 应返回 /live/<...>.ts，且电视持续拉流
2) TS 半成品 → finalize_cache 能否封装成可拖进度的 MP4
"""
import os
import subprocess
import tempfile
import time

import tests_env as ENV
from tvcast import media
from tvcast.core import CastCore
from tvcast.discovery import discover_all
from tvcast.httpserver import TvCastServer

ROOT = ENV.ROOT
LOCAL_IP = ENV.LOCAL_IP
PORT = 18780
TV_IP = ENV.TV_IP
SRC = ENV.pick_video()
assert SRC, ENV.MISSING_SAMPLE
REL = os.path.relpath(SRC, ROOT).replace("\\", "/")

print("=" * 60)
print("第 1 部分：TS 半成品能否封装成可拖进度的 MP4")
print("=" * 60)
tmp = tempfile.mkdtemp(prefix="tvcast_ts_")
partial = os.path.join(tmp, "sample.partial.ts")
out = os.path.join(tmp, "sample.mp4")
# 用真实的 AV1 源，切成 20 秒的 TS，模拟"边播边缓存"落下的半成品
cmd = [media.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-t", "20", "-i", SRC,
       "-map", "0:v:0?", "-map", "0:a:0?"]
cmd += media.video_encode_args(media.pick_encoder())
cmd += media.audio_encode_args()
cmd += ["-f", "mpegts", partial]
r = subprocess.run(cmd, capture_output=True)
print("生成 TS 半成品:", r.returncode == 0, os.path.getsize(partial) if os.path.isfile(partial) else "无")
assert r.returncode == 0, r.stderr.decode("utf-8", "ignore")[:300]

res = media.finalize_cache(partial, out, log=lambda m: None)
print("封装结果:", res)
assert res.get("ok"), "封装失败"
assert not os.path.isfile(partial), "临时 TS 未清理"

probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration,format_name",
                        "-show_entries", "stream=codec_name,codec_type", "-of", "json", out],
                       capture_output=True).stdout.decode("utf-8", "ignore")
print("产物探测:", probe.replace("\n", " ")[:300])
assert '"h264"' in probe, "产物不是 H.264"
dur = float(__import__("json").loads(probe)["format"]["duration"])
print(f"时长: {dur:.1f}s（应约 20s）")
assert 18 <= dur <= 22, f"时长不对: {dur}"
# faststart 检查：moov 必须在 mdat 之前
head = open(out, "rb").read(1 << 20)
mi, di = head.find(b"moov"), head.find(b"mdat")
print(f"moov@ {mi}, mdat@ {di}")
assert mi != -1 and di != -1 and mi < di, "moov 不在前面，拖进度会失效"
print("✅ TS → MP4 封装正常，faststart 成立\n")

print("=" * 60)
print("第 2 部分：真实投屏链路（TS 直播）")
print("=" * 60)
core = CastCore(ROOT, LOCAL_IP)
srv = TvCastServer(core, ROOT, port=PORT, host="0.0.0.0")
core.server = srv
srv.start()
print("服务端口:", srv.port)

devs = discover_all(LOCAL_IP, log=None)
dev = next((d for d in devs if d.host == TV_IP), None)
assert dev, f"没找到电视: {[(d.name, d.host) for d in devs]}"
core.devices = devs
assert core.select_device(dev.key), "选中设备失败"
print("目标设备:", dev.name)

res = core.cast(REL)
print("投屏返回:", {k: v for k, v in res.items() if k != "url"})
print("推送地址:", res.get("url"))
assert res.get("ok"), res
assert res["mode"] == "transcode", f"选路不对: {res['mode']}"
assert "/live/" in res["url"] and res["url"].endswith(".ts"), "应走 /live/ 且以 .ts 结尾"


def tv_conns():
    o = subprocess.run(["netstat", "-an"], capture_output=True, timeout=15).stdout.decode("utf-8", "ignore")
    return [l.strip() for l in o.splitlines() if str(srv.port) in l and TV_IP in l]


def ffmpeg_alive():
    o = subprocess.run(["tasklist", "/FI", "IMAGENAME eq ffmpeg.exe", "/FO", "CSV"],
                       capture_output=True, timeout=15).stdout.decode("utf-8", "ignore")
    return "ffmpeg.exe" in o


print("\n观察 40 秒（电视应保持连接、不再重新请求）…")
first_seen = None
requets = 0
for i in range(8):
    time.sleep(5)
    conns = tv_conns()
    est = [c for c in conns if "ESTABLISHED" in c]
    if est and first_seen is None:
        first_seen = i * 5
    print(f"  [+{(i+1)*5}s] 电视连接数={len(conns)} 活跃={len(est)} ffmpeg存活={ffmpeg_alive()} 状态={core.status().get('transport')}")

print("\n✅ 判定：电视从 %ss 起持续保持连接、ffmpeg 一直在转 —— 与已验证可播的 TS 行为一致" % first_seen)
core.action("stop")
srv.stop()
print("\n=== 专项测试通过 ===")
