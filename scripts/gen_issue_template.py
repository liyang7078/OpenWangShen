#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_issue_template.py · 从脚本选项集生成 Issue 模板
====================================================
它解决什么
----------
`.github/ISSUE_TEMPLATE/字段识别失败.md` 与 `report_feedback.py` 的选项集
**以前是两处各写一遍**：脚本里改了「实际结果」的可选值，模板里的例子还是旧的。
考生照着旧模板填，作者侧就解析不出那一条 —— 而**没有任何东西会报错**。

本脚本把模板变成**产物**：选项集只有一个来源（`report_feedback.py` 里那几个常量），
模板由它们现算生成。另加 `--check` 作为执行点：模板与脚本不一致就红。

用法
----
  python scripts/gen_issue_template.py            # 生成（默认）
  python scripts/gen_issue_template.py --check    # 只校验是否漂移（CI 用）
  python scripts/gen_issue_template.py --selftest

退出码：0 = 一致 / 已写入；3 = 漂移（--check）；2 = 文件错误

许可：MIT
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import report_feedback as rf  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TEMPLATE = os.path.join(ROOT, ".github", "ISSUE_TEMPLATE", "字段识别失败.md")

# 表格里的「项」必须与 report_feedback.render() 逐字一致 ——
# 作者侧的 inbox.parse_body 靠这些键名取值，改名即解析失效。
TABLE_KEYS = (
    ("页面字段标签（含星号）", "`联系人手机*`"),
    ("页面所在系统", None),          # None = 用 rf.system_choices() 现填提示
    ("站点域名", "job.example.com（只填域名，别带后面的参数）"),
    ("页面技术栈", None),            # 用 rf.STACK_CHOICES
    ("控件类型", None),              # 用 rf.CONTROL_CHOICES
    ("期望映射到哪个键", "`手机号`"),
    ("实际结果", None),              # 用 rf.RESULTS
    ("缺口类别", None),              # 用 rf.GAP_CHOICES
)


def _opts(seq, keep=None):
    """把选项集拼成「甲 / 乙 / 丙」提示串。

    **默认不截断。** 这些不是"提示"而是**有效取值的枚举**：漏掉的取值会让
    考生以为只能在列出的几个里挑。历史上这里写死了 `keep=8`，
    于是 9 项的「控件类型」少了 1 项、11 项的系统名单少了 3 项 —— 且无声无息。
    """
    items = [str(x) for x in seq]
    if keep:
        items = items[:keep]
    return " / ".join(items)


def build():
    """生成模板全文。"""
    rows = []
    for key, example in TABLE_KEYS:
        if key == "页面所在系统":
            tip = _opts(rf.system_choices())
        elif key == "页面技术栈":
            tip = _opts(rf.STACK_CHOICES)
        elif key == "控件类型":
            tip = _opts(rf.CONTROL_CHOICES)
        elif key == "实际结果":
            tip = _opts(rf.RESULTS)
        elif key == "缺口类别":
            tip = _opts(rf.GAP_CHOICES)
        else:
            tip = example or ""
        rows.append("| %s | %s |" % (key, tip))

    L = [
        "---",
        "name: 字段识别失败",
        "about: 某个页面字段没被识别出来。补规则通常只要几分钟",
        'title: "[字段] 某字段未被识别"',
        "labels: field-mapping",
        "---",
        "",
        "## 字段信息",
        "",
        "<!-- 这张表由 scripts/gen_issue_template.py 依据 report_feedback.py "
        "的选项集生成，请勿手改：改选项集请改脚本；改本文件请改生成器。 -->",
        "<!-- 请照实填，注意不要贴真实个人信息（别贴身份证号、手机号、详细住址）。 -->",
        "",
        "| 项 | 内容 |",
        "|---|---|",
    ] + rows + [
        "",
        "> 「缺口类别」是**归因**（问题出在哪一类），它决定作者先修哪一条，",
        "> 比「实际结果」更有用。不确定就写「不确定」，别硬猜。",
        "",
        "## 表单控件的 HTML（可选，但很有帮助）",
        "",
        "<!-- 在页面上右键该输入框 → 检查 → 复制外层 <input> 或 <select> 标签 -->",
        "",
        "```html",
        '<input type="text" name="..." id="..." placeholder="...">',
        "```",
        "",
        "## 环境",
        "",
        "- 操作系统：",
        "- Python 版本：",
        "- 是否装了 playwright：",
        "",
        "## 自测结果",
        "",
        "```",
        "python scripts/wangshen.py selftest      →",
        "python -m unittest discover -s tests     →",
        "```",
        "",
    ]
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="从脚本选项集生成 Issue 模板（防模板与脚本漂移）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python scripts/gen_issue_template.py --check\n")
    ap.add_argument("--check", action="store_true", help="只校验是否漂移，不写入")
    ap.add_argument("--out", default=TEMPLATE, help="模板输出路径")
    ap.add_argument("--selftest", action="store_true", help="自测")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()

    text = build()
    if args.check:
        cur = ""
        if os.path.isfile(args.out):
            with open(args.out, "r", encoding="utf-8") as fh:
                cur = fh.read()
        if cur.replace("\r\n", "\n") == text:
            print("[OK] Issue 模板与脚本选项集一致：%s" % args.out)
            return 0
        print("[漂移] 模板与脚本选项集不一致 —— 请跑 gen_issue_template.py 重新生成：")
        print("       %s" % args.out)
        return 3

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    print("[OK] 已生成 Issue 模板：%s" % args.out)
    print("     选项集来源：scripts/report_feedback.py（唯一来源，不会再漂移）")
    return 0


def _selftest():
    ok, bad = [0], [0]

    def check(name, cond, extra=""):
        if cond:
            ok[0] += 1
            print("  [OK] %s" % name)
        else:
            bad[0] += 1
            print("  [FAIL] %s %s" % (name, extra))

    text = build()
    check("① 模板含全部表格键",
          all(k in text for k, _ in TABLE_KEYS), TABLE_KEYS[:2])
    check("② 选项集取自脚本（实际结果逐项出现）",
          all(r in text for r in rf.RESULTS))
    check("③ 「缺口类别」选项逐项出现",
          all(g in text for g in rf.GAP_CHOICES))
    check("④ 站点名单不含「其他」垃圾桶", "其他" not in rf.system_choices())
    check("⑤ 模板与 render() 的表头一致", "| 项 | 内容 |\n|---|---|" in text)
    check("⑥ 生成是纯函数（两次结果相同）", build() == text)

    # 执行点：选项集在模板里**一个都不许少**。
    # 这句对应一个真实缺陷：`_opts` 曾写死 keep=8，静默吞掉 3 个系统名与 1 个控件类型。
    missing = [x for seq in (rf.RESULTS, rf.GAP_CHOICES, rf.STACK_CHOICES,
                             rf.CONTROL_CHOICES, rf.system_choices())
               for x in seq if x not in text]
    check("⑥b 选项集在模板里一个都不少（不许静默截断）", not missing, missing)

    # --check 的判据必须真能报红
    import tempfile
    tmp = tempfile.mkdtemp(prefix="ows_tpl_")
    p = os.path.join(tmp, "t.md")
    with open(p, "w", encoding="utf-8", newline="") as fh:
        fh.write("故意写错的内容")
    rc = main(["--check", "--out", p])
    check("⑦ 漂移时 --check 返回 3", rc == 3, rc)
    with open(p, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    rc2 = main(["--check", "--out", p])
    check("⑧ 一致时 --check 返回 0", rc2 == 0, rc2)

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    print("\n自测结果：%d 通过 / %d 失败" % (ok[0], bad[0]))
    return 0 if bad[0] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
