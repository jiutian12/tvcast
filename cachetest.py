"""验证「边播边缓存」闭环：

完整消费一路实时转码流 → 等待自动封装 → 校验缓存产物
（H.264+AAC、moov 前置即 faststart、时长与源一致、可 Range 读取、临时文件已清理）
"""
import glob
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tests_env as ENV
from tvcast import media
from tvcast.core import CastCore
from tvcast.httpserver import TvCastServer
from tvcast.util import MEDIA_EXT

ROOT = ENV.ROOT


def pick_small_incompatible():
    p = ENV.pick_incompatible()
    if not p:
        raise SystemExit(ENV.MISSING_SAMPLE)
    return p, media.probe(p)


def moov_first(path):
    with open(path, "rb") as f:
        head = f.read(1 << 20)
    im, id_ = head.find(b"moov"), head.find(b"mdat")
    return im, id_, (im != -1 and (id_ == -1 or im < id_))


def main():
    src, info = pick_small_incompatible()
    print(f"样本: {os.path.basename(src)[:52]}")
    print(f"      源格式 {info.summary}  时长 {info.duration:.0f}s  "
          f"大小 {os.path.getsize(src)/1048576:.1f}MB  direct={info.direct}")

    core = CastCore(ROOT, "127.0.0.1", log=lambda m: print("  [log]", m))
    srv = TvCastServer(core, ROOT, port=0, host="127.0.0.1")
    core.server = srv
    srv.start()
    base = f"http://127.0.0.1:{srv.port}"
    print("服务端口:", srv.port)

    rel = os.path.relpath(src, core.root_dir).replace("\\", "/")
    q = urllib.parse.quote(rel)

    print("\n-- 1) 完整消费一路转码流 --")
    t0 = time.time()
    r = urllib.request.urlopen(f"{base}/stream/{q}", timeout=300)
    total = 0
    while True:
        c = r.read(1 << 20)
        if not c:
            break
        total += len(c)
    r.close()
    print(f"  流结束: {total/1048576:.1f}MB, 耗时 {time.time()-t0:.1f}s, "
          f"类型 {r.headers.get('Content-Type')}")

    print("\n-- 2) 等待自动封装为可拖 MP4 --")
    deadline = time.time() + 300
    last = None
    while time.time() < deadline:
        st = media.finalize_status()
        last = st.get("last")
        if last and last.get("state") != "running":
            break
        time.sleep(0.5)
    print("  封装结果:", json.dumps(last, ensure_ascii=False))

    print("\n-- 3) 校验缓存产物 --")
    cached = media.find_cached(ROOT, src)
    assert cached, "缓存文件不存在"
    print(f"  路径: {cached}")
    print(f"  大小: {os.path.getsize(cached)/1048576:.1f}MB")

    ci = media.probe(cached)
    print(f"  格式: {ci.container} | {ci.summary} | {ci.width}x{ci.height} | "
          f"时长 {ci.duration:.0f}s")
    assert ci.vcodec == "h264", f"视频编码不是 h264: {ci.vcodec}"
    assert ci.acodec == "aac", f"音频编码不是 aac: {ci.acodec}"
    assert ci.direct, f"缓存产物居然不能直连: {ci.reasons}"
    assert abs(ci.duration - info.duration) < max(3.0, info.duration * 0.02), \
        f"时长不符: {ci.duration} vs {info.duration}"

    im, id_, ok = moov_first(cached)
    print(f"  moov@{im} mdat@{id_}  faststart={'是' if ok else '否'}")
    assert ok, "moov 不在文件前部，无法秒开拖进度"

    partial = media.partial_path_for(ROOT, src)
    print(f"  临时文件已清理: {not os.path.isfile(partial)}")
    assert not os.path.isfile(partial)

    print("\n-- 4) 缓存按 Range 提供（拖进度） --")
    cr = os.path.relpath(cached, core.root_dir).replace("\\", "/")
    req = urllib.request.Request(f"{base}/media/{urllib.parse.quote(cr)}",
                                 headers={"Range": "bytes=1000000-1000099"})
    resp = urllib.request.urlopen(req, timeout=20)
    print(f"  {resp.status} {resp.headers.get('Content-Range')} "
          f"Accept-Ranges={resp.headers.get('Accept-Ranges')} "
          f"读到 {len(resp.read())} 字节")
    assert resp.status == 206

    print("\n-- 5) 二次投屏应命中缓存 --")
    url, mode = core._play_url(src)  # noqa: SLF001
    print(f"  选路: {mode}  {url}")
    assert mode == "cache", f"应该走缓存，实际 {mode}"

    srv.stop()
    print("\n=== 边播边缓存 全链路通过 ===")


if __name__ == "__main__":
    main()
