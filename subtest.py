# -*- coding: utf-8 -*-
"""字幕链路端到端自测：合成素材 → 真实转码命令 → 像素级验证字幕是否真的画进画面。

不依赖用户的片库，全部用 lavfi 合成 + 自造 MKV 内嵌字幕轨。
"""
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tvcast import media  # noqa: E402

OK, FAIL = [], []


def check(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print(("  [OK ] " if cond else "  [!!] ") + name + (f"   {extra}" if extra else ""))


def bright_pixels(args, cwd=None):
    r = subprocess.run(args, capture_output=True, cwd=cwd, timeout=300,
                       **media._popen_kwargs())
    if r.returncode != 0:
        return r.returncode, -1, r.stderr.decode("utf-8", "ignore").strip()[-200:]
    return 0, sum(1 for b in r.stdout if b > 10), ""


def burn_frame_count(video, vf=None, at="0.5"):
    """把指定时间的帧解出来数亮像素。"""
    args = [media.FFMPEG, "-hide_banner", "-v", "error"]
    if vf:
        args += ["-vf", vf]
    args += ["-ss", at, "-i", video, "-frames:v", "1",
             "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    return bright_pixels(args)


def main():
    tmp = os.path.join(tempfile.gettempdir(), "tvsub_burn")
    if os.path.isdir(tmp):
        shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    print("工作目录:", tmp)

    # ---------------------------------------------------------------- 素材
    print("\n[1] 合成测试素材")
    src = os.path.join(tmp, "测试影片.mp4")
    r = subprocess.run(
        [media.FFMPEG, "-hide_banner", "-v", "error", "-y",
         "-f", "lavfi", "-i", "color=c=black:s=640x360:d=6:r=25",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", src],
        capture_output=True, **media._popen_kwargs())
    check("合成 6 秒黑底视频", r.returncode == 0 and os.path.isfile(src),
          r.stderr.decode("utf-8", "ignore")[-160:])

    rc, base_bright, _ = burn_frame_count(src, at="0.5")
    check("基准：无字幕时画面全黑", rc == 0 and base_bright == 0, f"亮像素={base_bright}")

    # GBK 外挂字幕（中文常见编码）
    gbk_srt = os.path.join(tmp, "测试影片.chs.srt")
    with open(gbk_srt, "wb") as f:
        f.write("1\n00:00:00,000 --> 00:00:06,000\n中文字幕烧录测试\n".encode("gb18030"))
    check("写入 GBK 外挂字幕", os.path.getsize(gbk_srt) > 0)

    # ---------------------------------------------------------------- 探测
    print("\n[2] 字幕探测与编码归一")
    subs = media.external_subtitles(src)
    check("识别到外挂字幕", len(subs) == 1 and subs[0]["label"] == "chs",
          str([s["label"] for s in subs]))
    stems = media.subtitle_stems(tmp)
    check("subtitle_stems 命中影片主名",
          os.path.splitext(os.path.basename(src))[0].lower() in stems)
    check("subtitle_stems 不含被正则区间吞出的怪值",
          not any(s.endswith("..") for s in stems), str(sorted(stems)))
    u8 = media.resolve_subtitle_file(src, subs[0])
    txt = open(u8, encoding="utf-8").read()
    check("GBK 自动转 UTF-8", "中文字幕烧录测试" in txt)

    # ---------------------------------------------------------------- 滤镜
    print("\n[3] 滤镜参数转义")
    f = media.subtitle_filter(u8)
    check("盘符冒号已转义且被单引号包裹", "'C\\:" in f and f.endswith("'"), f[:60])

    # ---------------------------------------------------------------- 真实烧录
    print("\n[4] 走真实转码命令烧录（build_live_cmd → 管道 → 像素验证）")
    info = media.probe(src)
    cmd = media.build_live_cmd(src, info, 0.0, sub_filter=f)
    check("命令里出现 -vf subtitles", "-vf" in cmd and any("subtitles=" in c for c in cmd))
    check("命令里强制重编码（不能用 -c:v copy）",
          not (cmd[cmd.index("-vf") - 2: cmd.index("-vf")] == ["-c:v", "copy"]))

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            bufsize=0, **media._popen_kwargs())
    data, err = proc.communicate(timeout=300)
    check("实时转码返回码为 0", proc.returncode == 0, err.decode("utf-8", "ignore")[-200:])
    check("产出 MP4 头正常", b"ftyp" in data[:64], f"{len(data)} bytes")

    # 把流落盘后抽帧验证
    out = os.path.join(tmp, "burned.mp4")
    with open(out, "wb") as fh:
        fh.write(data)
    rc, bright, e = burn_frame_count(out, at="3.0")
    check("烧录后画面出现字幕像素", rc == 0 and bright > 0, f"亮像素={bright} {e}")
    rc0, b0, _ = burn_frame_count(src, at="3.0")
    check("原片同一时刻仍是全黑（对比成立）", b0 == 0, f"亮像素={b0}")

    # ---------------------------------------------------------------- 内嵌字幕轨
    print("\n[5] 内嵌字幕轨抽取与烧录")
    emb = os.path.join(tmp, "内嵌字幕影片.mkv")
    r = subprocess.run(
        [media.FFMPEG, "-hide_banner", "-v", "error", "-y",
         "-f", "lavfi", "-i", "color=c=black:s=640x360:d=6:r=25",
         "-i", u8, "-map", "0:v", "-map", "1:0",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:s", "srt",
         "-metadata:s:s:0", "language=chi", emb],
        capture_output=True, **media._popen_kwargs())
    check("合成带内嵌字幕轨的 MKV", r.returncode == 0 and os.path.isfile(emb),
          r.stderr.decode("utf-8", "ignore")[-200:])

    einfo = media.probe(emb)
    check("ffprobe 识别到内嵌文本字幕轨",
          any(t.get("text") for t in einfo.subs_embedded),
          str(einfo.subs_embedded))

    tracks = media.all_subtitles(emb, einfo)
    check("all_subtitles 能列出内嵌轨", any(t.get("kind") == "embedded" for t in tracks))
    etrack = next(t for t in tracks if t.get("kind") == "embedded")
    efile = media.resolve_subtitle_file(emb, etrack)
    check("内嵌字幕成功抽成独立 SRT",
          bool(efile) and os.path.isfile(efile) and "中文字幕烧录测试" in open(efile, encoding="utf-8").read())

    ecmd = media.build_live_cmd(emb, einfo, 0.0, sub_filter=media.subtitle_filter(efile))
    ep = subprocess.Popen(ecmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          bufsize=0, **media._popen_kwargs())
    edata, eerr = ep.communicate(timeout=300)
    eout = os.path.join(tmp, "embedded_burned.mp4")
    with open(eout, "wb") as fh:
        fh.write(edata)
    rc, bright, e = burn_frame_count(eout, at="3.0")
    check("内嵌字幕烧录后出现字幕像素", rc == 0 and bright > 0, f"亮像素={bright} {e}")

    # ---------------------------------------------------------------- 模式语义
    print("\n[6] plan_subtitles 三种模式的语义")
    # 造一个"可直连"的文件（h264+aac+mp4）
    direct = os.path.join(tmp, "可直连.mp4")
    subprocess.run(
        [media.FFMPEG, "-hide_banner", "-v", "error", "-y",
         "-f", "lavfi", "-i", "color=c=black:s=320x240:d=2:r=25",
         "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "2",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-movflags", "+faststart", direct],
        capture_output=True, **media._popen_kwargs())
    shutil.copy(gbk_srt.replace(".chs.srt", ".chs.srt"),
                os.path.join(tmp, "可直连.chs.srt"))
    dinfo = media.probe(direct)
    check("可直连样本判定为 direct", dinfo.direct, dinfo.summary)

    flt, note = media.plan_subtitles(direct, dinfo, "off")
    check("off 模式不烧字幕", flt is None and note == "", note)
    flt, note = media.plan_subtitles(direct, dinfo, "auto")
    check("auto 模式对可直连文件跳过烧录（保画质）", flt is None and "重编码" in note, note)
    flt, note = media.plan_subtitles(direct, dinfo, "burn")
    check("burn 模式对可直连文件也烧", flt is not None and "已烧录" in note, note)
    flt, note = media.plan_subtitles(direct, dinfo, "auto", force_burn=True)
    check("force_burn 绕过 auto 的跳过（供显式转码用）", flt is not None, note)
    flt, note = media.plan_subtitles(src, dinfo, "off")
    check("没有字幕的文件任何模式都不烧", media.plan_subtitles(direct, dinfo, "off")[0] is None)

    # ---------------------------------------------------------------- 缓存变体
    print("\n[7] 缓存路径变体隔离")
    p0 = media.cache_path_for(tmp, src, "")
    p1 = media.cache_path_for(tmp, src, "sub")
    check("烧字幕与不烧字幕的缓存路径不同", p0 != p1, f"{os.path.basename(p0)} vs {os.path.basename(p1)}")
    check("变体路径仍以 .mp4 结尾", p1.endswith(".mp4"))
    check("未使用变体时路径不含 @/.sub 标记", "sub" not in os.path.basename(p0))
    check("find_cached 对不存在的变体返回 None", media.find_cached(tmp, src, "sub") is None)

    print("\n" + "=" * 60)
    print(f"通过 {len(OK)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for n in FAIL:
            print("  失败:", n)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
