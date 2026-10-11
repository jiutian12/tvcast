# -*- coding: utf-8 -*-
"""实测 Windows 上 ffmpeg subtitles 滤镜的路径转义方案。

判定方法：渲染 1 帧灰度原始像素，统计非黑像素数量。
黑底 + 白字幕 → 有字幕则亮像素明显 > 0。
"""
import os
import shutil
import subprocess
import sys
import tempfile

FFMPEG = "ffmpeg"

SRT_BODY = """1
00:00:00,000 --> 00:00:10,000
字幕测试 ABC 12345
"""


def make_srt(path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(SRT_BODY)


def bright_pixels(args, cwd=None):
    """跑 ffmpeg，返回 (returncode, 亮像素数, stderr 摘要)"""
    r = subprocess.run(args, capture_output=True, cwd=cwd, timeout=120)
    if r.returncode != 0:
        return r.returncode, -1, r.stderr.decode("utf-8", "ignore").strip().splitlines()[-1:] or [""]
    data = r.stdout
    bright = sum(1 for b in data if b > 10)
    return 0, bright, []


def base_in():
    return ["-hide_banner", "-v", "error", "-f", "lavfi", "-i",
            "color=c=black:s=640x360:d=1:r=1"]


def run_case(name, vf, cwd=None, subdir=False):
    args = [FFMPEG] + base_in() + ["-vf", vf, "-frames:v", "1",
                                   "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    rc, bright, err = bright_pixels(args, cwd=cwd)
    mark = "OK " if (rc == 0 and bright > 0) else ("-- " if rc == 0 else "ERR")
    print(f"  [{mark}] {name:<52} rc={rc} 亮像素={bright}  {'' if not err else err[0][:90]}")
    return rc == 0 and bright > 0


def main():
    tmp = os.path.join(tempfile.gettempdir(), "tvsub_test")
    if os.path.isdir(tmp):
        shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)

    ascii_srt = os.path.join(tmp, "sample.srt")
    cn_srt = os.path.join(tmp, "中文 字幕（测试）.srt")
    make_srt(ascii_srt)
    make_srt(cn_srt)

    print("临时目录:", tmp)
    print()
    print("--- 1) 无字幕基准（应为 0 亮像素）---")
    run_case("benchmark: 不加 subtitles 滤镜", "null")

    print()
    print("--- 2) ASCII 路径的各种转义写法 ---")
    p = ascii_srt.replace("\\", "/")
    run_case("原文不转义:  subtitles=f=C:/temp/sample.srt",
             f"subtitles=f={p}")
    run_case("冒号转义:    subtitles=f=C\\:/temp/sample.srt",
             f"subtitles=f={p.replace(':', chr(92) + ':')}")
    run_case("冒号转义+引号: subtitles=filename='C\\:/temp/sample.srt'",
             f"subtitles=filename='{p.replace(':', chr(92) + ':')}'")
    run_case("双反斜杠+引号: subtitles='C:/temp/sample.srt'",
             f"subtitles='{p}'")

    print()
    print("--- 3) 中文+空格+全角括号路径 ---")
    pc = cn_srt.replace("\\", "/")
    run_case("中文路径+冒号转义+引号",
             f"subtitles=filename='{pc.replace(':', chr(92) + ':')}'")
    run_case("中文路径+不转义+引号", f"subtitles='{pc}'")

    print()
    print("--- 4) cwd 技巧：切到字幕目录、只用文件名（绕开冒号）---")
    run_case("cwd + ASCII 文件名:  subtitles=sample.srt",
             "subtitles=sample.srt", cwd=tmp)
    run_case("cwd + 中文名(引号):   subtitles='中文 字幕（测试）.srt'",
             "subtitles='中文 字幕（测试）.srt'", cwd=tmp)
    run_case("cwd + 中文名(无引号)", "subtitles=中文 字幕（测试）.srt", cwd=tmp)

    print()
    print("--- 5) 相对路径（相对输入文件？）用 -i 目录 ---")
    run_case("subtitles 用相对路径 filename=sample.srt (cwd=tmp)",
             "subtitles=filename=sample.srt", cwd=tmp)


if __name__ == "__main__":
    sys.exit(0 if main() is None else 1)
