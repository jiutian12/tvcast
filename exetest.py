"""实测打包后的 exe：验证 /live/ TS 直播链路（不改动用户正在运行的实例）。"""
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

import tests_env as ENV

EXE = ENV.EXE
ROOT = ENV.ROOT
PORT = 18790
TV_IP = ENV.TV_IP
_SRC = ENV.pick_video()
assert _SRC, ENV.MISSING_SAMPLE
REL = os.path.relpath(_SRC, ROOT).replace("\\", "/")


def api(path, obj=None, timeout=60):
    try:
        if obj is None:
            req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}")
        else:
            req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}",
                                         data=json.dumps(obj).encode(),
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        return {"_err": f"HTTP {e.code}"}
    except Exception as e:
        return {"_err": f"{type(e).__name__}: {e}"}


proc = subprocess.Popen([EXE, "--web", "--root", ROOT, "--port", str(PORT)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    print("等待 exe 启动…")
    ok = False
    for _ in range(30):
        time.sleep(1)
        if api("/api/ping", timeout=5).get("ok"):
            ok = True
            break
    assert ok, "exe 没有起来"
    print("✅ exe 启动，网页服务可用")

    st = api("/api/state")
    print("状态:", {k: st.get(k) for k in ("play_mode", "ffmpeg_ok", "encoder", "auto_transcode")})
    assert st.get("ffmpeg_ok"), "exe 里找不到 ffmpeg"

    print("扫描设备…")
    devs = []
    for attempt in range(4):
        sc = api("/api/scan", {}, timeout=90)
        devs = sc.get("devices") or []
        print(f"  第{attempt+1}次扫描:", [(d["name"], d["host"], d["kind"]) for d in devs])
        if devs:
            break
        time.sleep(4)
    dev = next((d for d in devs if d["host"] == TV_IP), None)
    assert dev, "没发现电视"

    r = api("/api/cast", {"rel": REL, "device_key": dev["key"]}, timeout=90)
    print("投屏返回: mode=%s" % r.get("mode"))
    print("推送地址:", r.get("url"))
    assert r.get("ok"), r
    assert r["mode"] == "transcode", r
    assert "/live/" in r["url"] and r["url"].endswith(".ts"), "未走 /live/ TS 路由"

    u = urllib.parse.urlparse(r["url"])
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{u.path}", method="HEAD")
    with urllib.request.urlopen(req, timeout=20) as resp:
        print(f"HEAD /live/ -> {resp.status} Content-Type={resp.headers.get('Content-Type')} "
              f"Content-Length={resp.headers.get('Content-Length')}")
        assert resp.headers.get("Content-Type") == "video/mp2t"
        assert resp.headers.get("Content-Length") != "0", "HEAD 回了 0，播放器会当成空文件"

    print("\n观察 25 秒…")
    held = 0
    for i in range(5):
        time.sleep(5)
        o = subprocess.run(["netstat", "-an"], capture_output=True,
                           timeout=15).stdout.decode("utf-8", "ignore")
        est = [l for l in o.splitlines()
               if str(PORT) in l and TV_IP in l and "ESTABLISHED" in l]
        s = api("/api/state", timeout=20)
        if est:
            held += 1
        print(f"  [+{(i+1)*5}s] 电视活跃连接={len(est)} transport={s.get('transport')} "
              f"pos={s.get('position')} dur={s.get('duration')} vol={s.get('volume')}")
    assert held == 5, "电视没有持续拉流"

    api("/api/action", {"action": "stop"}, timeout=30)
    print("\n✅ 打包后的 exe：TS 直播链路完全正常")
finally:
    try:
        proc.terminate()
        proc.wait(timeout=15)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    print("已关闭测试实例（PID %s）" % proc.pid)
