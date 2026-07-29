#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v7 视频:states_v2 九态 → 8 段 frames2video。

预算规则:迭代验证一律 seedance2.0mini 720p(45积分/段)。
闸门 I 过了才许运行本脚本。校验数真实帧数(下载截断教训)。
"""
import glob
import math
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

SRC = "/Users/sk/AIFOS/workspace/artifacts/p014/e001/states_v2"
OUT = "/Users/sk/AIFOS/workspace/artifacts/p014/e001/videos_v7"
FFMPEG = os.path.expanduser("~/.local/bin/ffmpeg")
MODEL = "seedance2.0mini"
RES = "720p"
DUR = 5
EXPECT_W, EXPECT_H = 720, 1280
MIN_FRAMES = 115

COMMON = (
    "严格只执行下面描述的这一个动作，不得增加任何剧情、人物或道具。"
    "画面内只有沈眉一人（除非本镜明确提到纱幔后的人影剪影）。"
    "身份、脸型、垂髻与素银簪、象牙白交领衫与浅沉香色半臂全程不变。"
    "场景为同一书阁：居中书案(摊开卷册+三足铜香炉)、案后挂纱幔的格棂窗、"
    "两侧书架立柱与立式灯笼——陈设全程不变，严禁新增或替换。"
    "光线保持暖金逆光自案后窗透入，不得变亮变冷或改光向。"
    "旧银铃是两指可捏的极小铃铛，全程保持极小尺寸，严禁放大。"
    "画面干净无噪点无闪烁；五官与手指结构稳定。"
    "画面内不得出现任何文字、字幕、箭头、标注或水印。"
)

SHOTS = {
    1: ("沈眉低头整理案上摊开的旧卷，随后左手抬起、指尖隔着衣襟按到心口"
        "偏左停住，右手仍留在卷上；她微低头看向自己指尖。"
        "案后纱幔洁净透光，窗后无人——严禁出现任何人影。"
        "镜头固定，只有呼吸感与香炉青烟缓升。"),
    2: ("按心口的左手让开，右手抬起，拇指食指捏住衣襟内侧探出的小别针端"
        "向外轻拉，线头松开；低头专注看手。动作缓慢迟疑。镜头固定。"),
    3: ("一枚极小的银铃连褪色红绳从衣襟滑出，落在案面上静止；她双手悬空"
        "收在胸前上方，低头看向案面上的小银铃。镜头缓慢拉开到含整张案面。"),
    4: ("沈眉上身微俯，右手缓缓伸向案上那枚极小的银铃，在离铃约一掌高处"
        "悬停，未触碰；视线始终在铃上。镜头非常缓慢地轻推。"),
    5: ("指尖落下捏起银铃提离案面，举到胸前偏下，红绳垂落轻摆；"
        "她始终微低头，目光跟着铃从案面移到指尖。镜头固定。"),
    6: ("捏铃的手指五指收拢握成拳，把银铃完全合入掌心，拳侧只露一小截"
        "红绳；手停在胸前偏下，她仍低头看着自己的拳。镜头固定。"),
    7: ("她保持握拳在胸前，从低头看手缓缓抬头，头部转向案后纱幔方向成"
        "四分之三侧脸，目光落在纱幔上的玄色人影(画面中左)；人影只做一次"
        "极轻微的姿态偏移，位置大小不变。镜头非常缓慢地后拉。"),
    8: ("镜头缓慢推近到她的面部；她把视线从纱幔收回到正前方偏下，"
        "握拳的手贴在胸前不动，眼神由惊疑化为怔忡，定格。"),
}


def sh(cmd, timeout=1800):
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout, stdin=subprocess.DEVNULL)


def submit(n):
    cmd = ["dreamina", "frames2video",
           f"--first={SRC}/state_{n - 1}.png", f"--last={SRC}/state_{n}.png",
           f"--prompt={SHOTS[n] + COMMON}",
           f"--model_version={MODEL}", f"--video_resolution={RES}",
           f"--duration={DUR}", "--poll=0"]
    r = sh(cmd, timeout=900)
    blob = r.stdout + r.stderr
    ids = re.findall(r'"submit_id"\s*:\s*"([^"]+)"', blob)
    if not ids:
        return n, None, blob[:600]
    return n, ids[0], ""


def probe(path):
    r = sh([FFMPEG, "-i", path, "-map", "0:v:0", "-f", "null", "-"],
           timeout=600)
    blob = r.stdout + r.stderr
    hits = re.findall(r"frame=\s*(\d+)", blob)
    frames = int(hits[-1]) if hits else 0
    m = re.search(r"Video:.*?,\s*(\d+)x(\d+)", blob)
    w, h = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    return frames, w, h


def collect(n, sid, deadline=2700):
    t0 = time.time()
    while time.time() - t0 < deadline:
        r = sh(["dreamina", "query_result", f"--submit_id={sid}"], timeout=180)
        blob = r.stdout + r.stderr
        status = (re.findall(r'"gen_status"\s*:\s*"([^"]+)"', blob) or [""])[0]
        if status == "success":
            break
        if status in ("failed", "fail"):
            reason = (re.findall(r'"fail_reason"\s*:\s*"([^"]*)"', blob)
                      or [""])[0]
            return n, False, f"生成失败: {reason[:150]}"
        time.sleep(20)
    else:
        return n, False, f"超时({deadline}s)"
    dl = f"{OUT}/_dl_{n}"
    os.makedirs(dl, exist_ok=True)
    sh(["dreamina", "query_result", f"--submit_id={sid}",
        f"--download_dir={dl}"], timeout=1800)
    got = sorted(glob.glob(f"{dl}/**/*.mp4", recursive=True),
                 key=os.path.getsize, reverse=True)
    if not got:
        shutil.rmtree(dl, ignore_errors=True)
        return n, False, "未下到文件"
    dest = f"{OUT}/shot_{n:03d}.mp4"
    shutil.move(got[0], dest)
    shutil.rmtree(dl, ignore_errors=True)
    frames, w, h = probe(dest)
    if frames < MIN_FRAMES:
        os.remove(dest)
        return n, False, f"下载截断:{frames}帧(应≥{MIN_FRAMES})"
    if (w, h) != (EXPECT_W, EXPECT_H):
        os.remove(dest)
        return n, False, f"尺寸异常 {w}x{h}"
    return n, True, f"{frames}帧 {w}x{h} {os.path.getsize(dest)//1024}KB"


def run(shots, attempt=1):
    os.makedirs(OUT, exist_ok=True)
    subs = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        for n, sid, err in ex.map(submit, shots):
            if sid:
                subs[n] = sid
                print(f"镜{n} 提交 {sid[:8]}", flush=True)
            else:
                print(f"镜{n} 提交失败 :: {err[:200]}", flush=True)
    ok, bad = [], [n for n in shots if n not in subs]
    with ThreadPoolExecutor(max_workers=4) as ex:
        for n, good, msg in ex.map(lambda kv: collect(*kv),
                                   sorted(subs.items())):
            print(f"镜{n} {'✓' if good else '✗'} {msg}", flush=True)
            (ok if good else bad).append(n)
    if bad and attempt < 3:
        print(f"第{attempt}轮未成: {sorted(bad)}，补交", flush=True)
        ok += run(sorted(bad), attempt + 1)
    return ok


def main():
    shots = ([int(x) for x in sys.argv[1:]] if len(sys.argv) > 1
             else sorted(SHOTS))
    done = run(shots)
    have = sorted(glob.glob(f"{OUT}/shot_*.mp4"))
    print(f"V7_DONE 成功 {sorted(set(done))} / 目录内 {len(have)} 段", flush=True)


if __name__ == "__main__":
    main()
