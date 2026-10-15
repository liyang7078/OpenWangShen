#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_checklist.py · 网申信息清单生成器（空白问卷）
==================================================
解决的是自动填表**之前**那一步：没有档案，填表无从谈起。

它把「投网申之前要先备好哪些信息」现算成一张**空白**问卷。
清单口径由词表现算 —— 词表加字段，清单自动跟着变，**不会过期**。

四档（判据只有一条：看词表怎么标，不另写一套逻辑）
  ① 必填    —— 词表标了 `必填` 的值字段（缺一条，多数网申就提交不了）
  ② 常用    —— `COMMON_KEYS` 里的键（几乎每个站都会问，建议一次填全）
  ③ 其余选填 —— 其余值字段（问到了就填，没问到直接跳过）
  ④ 记录组  —— 站点上以「表格 + 添加一行」呈现的多条结构（`assets/记录组_通用.json`）

三条硬性质
  · **零网络**：只读本机词表，不发任何请求
  · **零真值**：产物是**空白**问卷，不含任何既有信息 —— 所以可以安全地交给 AI 陪你逐项过
  · **不覆盖**：同名文件默认改存带时间戳的新名（与 `make_sheet.py` 一致）

用法
  python scripts/make_checklist.py
  python scripts/make_checklist.py --out 网申信息清单.md
  python scripts/make_checklist.py --selftest
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ASSETS = os.path.join(ROOT, "assets")
RULES = os.path.join(ASSETS, "字段规则_通用.json")
GROUPS = os.path.join(ASSETS, "记录组_通用.json")
FIXTURE = os.path.join(ROOT, "fixtures", "档案_示例.json")
DEFAULT_OUT = "网申信息清单.md"
ATTACH_PREFIX = "附件_"

# 公开版词表用英文类型（私有侧同步时按 TYPE_EN 转换），这里换回中文给人看
TYPE_CN = {"text": "文本", "textarea": "多行文本", "number": "数字",
           "date": "日期", "select": "下拉", "radio": "单选",
           "checkbox": "复选", "file": "文件"}

TYPE_HINT = {
    "日期": "格式 YYYY-MM 或 YYYY-MM-DD",
    "多行文本": "可多行；多条用「；」或换行分隔",
    "文件": "上传文件，本问卷不填",
}

BAND_REQUIRED = "必填"
BAND_COMMON = "常用"
BAND_REST = "其余选填"
BAND_ATTACH = "附件"

BAND_TITLES = {
    BAND_REQUIRED: "① 必填 —— 缺一条，多数网申就提交不了",
    BAND_COMMON: "② 常用 —— 几乎每个站都会问，建议一次填全",
    BAND_REST: "③ 其余选填 —— 问到了就填，没问到直接跳过",
}

# 「常用」档：网申表单高频出现、但词表未标必填的项。
# 依据 = 三站实测 + 常见 ATS 表单归纳。**要调整就改这个列表，别改逻辑。**
COMMON_KEYS = [
    "通讯地址", "民族", "学位", "入学时间", "本科院校", "本科专业",
    "外语水平", "证书", "实习经历", "项目经历", "校园经历", "获奖情况",
    "技能特长", "自我评价", "意向岗位", "意向城市", "到岗时间", "是否应届",
    "紧急联系人姓名", "紧急联系人电话",
    # 实测这 4 项在传统自建站「基本信息」区块常被列为必填，却因被归在「其余选填」
    # 而整段跳过、档案里常年为空。提到「常用」让它们出现在清单显眼处。
    "身高", "体重", "生源地", "籍贯",
]

PII_PAT = {
    "手机号": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    "身份证": re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\d)"),
    "邮箱": re.compile(r"(?i)(?<![\w.+-])[\w.+-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+"),
}


def is_attach(field):
    return str(field.get("键", "")).startswith(ATTACH_PREFIX)


def band_of(field, common=None):
    common = COMMON_KEYS if common is None else common
    if is_attach(field):
        return BAND_ATTACH
    if field.get("必填"):
        return BAND_REQUIRED
    if field.get("键") in common:
        return BAND_COMMON
    return BAND_REST


def bucket(fields, common=None):
    out = {BAND_REQUIRED: [], BAND_COMMON: [], BAND_REST: [], BAND_ATTACH: []}
    for f in fields:
        out[band_of(f, common)].append(f)
    return out


def load_rules(path=RULES):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return (data.get("字段") or []), (data.get("值域别名") or {})


def load_groups(path=GROUPS):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("记录组") or []


def hint_of(field, domains=None):
    """可选值 + 格式提示。**没有就留空，不猜。**"""
    domains = domains or {}
    notes = []
    dom = domains.get(field.get("值域"))
    if dom:
        notes.append("可选：%s" % " / ".join(dom.keys()))
    t = TYPE_CN.get(str(field.get("类型") or ""), str(field.get("类型") or ""))
    if TYPE_HINT.get(t):
        notes.append(TYPE_HINT[t])
    return "；".join(notes)


def render(fields, domains=None, groups=None, today=None):
    groups = groups or []
    b = bucket(fields)
    n_val = len(b[BAND_REQUIRED]) + len(b[BAND_COMMON]) + len(b[BAND_REST])
    n_rec = sum(len(g.get("字段") or []) for g in groups)
    day = today or datetime.datetime.now().strftime("%Y-%m-%d")

    L = []
    L.append("# 网申信息清单（空白问卷）")
    L.append("")
    L.append("> **这张表里没有任何你的已有信息** —— 所以它可以安全地交给 AI，让 AI 陪你逐项过。")
    L.append(">")
    L.append("> **怎么填**：在每行最后一列写答案。不确定的、没有的，**留空就行** ——")
    L.append("> 留空就会被记成「待补」，不会有谁替你猜一个填进去。")
    L.append(">")
    L.append("> **填完怎么办**：自己存好。⚠️ **别外发、别上传、别贴进任何在线工具。**")
    L.append(">")
    L.append("> **口径从哪来**：由 `assets/字段规则_通用.json` 现算。词表加字段，清单自动"
             "跟着变 —— 所以它不会过期。")
    L.append("")
    L.append("**规模**：值字段 %d 项 —— 必填 %d ｜ 常用 %d ｜ 其余选填 %d"
             "；另记录组 %d 组 / %d 项"
             % (n_val, len(b[BAND_REQUIRED]), len(b[BAND_COMMON]),
                len(b[BAND_REST]), len(groups), n_rec))
    L.append("")
    L.append("生成时间：%s" % day)
    L.append("")
    L.append("---")
    L.append("")

    for band in (BAND_REQUIRED, BAND_COMMON, BAND_REST):
        rows = b[band]
        L.append("## %s（%d 项）" % (BAND_TITLES[band], len(rows)))
        L.append("")
        L.append("| 字段 | 类型 | 提示 | 填写 |")
        L.append("|---|---|---|---|")
        for f in rows:
            L.append("| %s | %s | %s |  |"
                     % (f.get("键", ""), TYPE_CN.get(str(f.get("类型") or ""), ""),
                        hint_of(f, domains)))
        L.append("")

    if groups:
        L.append("## ④ 记录组 —— 站点上「表格 + 添加一行」的多条结构（%d 组 / %d 项）"
                 % (len(groups), n_rec))
        L.append("")
        L.append("> 这些**不是单值，是可以有多条的**经历。有几条写几条，没有就整组留空。")
        L.append("")
        for g in groups:
            L.append("### %s（%d 项）" % (g.get("名称", ""), len(g.get("字段") or [])))
            L.append("")
            L.append("| 字段 | 填写 |")
            L.append("|---|---|")
            for name in g.get("字段") or []:
                L.append("| %s |  |" % name)
            L.append("")

    att = b[BAND_ATTACH]
    if att:
        L.append("## 附：上传材料（%d 类，**不在上面的问卷里**）" % len(att))
        L.append("")
        L.append("> 这些是**上传文件**，投递时按企业简章**自己选、自己传** ——")
        L.append("> 程序不代选、不代传、也不登记。这里只列出常见的类目，方便你先备齐。")
        L.append("")
        for f in att:
            L.append("- %s" % str(f.get("键", ""))[len(ATTACH_PREFIX):])
        L.append("")

    return "\n".join(L) + "\n"


def _selftest():
    ok = [0]
    bad = [0]

    def check(name, cond, extra=""):
        if cond:
            ok[0] += 1
            print("  [OK] %s" % name)
        else:
            bad[0] += 1
            print("  [FAIL] %s %s" % (name, extra))

    print("=" * 68)
    print("make_checklist.py 自测")
    print("=" * 68)

    before = hashlib.md5(open(RULES, "rb").read()).hexdigest()

    fields, domains = load_rules()
    groups = load_groups()
    md = render(fields, domains, groups, today="2026-09-30")

    val = [f for f in fields if not is_attach(f)]
    att = [f for f in fields if is_attach(f)]
    check("① 词表可读且有字段", len(fields) > 0 and len(val) > 0, len(fields))
    check("② 附件前缀识别正确（值字段 %d / 附件 %d）" % (len(val), len(att)),
          len(att) > 0 and len(val) + len(att) == len(fields))

    b = bucket(fields)
    covered = sum(len(b[x]) for x in (BAND_REQUIRED, BAND_COMMON, BAND_REST, BAND_ATTACH))
    check("③ 四档不重不漏（覆盖 %d = 全表 %d）" % (covered, len(fields)),
          covered == len(fields))
    check("④ 必填档 = 词表标必填的值字段数",
          len(b[BAND_REQUIRED]) == len([f for f in val if f.get("必填")]),
          len(b[BAND_REQUIRED]))
    check("⑤ 常用档只收词表里真实存在的键",
          all(f.get("键") in COMMON_KEYS for f in b[BAND_COMMON])
          and len(b[BAND_COMMON]) > 0, len(b[BAND_COMMON]))
    check("⑥ 附件不混进必填档（必填档无 附件_ 前缀）",
          all(not is_attach(f) for f in b[BAND_REQUIRED]))
    check("⑦ 记录组可读且有字段",
          len(groups) > 0 and sum(len(g.get("字段") or []) for g in groups) > 0,
          len(groups))

    check("⑧ 产物含四档标题",
          all(t in md for t in ("① 必填", "② 常用", "③ 其余选填", "④ 记录组")))
    check("⑨ 产物每档有表头且行数对得上",
          md.count("| 字段 | 类型 | 提示 | 填写 |") == 3
          and md.count("| 字段 | 填写 |") == len(groups))

    hit = [k for k, p in PII_PAT.items() if p.search(md)]
    check("⑩ 产物零真值（值形态扫描）", not hit, hit)

    leak = []
    if os.path.isfile(FIXTURE):
        with open(FIXTURE, "r", encoding="utf-8") as f:
            fx = json.load(f)

        def walk(v):
            if isinstance(v, str):
                yield v
            elif isinstance(v, dict):
                for x in v.values():
                    for y in walk(x):
                        yield y
            elif isinstance(v, list):
                for x in v:
                    for y in walk(x):
                        yield y

        for s in walk(fx):
            if len(s) >= 6 and s in md:
                leak.append(s)
    else:
        fx = None
    check("⑪ 产物不含示例档案里的值", not leak, leak[:3])
    check("⑫ 示例档案夹具存在（零真值校验的前提）", fx is not None)

    after = hashlib.md5(open(RULES, "rb").read()).hexdigest()
    check("⑬ 自测不改动词表", before == after)

    check("⑭ 产物行尾为 LF（无 CRLF）", "\r" not in md)

    # 产物落系统临时目录，绝不回写仓库（与 v0.1.1 的教训一致）
    tmp = tempfile.mkdtemp(prefix="checklist_")
    p = os.path.join(tmp, "网申信息清单.md")
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(md)
    check("⑮ 产物可落盘且非空", os.path.getsize(p) > 500, os.path.getsize(p))

    # 统一 safe_write 的接线实测：默认不覆盖同名产物（换名另存）
    q = os.path.join(tmp, "safe_write探针.md")
    q1 = core.safe_write(q, "第一版")
    q2 = core.safe_write(q, "第二版")
    check("⑯ 默认不覆盖同名产物（统一走 core.safe_write）",
          q1 == os.path.abspath(q) and q2 != q1
          and open(q1, encoding="utf-8").read() == "第一版",
          (q1, q2))
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

    print("")
    print("自测结果：%d 通过 / %d 失败" % (ok[0], bad[0]))
    return 0 if bad[0] == 0 else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="生成网申信息清单（空白问卷）")
    ap.add_argument("--rules", default=RULES, help="字段词表路径")
    ap.add_argument("--groups", default=GROUPS, help="记录组路径")
    ap.add_argument("--out", default=DEFAULT_OUT, help="输出路径（.md）")
    ap.add_argument("--overwrite", action="store_true",
                    help="允许覆盖同名文件（默认改存带时间戳的新名）")
    ap.add_argument("--selftest", action="store_true", help="用内置词表自测，不写仓库")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()

    try:
        fields, domains = load_rules(args.rules)
    except (OSError, ValueError) as e:
        print("[ERR] 读词表失败：%s" % e)
        return 2
    try:
        groups = load_groups(args.groups)
    except (OSError, ValueError) as e:
        print("[WARN] 读记录组失败，将跳过第 ④ 档：%s" % e)
        groups = []

    out = args.out
    md = render(fields, domains, groups)
    # 统一 safe_write（2026-10-16）：默认不覆盖同名（换名另存），原子替换。
    wrote = core.safe_write(out, md, overwrite=args.overwrite)
    if os.path.abspath(wrote) != os.path.abspath(out):
        print("[WARN] 同名文件已存在，改存为：%s" % wrote)

    b = bucket(fields)
    print("[OK] 已生成 %s" % wrote)
    print("     值字段 %d（必填 %d / 常用 %d / 其余选填 %d）｜ 附件 %d ｜ 记录组 %d 组"
          % (len(b[BAND_REQUIRED]) + len(b[BAND_COMMON]) + len(b[BAND_REST]),
             len(b[BAND_REQUIRED]), len(b[BAND_COMMON]), len(b[BAND_REST]),
             len(b[BAND_ATTACH]), len(groups)))
    print("     产物为空白问卷，不含任何你的信息 —— 可安全交给 AI 陪你逐项过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
