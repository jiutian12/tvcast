"""实测打包后的 exe 是否带上了「转码可拖进度条」：
  1) 网页里已换成新文案（不再说"本次不能拖"）
  2) /api/state 暴露 seek_mode
  3) 真实请求 /live/<片>.ts?t=90，抓第一帧与源片 90s 处比 PSNR
  4) HEAD /live/ 回归：仍是 video/mp2t 且 Content-Length 不为 0
不需要电视。
"""
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

import tests_env as ENV

EXE = ENV.EXE
ROOT = ENV.ROOT
PORT = 18794
SRC = ENV.pick_incompatible(min_bytes=20 << 20)
REL = os.path.relpath(SRC, ROOT).replace("\\", "/") if SRC else ""
TMP = tempfile.mkdtemp(prefix="tvcast_exeseek_")
FF = ENV.FFMPEG

assert os.path.isfile(EXE), EXE
assert SRC and os.path.isfile(SRC), ENV.MISSING_SAMPLE


def api(path, obj=None, timeout=60, raw=False):
    try:
        if obj is None:
            req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}")
        else:
            req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}",
                                         data=json.dumps(obj).encode(),
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
            return data.decode("utf-8", "ignore") if raw else json.loads(data.decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        return {"_err": f"HTTP {e.code}"}
    except Exception as e:
        return {"_err": f"{type(e).__name__}: {e}"}


def first_frame(src, dst, t=None):
    cmd = [FF, "-hide_banner", "-loglevel", "error", "-y"]
    if t is not None:
        cmd += ["-ss", f"{t:.3f}"]
    cmd += ["-i", src, "-frames:v", "1", "-f", "image2", dst]
    r = subprocess.run(cmd, capture_output=True, timeout=180)
    return r.returncode == 0 and os.path.isfile(dst) and os.path.getsize(dst) > 0


def psnr(a, b):
    r = subprocess.run([FF, "-hide_banner", "-i", a, "-i", b, "-lavfi", "psnr", "-f", "null", "-"],
                       capture_output=True, timeout=120)
    m = re.search(r"average:([0-9.]+|inf)", r.stderr.decode("utf-8", "ignore"))
    if not m:
        return None
    return float("inf") if m.group(1) == "inf" else float(m.group(1))


proc = subprocess.Popen([EXE, "--web", "--root", ROOT, "--port", str(PORT)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    print("等待 exe 启动…")
    ok = False
    for _ in range(40):
        time.sleep(1)
        if api("/api/ping", timeout=5).get("ok"):
            ok = True
            break
    assert ok, "exe 没起来"
    print("✅ exe 启动")

    print("\n[1] 网页文案")
    html = api("/", raw=True)
    assert isinstance(html, str) and "TV投屏" in html, "首页异常"
    assert "seek_mode" in html, "网页没拿到 seek_mode（旧版 exe？）"
    assert "doSeek" in html and "重新起流" in html, "网页没有新的定位逻辑"
    assert "本次不能拖进度" not in html, "网页还是旧文案"
    assert "本次不能拖" not in html
    print("  ✓ 含 seek_mode / doSeek / 重新起流；旧的“本次不能拖”已消失")

    print("\n[2] /api/state 暴露 seek_mode")
    st = api("/api/state")
    assert "seek_mode" in st, f"缺 seek_mode: {sorted(st)[:12]}"
    assert st["seek_mode"] in ("native", "restream"), st["seek_mode"]
    print(f"  ✓ seek_mode={st['seek_mode']}  play_mode={st.get('play_mode')!r}")

    print("\n[3] 真实流定位：/live/*.ts?t=90")
    url = f"http://127.0.0.1:{PORT}/live/{urllib.parse.quote(REL)}.ts?t=90"
    dst = os.path.join(TMP, "live90.ts")
    got, t0 = 0, time.time()
    with urllib.request.urlopen(url, timeout=20) as resp:
        print(f"  HTTP {resp.status}  {resp.headers.get('Content-Type')}")
        with open(dst, "wb") as f:
            while got < (6 << 20) and time.time() - t0 < 8:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
    print(f"  收到 {got / 1048576:.1f} MB")
    assert got > (1 << 20), "定位流没数据"

    f90 = os.path.join(TMP, "f90.png")
    assert first_frame(dst, f90), "定位流解不出画面"
    ref90 = os.path.join(TMP, "ref90.png")
    ref0 = os.path.join(TMP, "ref0.png")
    first_frame(SRC, ref90, t=90)
    first_frame(SRC, ref0, t=0)
    p90, p0 = psnr(f90, ref90), psnr(f90, ref0)
    print(f"  PSNR(?, 源片90s)={p90}   PSNR(?, 源片0s)={p0}")
    assert p90 and p0 and p90 > p0, f"定位没生效: {p90} vs {p0}"
    print("  ✅ ?t=90 出来的确实是 90s 的画面")

    print("\n[4] HEAD /live/ 回归")
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/live/{urllib.parse.quote(REL)}.ts", method="HEAD")
    with urllib.request.urlopen(req, timeout=20) as resp:
        cl = resp.headers.get("Content-Length")
        print(f"  {resp.status}  Content-Type={resp.headers.get('Content-Type')}  Content-Length={cl}")
        assert resp.headers.get("Content-Type") == "video/mp2t"
        assert cl != "0", "HEAD 回了 0"

    print("\n✅ 打包后的 exe：转码播放可拖进度条（重推定位）已验证")
finally:
    # ⚠️ PyInstaller onefile 的 exe 是"父进程拉子进程"的结构，terminate() 只杀得掉
    # 父进程，子进程会变成孤儿继续占着端口和 ffmpeg。必须按进程树杀。
    try:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       capture_output=True, timeout=20)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.wait(timeout=10)
    except Exception:
        pass
    print("已关闭测试实例（PID %s）  临时目录 %s" % (proc.pid, TMP))
