"""验证：中途中断的转码流不会留下缓存 / 临时文件。"""
import os
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
SRC = ENV.pick_video()
assert SRC, ENV.MISSING_SAMPLE

logs = []
core = CastCore(ROOT, "127.0.0.1", log=logs.append)
srv = TvCastServer(core, ROOT, port=0, host="127.0.0.1")
core.server = srv
srv.start()

rel = os.path.relpath(SRC, core.root_dir).replace("\\", "/")
cache = media.cache_path_for(ROOT, SRC)
partial = media.partial_path_for(ROOT, SRC)

for p in (cache, partial):
    if os.path.isfile(p):
        os.remove(p)
print("清场: 缓存存在=", os.path.isfile(cache), " 临时存在=", os.path.isfile(partial))

print("\n-- 只读 3MB 就断开（模拟用户中途停止） --")
r = urllib.request.urlopen(f"http://127.0.0.1:{srv.port}/stream/{urllib.parse.quote(rel)}", timeout=120)
got = r.read(3 << 20)
r.close()
print(f"  读入 {len(got)/1048576:.1f}MB 后断开")

# 等服务端收尾：处理线程走完 finally（释放占用标记 + 清掉临时文件）
print("  等待服务端收尾…")
t0 = time.time()
released = False
while time.time() - t0 < 90:
    if media.claim_partial(partial):
        media.release_partial(partial)
        released = True
        break
    time.sleep(0.3)
while time.time() - t0 < 90 and os.path.isfile(partial):
    time.sleep(0.3)
print(f"  收尾完成={released}  用时 {time.time()-t0:.1f}s")

print("\n-- 校验 --")
print("  日志:", " | ".join(x for x in logs if "缓存" in x or "未跑完" in x))
print("  临时文件已清理:", not os.path.isfile(partial))
print("  未生成缓存:", not os.path.isfile(cache))
assert released, "处理线程没有退出，占用标记没释放"
assert not os.path.isfile(partial), "临时文件残留"
assert not os.path.isfile(cache), "中途停止竟然生成了缓存"
srv.stop()
print("\n=== 中断路径安全 ===")
