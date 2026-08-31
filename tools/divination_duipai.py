"""六爻/梅花/大运/神煞/关系/五虎遁 双实现独立对拍(INV-08/09)。

主实现: ~/Projects/sk/consult-engine/{liuyao,meihua,luck}.py(Claude 作)
参考实现: 本仓 reference/*(Codex 盲写)。仅比共享规格定义的字段;
词表差异按映射表归一,归一失败记为"词表分歧"而非数值分歧。
一致仅表示当前观测范围内未发现差异,不构成正确性证明;分歧不由实现方单方裁决。
运行: python3 tools/divination_duipai.py
"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
MAIN = Path.home() / "Projects" / "sk" / "consult-engine"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(MAIN))

import liuyao as m_liuyao  # noqa: E402  主实现
import luck as m_luck  # noqa: E402
import meihua as m_meihua  # noqa: E402
from reference import dayun_ref, liuyao_ref, meihua_ref, relations_ref, shensha_ref, wuhudun_ref  # noqa: E402

STEMS = "甲乙丙丁戊己庚辛壬癸"
BRANCHES = "子丑寅卯辰巳午未申酉戌亥"
JIAZI = [STEMS[i % 10] + BRANCHES[i % 12] for i in range(60)]
DIFFS: list[str] = []
STATS: dict[str, int] = {}


def note(section: str, msg: str, cap: int = 12) -> None:
    STATS[section] = STATS.get(section, 0) + 1
    if STATS[section] <= cap:
        DIFFS.append(f"[{section}] {msg}")


def duipai_wuhudun() -> int:
    n = 0
    for ys in STEMS:
        a = m_luck.liuyue_ganzhi(ys)
        b = wuhudun_ref.wuhudun(ys)
        if list(a) != list(b):
            note("五虎遁", f"{ys}: 主={a} 参={b}")
        n += 1
    return n


def duipai_ten_god() -> int:
    n = 0
    for d in STEMS:
        for o in STEMS:
            a = m_luck.shishen(d, o)
            b = relations_ref.ten_god(d, o)
            if a != b:
                note("十神", f"{d}见{o}: 主={a} 参={b}")
            n += 1
    return n


# 地支关系词表归一:两侧措辞 → 规范类别
_REL_NORM = {
    "六合": "六合", "六冲": "六冲", "六害": "六害", "相刑": "刑", "三刑": "刑",
    "自刑": "自刑", "三合半合": "半合", "半合": "半合", "同气": "同气", "无明显作用": "无",
    "刑": "刑", "害": "六害", "冲": "六冲", "合": "六合",
}


def duipai_branch_rel() -> int:
    n = 0
    for x in BRANCHES:
        for y in BRANCHES:
            a = {_REL_NORM.get(r, f"?主:{r}") for r in m_luck.branch_rel(x, y)} - {"无"}
            b_raw = relations_ref.relation_names(x, y)
            b = {_REL_NORM.get(r, f"?参:{r}") for r in b_raw} - {"无"}
            if a != b:
                note("地支关系", f"{x}×{y}: 主={sorted(a)} 参={sorted(b)}")
            n += 1
    return n


def duipai_kongwang() -> int:
    n = 0
    for day in JIAZI:
        a = m_luck.kongwang(day)
        b = "".join(shensha_ref.void_branches(day))
        if set(a) != set(b):
            note("旬空", f"{day}: 主={a} 参={b}")
        n += 1
    return n


def duipai_liuyao(days: list[str]) -> int:
    n = 0
    for lines in itertools.product((6, 7, 8, 9), repeat=6):
        for day in days:
            a = m_liuyao.cast(list(lines), day, "寅")
            b = liuyao_ref.liuyao(list(lines), day)
            pairs = [
                ("本卦名", a["ben"]["name"], b["original"]["name"]),
                ("世位", a["ben"]["shi"], next(y["position"] for y in b["yao"] if y["is_shi"])),
                ("应位", a["ben"]["ying"], next(y["position"] for y in b["yao"] if y["is_ying"])),
            ]
            if a["bian"] is not None:  # 无动爻时主实现无变卦(规格如此),不比
                pairs.append(("变卦名", a["bian"]["name"], b["changed"]["name"]))
            for i in range(6):
                ya, yb = a["yao"][i], b["yao"][i]
                pairs += [
                    (f"爻{i+1}纳支", ya["branch"], yb["na_jia_branch"]),
                    (f"爻{i+1}六亲", ya["liuqin"], yb["six_relative"]),
                    (f"爻{i+1}六神", ya["liushen"].replace("螣", "腾"),
                     yb["six_spirit"].replace("螣", "腾")),  # 螣/腾为同神异体字,归一后比
                    (f"爻{i+1}空亡", ya["kong"], yb["void"]),
                    (f"爻{i+1}动", ya["moving"], yb["changing"]),
                ]
            for field, va, vb in pairs:
                if va != vb:
                    note("六爻", f"{''.join(map(str,lines))}@{day} {field}: 主={va} 参={vb}")
            n += 1
    return n


_MEI_REL_NORM = {"体用比和": "bihe", "比和": "bihe", "用生体": "yong_sheng_ti", "体生用": "ti_sheng_yong",
                 "用克体": "yong_ke_ti", "体克用": "ti_ke_yong",
                 "bihe": "bihe", "yong_sheng_ti": "yong_sheng_ti", "ti_sheng_yong": "ti_sheng_yong",
                 "yong_ke_ti": "yong_ke_ti", "ti_ke_yong": "ti_ke_yong"}


def duipai_meihua() -> int:
    n = 0
    for n1 in range(1, 49):
        for n2 in range(1, 49):
            for hb in BRANCHES:
                a = m_meihua.cast_numbers(n1, n2, hb)
                b = meihua_ref.meihua(n1, n2, hb)
                raw_rel = str(b.get("body_use_relation", "")).split("(")[0].strip()
                rel_b = _MEI_REL_NORM.get(raw_rel, f"?参:{raw_rel}")
                pairs = [
                    ("本卦", a["ben"], b["original"]["name"]),
                    ("互卦", a["hu"], b["mutual"]["name"]),
                    ("变卦", a["bian"], b["changed"]["name"]),
                    ("动爻", a["moving"], b["moving_line"]),
                    ("体", a["ti"]["trigram"], b["body"]["trigram"]),
                    ("用", a["yong"]["trigram"], b["use"]["trigram"]),
                    ("生克", a["relation_code"], rel_b),
                ]
                for field, va, vb in pairs:
                    if va != vb:
                        note("梅花", f"{n1},{n2},{hb} {field}: 主={va} 参={vb}")
                n += 1
    return n


def duipai_dayun() -> int:
    n = 0
    for ys in STEMS:
        for gender in ("male", "female"):
            a_dir = 1 if ((STEMS.index(ys) % 2 == 0) == (gender == "male")) else -1
            b_dir = dayun_ref.dayun_direction(ys, gender)
            if a_dir != b_dir:
                note("大运", f"方向 {ys}/{gender}: 主={a_dir} 参={b_dir}")
            n += 1
    for days in (0.0, 0.5, 1.0, 2.9, 3.0, 7.4, 22.0, 29.6):
        a = m_luck.dayun("壬午", "庚", "male", days, 30 - days, 1990, 3)
        b = dayun_ref.start_age(0, -int((30 - days) * 86400), int(days * 86400), 1)
        am = a["start_age"] * 12 + a["start_months"]
        bm = b["age"]["years"] * 12 + b["age"]["months"]
        if am != bm:
            note("大运", f"起运 {days}天: 主={am}月 参={bm}月")
        n += 1
    return n


def main() -> None:
    counts = {
        "五虎遁(10)": duipai_wuhudun(),
        "十神(100)": duipai_ten_god(),
        "地支关系(144)": duipai_branch_rel(),
        "旬空(60)": duipai_kongwang(),
        "六爻(4096×12天)": duipai_liuyao(JIAZI[::5]),
        "梅花(48×48×12)": duipai_meihua(),
        "大运(方向20+起运8)": duipai_dayun(),
    }
    total_diff = sum(STATS.values())
    print("=== 双实现对拍结果(共享规格字段) ===")
    for k, v in counts.items():
        sec = k.split("(")[0]
        print(f"  {k}: 比对 {v} 组,分歧 {STATS.get(sec, 0)}")
    print(f"总分歧 {total_diff};样例(每节≤12条):")
    for d in DIFFS:
        print("  " + d)
    print("说明:一致不构成正确性证明;全部分歧升级 INV-08 流程,不由实现方单方裁决。")


if __name__ == "__main__":
    main()
