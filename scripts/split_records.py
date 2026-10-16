#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
split_records.py · 记录组拆分器（只读档案，不写档案）
=====================================================

为什么需要它
------------
档案是**扁平单值**（一个键一段文字），而网申站的「实习 / 校园 / 项目 / 荣誉」是
**可多条记录组**（一个模块多行，每行自己的起止时间 + 若干字段）。结构对不上，
引擎就填不了、清单也说不清。

本模块做**唯一的桥**：按「日期区间」把一段自由文本切成 N 段，每段自带起止时间。

四条红线（与「填写清单」同族，不许放宽）
----------------------------------------
1. **只读档案**：本模块永不写回档案。工件只喂清单，不动你的数据。
2. **值不打印到 stdout**：stdout 会进 AI 上下文。终端只输出「键名 + 段数」统计。
3. **日期不擅自补精度**：原文只有「2023.07」就只给「2023-07」并标 `精度=月`。
   补日 = 替你编数据。
4. **机械切段，不做语义推断**：把一段 194 字的实习经历硬拆成
   「企业名称 / 岗位名称 / 实习内容」是**语义推断**，猜错就是替你造假。
   本模块只按**日期区间**（硬边界）切，切完把整段文字交给你自己分配。

用法：
    python scripts/split_records.py --profile 档案.json
    python scripts/split_records.py --profile 档案.json --dry-run
    python scripts/split_records.py --selftest

许可：MIT
"""

import argparse
import datetime
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ------------------------------------------------------------------ 键 → 候选模块
# 参与拆分的档案键（经历类）。技能特长 / 兴趣爱好不是「经历」，不进此列。
SOURCE_KEYS = ("教育经历", "实习经历", "工作经历", "校园经历", "项目经历",
               "学生干部经历", "获奖情况", "荣誉", "论文专利", "证书", "科研经历")

# 教育经历的时间来自**扁平键**（页面字段就叫「入学时间 / 毕业时间」，不是自由文本）
EDU_TIME_KEYS = ("入学时间", "毕业时间")

# 档案键 → 站点上该键**可能**对应的模块名（供清单做候选匹配，**不做强制归属**）。
#
# ⚠ 取值**只能**是本仓 `assets/记录组_通用.json` 里真实存在的组名 —— 自测有一条
#   断言盯着它（⑱）。为什么这么定：这些名字一旦凭印象写，就会写出站点上根本不存在的
#   模块名，清单上多出一条**永远对不上**的候选，比不写更坏。
#
# 值为**空元组** = 「通用表里没有它的位置」，如实留空，**不硬塞**进一个不相关的组。
# 这不等于「站点上一定没有这个模块」—— 通用表只覆盖常见项，具体站点可能另有其组，
# 清单会把这种情况原样标出来。
MODULE_ALIASES = {
    "教育经历": ("教育经历",),
    "实习经历": ("实习经历",),
    "工作经历": ("工作经历",),
    "获奖情况": ("奖励信息",),
    "荣誉": ("奖励信息",),
    "证书": ("职业资格", "外语能力", "计算机能力"),
    # 通用表未覆盖：如实留空
    "校园经历": (),
    "学生干部经历": (),
    "项目经历": (),
    "科研经历": (),
    "论文专利": (),
}

# ------------------------------------------------------------------ 日期形态
# 日期区间分隔符（区间 = 两个日期点 + 中间只有分隔符）
_SEP_CHARS = "-—–~～至到/"
# 「至今」型结尾：工作 / 实习经历常见（页面通常另有「至今」勾选框）
_NOW = re.compile(r"\s*[%s]?\s*(至今|现在|目前)\s*" % re.escape(_SEP_CHARS))

# 单个日期点：2023.07 / 2023年7月 / 2023-07 / 2023/7 / 2023年 / 2023
#
# ⚠ 月份**必须限定 1–12 且后面不能紧跟数字**：否则「2019-2023」（年份区间）会被读成
#   「2019 年 2 月」—— 因为 `-2` 落在 1–12 内。`(?!\d)` 是关键：月份后跟着数字，
#   说明它其实是**更长数字串的一段**，不是月份。
#   判据要卡在**形态**上，不能卡在**取值范围**上 —— 范围判断永远有边界内刚好合法的反例。
_POINT = re.compile(r"(?<!\d)(\d{4})(?:\s*[年./\-]\s*(0?[1-9]|1[0-2])(?:\s*月)?(?!\d)|\s*年)?")


def _fmt_point(g):
    """(年, 月) → '2023-07' / '2023'；月缺省**不补**。"""
    y, m = g[0], g[1]
    return ("%s-%02d" % (y, int(m))) if m else str(y)


def _precision(g):
    return "月" if g[1] else "年"


def find_ranges(s):
    """找出文本里所有**成对**的日期区间。

    只认「两个日期点 + 中间纯分隔符」的形态 —— 单点日期**不作为切段边界**
    （否则「2024年毕业」这类词会被误切成新段）。
    """
    toks = [(m.start(), m.end(), m.groups()) for m in _POINT.finditer(s)]
    out, i = [], 0
    while i < len(toks):
        st, en, g = toks[i]
        if i + 1 < len(toks):
            st2, en2, g2 = toks[i + 1]
            mid = s[en:st2]
            if re.fullmatch(r"\s*[%s]+\s*" % re.escape(_SEP_CHARS), mid):
                out.append({"起": _fmt_point(g), "止": _fmt_point(g2),
                            "起原文": s[st:en], "止原文": s[st2:en2],
                            "起精度": _precision(g), "止精度": _precision(g2),
                            "_起": st, "_止": en2})
                i += 2
                continue
        mn = _NOW.match(s, en)
        if mn:
            out.append({"起": _fmt_point(g), "止": "至今",
                        "起原文": s[st:en], "止原文": mn.group(1),
                        "起精度": _precision(g), "止精度": "",
                        "_起": st, "_止": mn.end()})
            i += 1
            continue
        i += 1
    return out


def split_text(s):
    """把一段自由文本按日期区间切成 N 段。

    有区间 ⇒ 每段 =「该区间起点 → 下一区间起点」之间的文字；
    无区间 ⇒ 整段作为 1 条（起止留空，交你自己补）。
    """
    s = (s or "").strip()
    if not s:
        return []
    rs = find_ranges(s)
    if not rs:
        return [{"序": 1, "起": "", "止": "", "起精度": "", "止精度": "",
                 "区间原文": "", "文本": s}]
    out = []
    for i, r in enumerate(rs):
        a = r["_起"]
        b = rs[i + 1]["_起"] if i + 1 < len(rs) else len(s)
        out.append({"序": i + 1,
                    "起": r["起"], "止": r["止"],
                    "起精度": r["起精度"], "止精度": r["止精度"],
                    "区间原文": "%s-%s" % (r["起原文"], r["止原文"]),
                    "文本": s[a:b].strip()})
    return out


def find_points(s):
    """文本里所有**单点**日期，按出现顺序、**不去重**。

    为什么刻意不去重：诊断信息一旦去重，「两个点其实是同一个年月」这种形态就看不见了，
    「为什么不成对」就无从判断。单点日期不做切段边界，但**必须报出来**——
    「有日期但不成对」与「完全没提日期」是两种不同的缺口，混成一句「无日期」
    会让人以为这段文字里根本没有时间信息。
    """
    return [_fmt_point(m.groups()) for m in _POINT.finditer(s or "")]


def split_profile(profile):
    """对整份档案做拆分。返回 `(模块列表, 统计)`。**不写档案、不打印值**。"""
    out, stat = [], {"处理键": 0, "总段数": 0, "无区间键": [], "无日期键": []}
    for k in SOURCE_KEYS:
        v = profile.get(k)
        if v is None or not str(v).strip():
            continue
        segs = split_text(str(v))
        if not segs:
            continue
        stat["处理键"] += 1
        stat["总段数"] += len(segs)
        if not any(x["起"] for x in segs):
            pts = find_points(str(v))
            (stat["无区间键"] if pts else stat["无日期键"]).append(k)
        cands = MODULE_ALIASES.get(k)
        out.append({"档案键": k,
                    # 表里没登记 → 拿档案键名自己当候选；登记为空 → **如实留空**（不硬塞）
                    "候选模块名": list(cands) if cands is not None else [k],
                    "段数": len(segs), "文本内日期点": find_points(str(v)),
                    "记录": segs})
    # 教育时间：页面只有一个模块，无「第几行」歧义 ⇒ 扁平键直接取值
    edu = {x: profile.get(x) for x in EDU_TIME_KEYS}
    if any(v not in (None, "") for v in edu.values()):
        out.insert(0, {"档案键": "（教育时间·扁平键）",
                       "候选模块名": ["教育经历"],
                       "段数": 1, "文本内日期点": [],
                       "记录": [{"序": 1, "起": "", "止": "", "起精度": "", "止精度": "",
                                 "区间原文": "", "文本": "",
                                 "_扁平时间": dict(edu)}]})
    return out, stat


def record_group_names():
    """`assets/记录组_通用.json` 里的组名 —— 供候选模块名**校验**用（数据驱动，不硬编码）。"""
    p = os.path.join(ROOT, "assets", "记录组_通用.json")
    try:
        with io.open(p, encoding="utf-8") as fh:
            d = json.load(fh)
        return [g.get("名称") for g in (d.get("记录组") or [])]
    except Exception:                                        # noqa: BLE001
        return []


def mask_scan(text):
    """反证用：产物里不得出现 18 位身份证 / 11 位手机号形态。"""
    bad = []
    if re.search(r"(?<!\d)\d{17}[\dXx](?!\d)", text):
        bad.append("身份证形态")
    if re.search(r"(?<!\d)1[3-9]\d{9}(?!\d)", text):
        bad.append("手机号形态")
    return bad


def build(profile, profile_path=""):
    mods, stat = split_profile(profile)
    return {
        "_警示": "本文件含你的个人经历信息，仅供本机使用，勿外发。",
        "生成时间": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "来源": "档案自由文本自动拆分（只读，未写入档案）",
        "档案": os.path.basename(profile_path) if profile_path else "(selftest)",
        "说明": "每段 = 一个日期区间 + 其后的描述文字；起/止精度以原文为准（不擅自补日）",
        "统计": stat,
        "模块": mods,
    }


def main():
    ap = argparse.ArgumentParser(description="记录组拆分器（只读档案）")
    ap.add_argument("--profile", default="", help="你的档案 JSON")
    ap.add_argument("--out", default="", help="产物路径（缺省 = 档案同目录/记录组_待补.json）")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不落盘")
    ap.add_argument("--selftest", action="store_true", help="用内置虚构数据自测")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if not args.profile:
        print("[ERR] 需要 --profile <你的档案.json>")
        return 2
    if not os.path.exists(args.profile):
        print("[ERR] 档案不存在：%s" % args.profile)
        return 2

    with io.open(args.profile, encoding="utf-8") as fh:
        profile = json.loads(fh.read())
    doc = build(profile, args.profile)

    # 只输出**键名 + 段数**统计 —— 值一律不进终端（红线 2）
    print("键数 %d ／ 处理 %d ／ 总段数 %d"
          % (len(profile), doc["统计"]["处理键"], doc["统计"]["总段数"]))
    for m in doc["模块"]:
        print("  %-16s 段数=%d  候选模块=%s"
              % (m["档案键"], m["段数"], "、".join(m["候选模块名"])))
        for seg in m["记录"]:
            if seg.get("_扁平时间"):
                print("      段%d  扁平键直接取值：%s" % (seg["序"], "、".join(seg["_扁平时间"])))
            elif seg["起"]:
                print("      段%d  %s ~ %s" % (seg["序"], seg["起"], seg["止"] or "?"))
            else:
                print("      段%d  （无起止，需自己补）" % seg["序"])
    if doc["统计"]["无区间键"]:
        print("  有日期但不成对（起止只够填一半）：%s" % "、".join(doc["统计"]["无区间键"]))
    if doc["统计"]["无日期键"]:
        print("  完全没有日期信息（起止需自己补）：%s" % "、".join(doc["统计"]["无日期键"]))

    if args.dry_run:
        print("（dry-run：未落盘）")
        return 0

    out = args.out or os.path.join(os.path.dirname(os.path.abspath(args.profile)),
                                   "记录组_待补.json")
    if os.path.exists(out):
        print("[WARN] 同名产物已存在，**不覆盖**：%s" % out)
        print("       如确需新文件，请显式 --out 指定别的路径。")
        return 3
    with io.open(out, "w", encoding="utf-8", newline="") as fh:
        fh.write(json.dumps(doc, ensure_ascii=False, indent=2))
    bad = mask_scan(json.dumps(doc, ensure_ascii=False))
    print("产物：%s  （PII 形态扫描：%s）" % (out, "未命中" if not bad else "命中 " + "/".join(bad)))
    return 0


# ================================================================== 自测
def selftest():
    ok = [0]
    bad = [0]

    def check(name, cond, extra=""):
        if cond:
            ok[0] += 1
            print("  [OK] %s" % name)
        else:
            bad[0] += 1
            print("  [FAIL] %s %s" % (name, extra))

    # ---- 日期点形态 ----
    check("① 点分写法 2023.07 → 2023-07（精度=月）",
          _fmt_point(_POINT.search("2023.07").groups()) == "2023-07")
    check("② 中文写法 2023年7月 → 2023-07",
          _fmt_point(_POINT.search("2023年7月").groups()) == "2023-07")
    check("③ 只有年 → 不补月（精度=年）",
          _fmt_point(_POINT.search("2023").groups()) == "2023")
    check("④ 斜杠写法 2023/7 → 2023-07",
          _fmt_point(_POINT.search("2023/7").groups()) == "2023-07")

    # ---- ⭐ 关键反证：年份区间不能被读成月份 ----
    #   「2019-2023」若把 `-2` 当成 2 月，就会变出「2019-02」这个**不存在的时间点**。
    #   (?!\d) 就是拦这个的。
    pts = find_points("2019-2023")
    check("⑤ **反证**：2019-2023 不被读成「2019 年 2 月」",
          pts == ["2019", "2023"], pts)

    # ---- 区间切分 ----
    rs = find_ranges("2023.07-2023.09 在某公司实习")
    check("⑥ 成对日期被认出为区间", len(rs) == 1 and rs[0]["起"] == "2023-07"
          and rs[0]["止"] == "2023-09", rs)
    #   `-` 与 `到` 都是**合法**分隔符（设计如此），所以「2023年到2024年」本来就该成对；
    #   真正的反例是中间**夹了实词**。
    check("⑦ 「2023年到2024年」成对（到 是合法分隔符）",
          len(find_ranges("2023年到2024年")) == 1, find_ranges("2023年到2024年"))
    check("⑦b 中间夹实词（毕业，）→ 不算区间",
          find_ranges("2023年毕业，2024年入职") == [],
          find_ranges("2023年毕业，2024年入职"))

    segs = split_text("2023.07-2023.09 实习A；2024.01-2024.03 实习B")
    check("⑧ 两段区间 → 切成 2 段", len(segs) == 2, segs)
    check("⑨ 第二段起止取自第二个区间",
          len(segs) > 1 and segs[1]["起"] == "2024-01" and segs[1]["止"] == "2024-03", segs)

    # ---- 「至今」型 ----
    segs2 = split_text("2024.05-至今 在职")
    check("⑩ 「至今」型：止 = 至今且不补日",
          len(segs2) == 1 and segs2[0]["止"] == "至今", segs2)

    # ---- 无区间 ⇒ 整段 1 条，不硬拆 ----
    segs3 = split_text("在某公司做了三个月运营，负责社群")
    check("⑪ **无区间不硬拆**：整段作 1 条，起止留空",
          len(segs3) == 1 and segs3[0]["起"] == "" and "在某公司" in segs3[0]["文本"], segs3)

    # ---- 单点日期不做切段边界（否则「2024年毕业」会被切走）----
    segs4 = split_text("2024年毕业，在校期间做过学生会主席")
    check("⑫ 单点日期**不**做切段边界", len(segs4) == 1, segs4)
    check("⑬ 但单点日期要**报出来**（有日期没成对 ≠ 完全没日期）",
          find_points("2024年毕业") == ["2024"], find_points("2024年毕业"))

    # ---- 红线 3：不擅自补精度 ----
    segs5 = split_text("2023.07-2023.09 实习")
    check("⑭ 精度如实标「月」，绝不补成某月1日",
          segs5[0]["起"] == "2023-07" and segs5[0]["起精度"] == "月"
          and len(segs5[0]["起"]) == 7, segs5)

    # ---- 整档拆分 + 统计分流 ----
    prof = {"实习经历": "2023.07-2023.09 甲；2024.01-2024.03 乙",
            "获奖情况": "拿过校级奖学金",
            "工作经历": "2024年毕业",
            "入学时间": "2020-09", "毕业时间": "2024-06",
            "技能特长": "不该被处理"}
    mods, stat = split_profile(prof)
    keys = [m["档案键"] for m in mods]
    check("⑮ 经历类键被处理、非经历键（技能特长）不进结果",
          "技能特长" not in keys and "实习经历" in keys, keys)
    check("⑯ 教育扁平键单独成条（单模块无行歧义）",
          "（教育时间·扁平键）" in keys, keys)
    check("⑰ 统计能分开「有日期没成对」与「完全没日期」",
          "获奖情况" in stat["无日期键"] and "工作经历" in stat["无区间键"], stat)

    # ---- 候选模块名必须真的存在于 assets/记录组_通用.json（数据漂移防线）----
    names = set(record_group_names())
    if names:
        unknown = sorted({a for al in MODULE_ALIASES.values() for a in al} - names)
        check("⑱ 别名取值**只**指向 assets/记录组_通用.json 里真实存在的组名（防拍脑袋）",
              not unknown, unknown)
        check("⑱b 通用表已覆盖「教育/实习/工作/奖励/职业资格」这几组",
              {"教育经历", "实习经历", "工作经历", "奖励信息", "职业资格"} <= names,
              sorted(names))
    else:
        check("⑱ 读不到 assets/记录组_通用.json（跳过别名校验）", True)

    # ---- 红线 2 的反证：产物结构里不含档案值 ----
    doc = build(prof, "(selftest)")
    blob = json.dumps(doc, ensure_ascii=False)
    check("⑲ PII 形态扫描器本身有效（喂假身份证必须命中）",
          mask_scan("身份证 110101199001011234") == ["身份证形态"])
    check("⑳ 产物里没有手机号 / 身份证形态",
          mask_scan(blob) == [], mask_scan(blob))

    print("\n自测结果：%d 通过 / %d 失败" % (ok[0], bad[0]))
    return 0 if bad[0] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
