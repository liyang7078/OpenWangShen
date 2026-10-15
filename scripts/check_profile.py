#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_profile.py · 档案体检
============================
检查你的档案 JSON 有没有写全、有没有写错地方。
**只报告字段名与有无，绝不打印任何真实值。**

用法
----
  python scripts/check_profile.py --profile profile.json
  python scripts/check_profile.py --profile profile.json --strict   # 必填项缺失即非零退出
  python scripts/check_profile.py --selftest

许可：MIT
"""

import argparse
import contextlib
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core  # noqa: E402


def analyze(profile, fields, attach=None):
    """体检的**结构化结果**。

    把「算出来的东西」与「打印出来的东西」分开：自测断言结构化结果，
    而不是去抓屏匹配文案 —— 改一句提示语不该让测试变红。
    """
    rpt = core.profile_report(profile, fields)
    must_missing = [k for k in rpt["缺失"]
                    if core.field_meta(fields, k).get("必填")]
    return {"报告": rpt, "必填缺失": must_missing, "附件": list(attach or [])}


def report(profile, fields, attach=None):
    a = analyze(profile, fields, attach)
    rpt = a["报告"]

    print("=" * 56)
    print("档案体检报告（只列字段名，不显示值）")
    print("=" * 56)
    print("规则库字段：%d 个" % rpt["总数"])
    print("已填：%d 个 ｜ 缺失：%d 个" % (len(rpt["已填"]), len(rpt["缺失"])))
    print("")

    print("【分组覆盖】")
    for g, c in rpt["分组"].items():
        total = c["已填"] + c["缺失"]
        bar = "█" * c["已填"] + "·" * c["缺失"]
        print("  %-10s %s  %d/%d" % (g, bar, c["已填"], total))
    print("")

    if rpt["缺失"]:
        print("【缺失字段】（可选项缺失不影响投递，必填项缺失会拦下）")
        for k in rpt["缺失"]:
            meta = core.field_meta(fields, k)
            mark = "【必填】" if meta.get("必填") else "（选填）"
            print("  - %-16s %s" % (k, mark))
        print("")

    if rpt["档案未登记字段"]:
        print("【档案里有、但规则库不认识的字段】")
        for k in rpt["档案未登记字段"]:
            print("  - %s" % k)
        print("  提示：拼写错误会导致该项永远填不上。请对照 assets/字段规则_通用.json 的键名。")
        print("")

    if a["附件"]:
        print("【附件类字段】%d 个（需准备文件，不存放于档案）" % len(a["附件"]))
        for f in a["附件"]:
            mark = "【必传】" if f.get("必填") else "（选传）"
            print("  - %-8s %s" % (core.attachment_label(f["键"]), mark))
        print("")

    print("【结论】")
    if a["必填缺失"]:
        print("  ⚠️ 必填项缺失 %d 个：%s"
              % (len(a["必填缺失"]), "、".join(a["必填缺失"])))
        print("     这些字段在真实网申里会被拦下，建议补齐后再投。")
    else:
        print("  ✅ 必填项齐全。")
    return a["必填缺失"]


def selftest():
    ok, bad = [0], [0]

    def check(name, cond, extra=""):
        if cond:
            ok[0] += 1
            print("  [OK] %s" % name)
        else:
            bad[0] += 1
            print("  [FAIL] %s %s" % (name, extra))

    fixture = os.path.join(core.PROJECT_ROOT, "fixtures", "档案_示例.json")
    fields_all = core.load_rules(None)
    attach = [f for f in fields_all if core.is_attachment(f["键"])]
    fields = [f for f in fields_all if not core.is_attachment(f["键"])]

    # ---- ① 基本盘：结果结构自洽 ----
    prof = core.load_profile(fixture)
    a = analyze(prof, fields, attach)
    rpt = a["报告"]
    check("① 示例档案能加载且规则库字段数 > 0", bool(prof) and rpt["总数"] > 0,
          rpt["总数"])
    check("② 已填 + 缺失 == 总数",
          len(rpt["已填"]) + len(rpt["缺失"]) == rpt["总数"],
          (len(rpt["已填"]), len(rpt["缺失"]), rpt["总数"]))

    known = [f["键"] for f in fields]
    check("③ 已填∪缺失 == 规则库键集，且两者不交",
          set(rpt["已填"]) | set(rpt["缺失"]) == set(known)
          and not (set(rpt["已填"]) & set(rpt["缺失"])),
          len(set(rpt["已填"]) | set(rpt["缺失"])))
    check("④ 必填缺失 ⊆ 缺失",
          set(a["必填缺失"]) <= set(rpt["缺失"]),
          set(a["必填缺失"]) - set(rpt["缺失"]))
    check("⑤ 分组计数之和 == 总数",
          sum(c["已填"] + c["缺失"] for c in rpt["分组"].values()) == rpt["总数"])
    check("⑥ 附件字段被排除在统计之外（不与档案字段重叠）",
          not (set(f["键"] for f in attach) & set(known)),
          set(f["键"] for f in attach) & set(known))

    # ---- ② 空档案：边界 ----
    a0 = analyze({}, fields, attach)
    check("⑦ 空档案：已填 0、缺失 == 总数",
          len(a0["报告"]["已填"]) == 0
          and len(a0["报告"]["缺失"]) == a0["报告"]["总数"])
    check("⑧ 空档案：必填缺失非空（不能把「什么都没填」读成没问题）",
          bool(a0["必填缺失"]), len(a0["必填缺失"]))

    # ---- ③ 不认识的字 → 必须出现在「档案未登记字段」里 ----
    a1 = analyze({"这个名字规则库里没有": "x"}, fields, None)
    check("⑨ 规则库不认识的键进入「档案未登记字段」",
          a1["报告"]["档案未登记字段"] == ["这个名字规则库里没有"],
          a1["报告"]["档案未登记字段"])
    check("⑩ 下划线开头的注释键不算未登记字段",
          analyze({"_备注": "x"}, fields, None)["报告"]["档案未登记字段"] == [])

    # ---- ④ 真值不外泄：把「只列字段名、不显示值」变成执行点 ----
    # 探针**从 fixture 现取**，不写字面量 —— 写字面量会被 dist_check 举报本仓库。
    leak_probes = [str(v) for k, v in prof.items()
                   if not str(k).startswith("_") and len(str(v)) >= 4]
    check("⑪ 探针取到了（否则下面两条是空转）", len(leak_probes) >= 5,
          len(leak_probes))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        report(prof, fields, attach)
    out = buf.getvalue()
    hit = [v for v in leak_probes if v in out]
    check("⑫ 报告正文里不出现档案里的任何**值**", not hit, hit[:3])

    # 反向：报告必须**确实提到了字段名**，否则「不泄露」可能只是因为什么都没打印
    check("⑬ 报告确实列出了字段名（不泄露不是因为没输出）",
          all(k in out for k in list(rpt["已填"])[:1] or ["姓名"]))

    # 再给一个唯一哨兵，确保不是「碰巧」不漏
    sentinel = "ZZ" + "-" + "leakprobe"
    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        report({"姓名": sentinel}, fields, None)
    out2 = buf2.getvalue()
    check("⑭ 哨兵值不外泄，但字段名照常出现",
          sentinel not in out2 and "姓名" in out2)

    # ---- ⑤ --strict 语义：必填缺失 → 退出码 2 ----
    import json
    import tempfile
    tmpd = tempfile.mkdtemp(prefix="ows-check-")
    empty = os.path.join(tmpd, "空档案.json")
    with open(empty, "w", encoding="utf-8") as fh:
        json.dump({"_测试": "空档案"}, fh, ensure_ascii=False)
    buf3 = io.StringIO()
    with contextlib.redirect_stdout(buf3):
        rc_strict = main(["--profile", empty, "--strict"])
        rc_soft = main(["--profile", empty])
    check("⑮ --strict：必填缺失 → 退出码 2", rc_strict == 2, rc_strict)
    check("⑯ 不加 --strict：只提示不改退出码", rc_soft == 0, rc_soft)

    print("\n自测结果：%d 通过 / %d 失败" % (ok[0], bad[0]))
    return 0 if bad[0] == 0 else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="本机档案体检")
    ap.add_argument("--profile", help="档案 JSON 路径")
    ap.add_argument("--rules", help="字段规则库路径")
    ap.add_argument("--strict", action="store_true", help="必填项缺失则退出码非 0")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    if not args.profile:
        ap.error("需要 --profile（或使用 --selftest）")

    fields = core.load_rules(args.rules)
    # 附件类字段（简历/成绩单/证件照…）是**文件**，不存放在档案里，
    # 因此不参与「档案必填缺失」判定，单独提示。
    attach = [f for f in fields if core.is_attachment(f["键"])]
    fields = [f for f in fields if not core.is_attachment(f["键"])]

    profile = core.load_profile(args.profile)
    must_missing = report(profile, fields, attach)

    if args.strict and must_missing:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
