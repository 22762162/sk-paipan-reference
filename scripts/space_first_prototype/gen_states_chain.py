#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v7 九态:720°全景 + 3D 空间定位 + 串行链式。

三层空间保证(对应用户三次纠偏):
1. 全景母版 = 房间几何唯一真相(720全景,用户要求的机制);
2. 每态用 v360 从全景按本镜机位切出「背景基准图」——背景不再靠模型想象,
   而是确定性数学投影(零成本);
3. 提示词写【3D空间定位】:以书案为原点的地标化坐标,人物/机位/窗/书架
   在哪一目了然(融入3D空间,人物位置立体清晰)。
关键帧仍串行生成:态N 锚态N-1(v6 教训:并行=各画各的房间)。
"""
import math
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, "/Users/sk/AIFOS")
from aifos.adapters import codex_image  # noqa: E402

BASE = "/Users/sk/AIFOS/workspace/artifacts/p014"
OUT = f"{BASE}/e001/states_v2"
SLICES = f"{BASE}/scenes/slices"
PANO = f"{BASE}/scenes/书阁内_暖金古室__view_panorama_v1.png"
FFMPEG = os.path.expanduser("~/.local/bin/ffmpeg")
CODEX = "/Users/sk/.local/node22/lib/node_modules/@openai/codex/bin/codex.js"
PORTRAIT = f"{BASE}/e001/cast/candidates/portrait_沈眉_candidate_01.png"
COSTUME = f"{BASE}/e001/cast/sheet_沈眉_costume.png"
PROP = f"{BASE}/e001/cast/props/candidates/prop_顾长渊旧银铃_candidate_01.png"

# ---- 3D 空间模型(全景坐标系:yaw0=北=窗向;书案为原点,单位米) ----
# 书案居中;纱幔格棂窗在案北约2.2m;沈眉站案东端外侧一步(x+0.9,z-0.5),
# 面朝西北;幕后人影在窗后 x-0.5;摄影机全片同机位组:案南约2.6m,镜高1.55m,
# 朝北看向沈眉——画面:人物右1/3,窗在中左,两侧画缘灯笼书架。
SPATIAL_BASE = (
    "【3D空间定位】以书案为原点:书案居中,案后约两步(北)是挂象牙纱幔的"
    "格棂窗;沈眉站在书案右(东)端外侧一步,面朝书案;摄影机在案南约2.6米、"
    "镜高1.55米,朝北看向她。因此画面中:沈眉在画面右侧约1/3处,书案横过"
    "画面下部,纱幔格棂窗在画面中左,左右画缘各有立式灯笼与书架立柱。"
    "背景必须与「背景基准图」逐处对应——那是同一房间在本机位方向的"
    "真实投影,不是风格参考。")

# 每态机位:yaw(度,0=北) / pitch / 横向视场(度) / 空间定位补充
CAMS = {
    0: (4, 2, 46, "中景:上述基准构图完整成立,窗与幕后人影清晰在画面中左。"),
    1: (6, 3, 38, "中近景:略推近,沈眉上半身为主,窗纱在其左后仍可见。"),
    2: (8, 1, 30, "近景:取景收到手部与心口,背景是她身后的书架与纱幔边缘。"),
    3: (2, -2, 48, "中景:含整张案面,镜头略俯,铃落案上;窗在画面中左。"),
    4: (4, -6, 32, "近景:俯向案面,案上卷册与铃为主体,人物俯身入画右。"),
    5: (6, 2, 32, "近景:胸前手部为主体,背景为她身后书架与灯笼暖光。"),
    6: (6, 2, 30, "近景:握拳的手与低头面部,背景同上一态,不变。"),
    7: (0, 2, 52, "中景:后拉,纱幔格棂窗与幕后人影完整进入画面中左,"
                  "沈眉在画面右。"),
    8: (6, 4, 22, "特写:面部为主,背景为虚化的纱幔窗与书架,不得换背景。"),
}

INVENTORY = (
    "【本场固定陈设清单·以全景母版为唯一事实源】居中深色木书案,案上数册"
    "摊开的黄纸卷册与一只三足青铜香炉(一缕细烟);案后挂象牙纱幔的格棂窗,"
    "窗透暖金光;两侧通高深色书架、粗木立柱、各一盏立式灯笼,书架旁小几与"
    "绿植;深色木地板木梁顶。严禁新增、移除、移动或替换任何陈设。")
WHO = (
    "沈眉:22岁女性。发型:简洁垂髻,只用一支素银簪——素直杆、尾端一粒小巧"
    "缠枝雕花簪头,严禁换成串珠纺锤等其他形制;碎发在额侧耳际。"
    "服装:象牙白交领右衽细棉衫,外罩极浅沉香色半臂(短袖及肘,有袖,不是"
    "无袖坎肩、不是长袖罩袍),全片不换装不改袖型领型。")
BELL = (
    "旧银铃:极小银质扁圆铃,直径不超过食指第一指节,两指可捏,掌心可藏;"
    "旧银包浆、浅阴刻纹、褪色红绳。道具参考图是放大数十倍棚拍特写,"
    "只认形制材质,严禁继承其画面占比。")
LIGHT = ("光线:暖金光自案后纱幔窗透入呈逆光,两侧灯笼暖光补充;"
         "全片同一色温同一光向,严禁变亮变冷变暗或换主光方向。")

STATES = {
    0: ("沈眉立于书案右侧,双手轻按案上摊开的旧卷低头整理;"
        "纱幔上映出静止的玄色人影剪影(幕后,面目不可见)。"),
    1: ("沈眉左手指尖隔着衣襟按在心口偏左,压卷动作停住,右手仍搭旧卷上,"
        "微低头看向自己指尖。银铃未露。纱幔人影同位静止。"),
    2: ("沈眉放开卷册,右手拇指食指捏住衣襟内侧探出的小别针端向外轻拉,"
        "线头松开;银铃仍藏衣襟内。低头专注看手。"),
    3: ("旧银铃连红绳滑出衣襟,落在案面上静止;沈眉双手悬空未触碰,低头看向"
        "案面。铃在案面上只是很小的银点,远小于旁边卷册。"),
    4: ("沈眉微俯身,视线落在案上小银铃,右手悬停铃上方约一掌高,未触碰。"),
    5: ("沈眉拇指食指指尖捏起银铃提离案面,举到胸前偏下;铃只在指尖露一点,"
        "红绳垂落。微低头,目光牢牢落在指尖的铃上,不看镜头不看两侧。"),
    6: ("沈眉五指收拢把银铃完全合入右掌心握紧,掌外只露一小截红绳;手停胸前"
        "偏下。仍低头,目光落在握紧的拳上,头不转向左右任何一侧。"),
    7: ("沈眉右手握拳藏铃停在胸前,头部明确转向案后纱幔,四分之三侧脸;"
        "【视线硬约束】双眼虹膜朝向纱幔上的人影(画面中左),严禁眼珠偏向"
        "画面右侧或看镜头;神情惊疑。纱幔人影与态0同位,只允许一次极轻微"
        "姿态偏移。"),
    8: ("沈眉面部特写,握拳的手在胸前画面下缘;视线已从纱幔收回,望向正前方"
        "偏下,眼神由惊疑转为怔忡。"),
}


def make_slice(i):
    """从全景按本态机位切背景基准图(确定性投影,零成本)。"""
    yaw, pitch, hfov, _ = CAMS[i]
    vfov = math.degrees(2 * math.atan(math.tan(math.radians(hfov) / 2) * 16 / 9))
    dest = f"{SLICES}/state_{i}_bg.png"
    subprocess.run(
        [FFMPEG, "-y", "-i", PANO, "-vf",
         f"v360=input=e:output=flat:yaw={yaw}:pitch={pitch}"
         f":h_fov={hfov:.1f}:v_fov={vfov:.1f},scale=810:1440",
         dest],
        check=True, capture_output=True, timeout=120)
    return dest


def manifest(i, prev_uri):
    refs = []
    if prev_uri:
        refs.append({
            "label": "上一状态(主导连续性锚)", "uri": prev_uri,
            "role": "continuity",
            "binding": "同场同机位组相邻帧:陈设、纱幔、书架、案上卷册香炉、"
                       "光线色温、服装袖型领型、发簪形制逐项一致,"
                       "只改本帧动作与视线。"})
    refs.append({
        "label": "背景基准图(全景在本机位方向的切片)",
        "uri": make_slice(i), "role": "scene",
        "binding": "本帧背景的几何真相:窗/纱幔/书架/灯笼/立柱/案面的位置"
                   "透视以此为准,背景逐处对应;人物以人物参考图为准。"
                   "严禁出现此图没有的陈设。"})
    refs.append({
        "label": "沈眉最终立绘", "uri": PORTRAIT, "role": "identity",
        "binding": "只锁脸型五官骨相、发际线、发型轮廓;服装姿势背景光线"
                   "服从连续性锚与背景基准。"})
    refs.append({
        "label": "服装设定图", "uri": COSTUME, "role": "wardrobe",
        "binding": "只锁服装形制:交领右衽细棉衫+浅沉香色半臂(短袖及肘);"
                   "不得改袖型领型配色。"})
    if i in (3, 4, 5, 6):
        refs.append({
            "label": "旧银铃母资产", "uri": PROP, "role": "prop",
            "binding": "只认形制材质包浆红绳;严禁继承其画面占比——"
                       "本帧里它必须是两指可捏的极小物件。"})
    for index, ref in enumerate(refs, 1):
        ref["index"] = index
    return refs


def prompt(i):
    n = "2人(沈眉+纱幔后玄色剪影)" if i in (0, 7) else "1人(沈眉)"
    return (f"9:16竖幅静态关键帧,超写实真人电影质感古风短剧。\n"
            f"{INVENTORY}\n{SPATIAL_BASE}\n【本镜取景】{CAMS[i][3]}\n"
            f"【人物】{WHO}\n【核心道具】{BELL}\n【光线】{LIGHT}\n"
            f"【本帧唯一状态】{STATES[i]}\n"
            f"【硬性要求】只定格这一个瞬间;画面可见真人共{n};"
            f"与背景基准图同一房间同一机位方向,与上一状态同机位组,"
            f"只改本帧动作与视线;画面干净无噪点;无字幕文字Logo水印。")


def gen(i, prev_uri, profile):
    os.environ["CODEX_HOME"] = profile
    refs = manifest(i, prev_uri)
    req = {
        "capability": "image",
        "out_dir": OUT,
        "payload": {
            "shot_no": i,
            "prompt": prompt(i),
            "aspect": "9:16",
            "image_quality": "high",
            "reference_manifest": refs,
            "reference_images": [m["uri"] for m in refs],
            **({"chain_first_uri": prev_uri} if prev_uri else {}),
            "identity_references": [
                {"uri": PORTRAIT, "name": "沈眉", "role": "identity"}],
        },
    }
    t0 = time.time()
    try:
        res = codex_image.run(req, CODEX, 1500, [], plain=True)
    except Exception as exc:                                # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}", time.time() - t0, None
    return (bool(res and res.get("ok")), str(res)[:200],
            time.time() - t0, (res or {}).get("uri"))


def main():
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(SLICES, exist_ok=True)
    profiles = [os.path.expanduser("~/.codex-account-b"),
                os.path.expanduser("~/.codex-account-c")]
    start = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    prev = f"{OUT}/state_{start - 1}.png" if start > 0 else None
    for i in range(start, 9):
        ok, msg, dt, uri = gen(i, prev, profiles[i % 2])
        produced = uri or f"{OUT}/shot_{i:03d}.keyframe.png"
        dest = f"{OUT}/state_{i}.png"
        if ok and produced and os.path.exists(produced):
            if os.path.abspath(produced) != os.path.abspath(dest):
                shutil.move(produced, dest)
            print(f"态{i} ✓ {os.path.getsize(dest)//1024}KB {dt:.0f}s",
                  flush=True)
            prev = dest
        else:
            print(f"态{i} ✗ {dt:.0f}s :: {msg}", flush=True)
            print(f"串行链断裂于态{i};修复后 `states5.py {i}` 续跑", flush=True)
            return
    print("STATES_V7_DONE", flush=True)


if __name__ == "__main__":
    main()
