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
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core  # noqa: E402


def report(profile, fields, attach=None):
    rpt = core.profile_report(profile, fields)

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

    # 必填项缺失统计
    must_missing = [k for k in rpt["缺失"]
                    if core.field_meta(fields, k).get("必填")]

    if attach:
        print("【附件类字段】%d 个（需准备文件，不存放于档案）" % len(attach))
        for f in attach:
            mark = "【必传】" if f.get("必填") else "（选传）"
            print("  - %-8s %s" % (core.attachment_label(f["键"]), mark))
        print("")

    print("【结论】")
    if must_missing:
        print("  ⚠️ 必填项缺失 %d 个：%s" % (len(must_missing), "、".join(must_missing)))
        print("     这些字段在真实网申里会被拦下，建议补齐后再投。")
    else:
        print("  ✅ 必填项齐全。")
    return must_missing


def main():
    ap = argparse.ArgumentParser(description="本机档案体检")
    ap.add_argument("--profile", help="档案 JSON 路径")
    ap.add_argument("--rules", help="字段规则库路径")
    ap.add_argument("--strict", action="store_true", help="必填项缺失则退出码非 0")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        args.profile = os.path.join(core.PROJECT_ROOT, "fixtures", "档案_示例.json")
        print("【自测模式】使用 fixtures/档案_示例.json\n")

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
