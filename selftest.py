"""转码链路自测：用真实片源验证 分析 / 实时转码 / 缓存转码。"""
import glob
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tests_env as ENV
from tvcast.core import CastCore
from tvcast.httpserver import TvCastServer
from tvcast import media


def main():
    print("=" * 62)
    print("ffmpeg:", media.FFMPEG)
    print("ffprobe:", media.FFPROBE)
    print("编码器:", media.pick_encoder())
    print("=" * 62)

    # 取一个真实片源（优先挑需要转码的），剪 25 秒做测试样本
    src = ENV.pick_incompatible(min_bytes=5_000_000)
    if not src:
        print(ENV.MISSING_SAMPLE); return 1
    print("源文件:", os.path.basename(src), f"({os.path.getsize(src)//1048576} MB)")

    root = tempfile.mkdtemp(prefix="tvcast_test_")
    sample = os.path.join(root, "sample_av1.mp4")
    subprocess.run([media.FFMPEG, "-v", "error", "-y", "-ss", "60", "-t", "25",
                    "-i", src, "-c", "copy", sample], check=True)
    print("样本:", os.path.getsize(sample) // 1024, "KB")

    core = CastCore(root, "127.0.0.1")
    srv = TvCastServer(core, root, port=0, host="127.0.0.1")
    core.server = srv
    srv.start()
    base = f"http://127.0.0.1:{srv.port}"
    print("服务端口:", srv.port)

    def get(p, n=None, timeout=90):
        r = urllib.request.urlopen(base + p, timeout=timeout)
        return r.read(n) if n else r.read()

    def post(p, obj):
        req = urllib.request.Request(base + p, data=json.dumps(obj).encode(),
                                     headers={"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req, timeout=90).read())

    # ---- 1. 格式分析 ----
    a = json.loads(get("/api/analyze?rel=sample_av1.mp4"))
    print("\n[1] 格式分析")
    print("    编码:", a.get("summary"), "| 可直连:", a.get("direct"))
    print("    需转视频:", a.get("need_video_transcode"), "需转音频:", a.get("need_audio_transcode"))
    for r in a.get("reasons", []):
        print("    ·", r)
    assert a.get("direct") is False, "AV1 样本不应判为可直连"
    print("    ✔ 正确识别为需转码")

    # ---- 2. 实时转码推流 ----
    print("\n[2] 实时转码推流 (/stream/)")
    t0 = time.time()
    r = urllib.request.urlopen(base + "/stream/sample_av1.mp4", timeout=120)
    head = r.read(400_000)
    first = time.time() - t0
    r.close()
    is_mp4 = b"ftyp" in head[:64]
    print(f"    首包延迟 {first:.2f}s，收到 {len(head)//1024} KB，MP4 头: {is_mp4}")
    assert len(head) > 100_000, "转码流输出太少"
    assert is_mp4, "不是 MP4 流"
    print("    ✔ 实时转码可用（电视可秒开）")

    # ---- 3. 缓存转码 ----
    print("\n[3] 缓存转码 (/api/transcode)")
    st = post("/api/transcode", {"rel": "sample_av1.mp4"})
    print("    启动:", st)
    assert st.get("ok"), st
    t0 = time.time()
    last = ""
    while time.time() - t0 < 300:
        s = json.loads(get("/api/transcode/status"))
        line = f"    {s['state']:9} {s.get('percent',0):5.1f}%  eta={s.get('eta')}"
        if line != last:
            print(line); last = line
        if s["state"] in ("done", "error", "cancelled"):
            break
        time.sleep(1.5)
    s = json.loads(get("/api/transcode/status"))
    assert s["state"] == "done", f"转码未成功: {s}"
    out = s["out"]
    print("    输出:", os.path.basename(out), f"({os.path.getsize(out)//1024} KB)")

    probe = subprocess.run([media.FFPROBE, "-v", "error", "-show_entries",
                            "stream=codec_type,codec_name", "-of", "csv=p=0", out],
                           capture_output=True).stdout.decode().strip().replace("\n", " ")
    print("    产物编码:", probe)
    assert "h264" in probe and "aac" in probe, "产物不是 H.264+AAC"

    # ---- 4. 缓存文件能被直连 ----
    a2 = json.loads(get("/api/analyze?rel=sample_av1.mp4"))
    print("\n[4] 再次分析: 有缓存 =", a2.get("cached"))

    # ---- 5. 缓存文件走 Range ----
    rel_out = os.path.relpath(out, root).replace("\\", "/")
    rr = urllib.request.urlopen(urllib.request.Request(
        base + "/media/" + rel_out, headers={"Range": "bytes=0-999"}))
    print("[5] 缓存产物 Range:", rr.status, rr.headers.get("Content-Range"))
    assert rr.status == 206

    srv.stop()
    print("\n" + "=" * 62)
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
