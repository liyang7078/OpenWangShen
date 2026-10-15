#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
report_feedback.py · 反馈包生成（考生侧）
==========================================
把「你踩到的坑」变成一条**可以直接提交的问题反馈**。

它解决什么
----------
自动填表总会遇到一些框填不上。过去这件事就止步于「你自己想办法」——
但**同一个坑，下一个考生还会踩**，除非有人把它记下来。

你只要回答几个问题，这份脚本会：
  ① 生成一段**可以直接粘贴**的 Issue 文本（含环境信息）
  ② 可选：把同样内容存成本机一份 JSON
  ③ 把提交地址一并打印出来

隐私（硬约束）
--------------
产物**只含字段标签、站点名、环境信息**，不含你的任何真实取值。
输出与写盘前统一过 `issue_registry.guard()` 反证：
命中手机号 / 邮箱 / 身份证 / 本机绝对路径 → **中止，既不打印也不写盘**。

用法
----
  python scripts/report_feedback.py                      交互式，答几问
  python scripts/report_feedback.py --save               顺便在本机存一份
  python scripts/report_feedback.py --from-report 填表报告.json
  python scripts/report_feedback.py --field "联系人手机*" --system 北森 \
      --key 手机号 --result 未命中                        免交互（脚本调用）
  python scripts/report_feedback.py --selftest

许可：MIT
"""

import argparse
import datetime
import json
import os
import platform
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import issue_registry as reg  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT_DIR = os.path.join(ROOT, "feedback_out")

REPO = "liyang7078/OpenWangShen"
ISSUE_NEW = "https://github.com/%s/issues/new" % REPO

SYSTEMS = ("北森", "Moka", "大易", "智联", "其他", "不知道")
RESULTS = ("未命中", "映射到了错误的键", "选项对不上", "其他")

# 结果 → 严重度（预判，最终由作者在试验阶段复核）
SEVERITY = {
    "未命中": "中",
    "映射到了错误的键": "高",
    "选项对不上": "中",
    "其他": "低",
}


# --------------------------------------------------------------------------
# 环境信息（不含路径，可安全外发）
# --------------------------------------------------------------------------
def playwright_state():
    try:
        import playwright  # noqa: F401
        return "是"
    except ImportError:
        return "否"


def env_lines():
    return [
        "- 操作系统：%s" % platform.platform(),
        "- Python 版本：%s" % platform.python_version(),
        "- 是否装了 playwright：%s" % playwright_state(),
    ]


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------
def render(payload):
    """渲染成 Issue 正文。结构与 .github/ISSUE_TEMPLATE/字段识别失败.md 对齐。"""
    L = [
        "## 字段信息", "",
        "| 项 | 内容 |", "|---|---|",
        "| 页面字段标签（含星号） | `%s` |" % payload.get("字段标签", ""),
        "| 页面所在系统 | %s |" % payload.get("系统", "不知道"),
        "| 期望映射到哪个键 | `%s` |" % (payload.get("期望键") or "不确定"),
        "| 实际结果 | %s |" % payload.get("实际结果", ""),
        "",
    ]
    if payload.get("控件HTML"):
        L += ["## 表单控件的 HTML", "", "```html",
              payload["控件HTML"].strip(), "```", ""]
    if payload.get("补充说明"):
        L += ["## 补充说明", "", payload["补充说明"].strip(), ""]
    L += ["## 环境", ""] + env_lines() + [""]
    return "\n".join(L)


def to_issue(payload, source=""):
    """转成问题库条目（`inbox --accept` 用同一套映射，保证两侧一致）。"""
    lab = payload.get("字段标签", "")
    res = payload.get("实际结果", "")
    sysname = payload.get("系统", "") or "不知道"
    item = {
        "现象": "字段「%s」%s" % (lab, res),
        "站点": [sysname] if sysname != "不知道" else ["*"],
        "指纹": [],                      # 考生不懂指纹，留空；试验阶段由作者补
        "影响": "该字段需手工填写",
        "严重度": SEVERITY.get(res, "中"),
        "路径": [],
        "来源": "考生反馈",
    }
    if source:
        item["来源Issue"] = source
    return item


# --------------------------------------------------------------------------
# 交互
# --------------------------------------------------------------------------
def _ask(prompt, default="", choices=None, allow_empty=False):
    hint = ""
    if choices:
        hint = "（%s）" % "/".join(choices)
    elif default:
        hint = "（回车用 %s）" % default
    while True:
        try:
            s = input("%s %s" % (prompt, hint)).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return default
        s = s or default
        if choices and s not in choices:
            print("   ⚠️ 请从 %s 里选一个。" % "/".join(choices))
            continue
        if s:
            return s
        if allow_empty:
            return ""


def pick_from_report(path):
    """从 fill 产出的报告里取「规则库未命中」字段，让考生挑一个。"""
    if not os.path.isfile(path):
        print("[ERR] 找不到报告文件：%s" % os.path.basename(path))
        return None
    with open(path, "r", encoding="utf-8") as fh:
        try:
            rep = json.load(fh)
        except Exception as exc:                      # noqa: BLE001
            print("[ERR] 报告不是合法 JSON：%s" % exc)
            return None
    # 只读「规则库未命中」（纯字段标签，零真值）。
    # 刻意不碰「填写结果 / 回读」——那两段含真值，读了就有外泄风险。
    miss = [x for x in (rep.get("规则库未命中") or []) if x]
    if not miss:
        print("这份报告里没有未命中的字段 —— 换个方式描述吧。")
        return None
    print("报告里未命中的字段：")
    for i, m in enumerate(miss, 1):
        print("  %d) %s" % (i, m))
    sel = _ask("选一个（填序号，回车取第 1 个）", default="1")
    try:
        return miss[int(sel) - 1]
    except (ValueError, IndexError):
        return miss[0]


# --------------------------------------------------------------------------
# 自测
# --------------------------------------------------------------------------
def _selftest():
    ok, bad = [0], [0]

    def check(name, cond, extra=""):
        if cond:
            ok[0] += 1
            print("  [OK] %s" % name)
        else:
            bad[0] += 1
            print("  [FAIL] %s %s" % (name, extra))

    demo = {"字段标签": "联系人手机*", "系统": "北森",
            "期望键": "手机号", "实际结果": "未命中"}
    txt = render(demo)
    check("① 渲染出字段表", "| 页面字段标签（含星号） | `联系人手机*` |" in txt)
    check("② 渲染含环境段", "## 环境" in txt and "Python 版本" in txt)
    check("③ 渲染零真值（过 guard）", reg.guard(txt)[0] is True)

    # 反证：掺进真值必须被拒（这是产物的硬门，不是文档里的承诺）
    dirty = txt + "\n联系电话：13800000000"
    check("④ 掺手机号 → 拒绝", reg.guard(dirty)[0] is False)
    dirty2 = txt + "\n简历路径：D:/resume.pdf"
    check("⑤ 掺本机路径 → 拒绝", reg.guard(dirty2)[0] is False)

    item = to_issue(demo, source=7)
    check("⑥ 转条目：站点取自系统名", item["站点"] == ["北森"])
    check("⑦ 转条目：严重度随结果", item["严重度"] == "中")
    check("⑧ 转条目：来源Issue 落上", item.get("来源Issue") == 7)
    check("⑨ 转条目：状态留给 do_add 兜底", "状态" not in item)
    unknown = to_issue({"字段标签": "某某*", "系统": "不知道",
                        "期望键": "", "实际结果": "未命中"})
    check("⑩ 不知道系统 → 站点通配 *", unknown["站点"] == ["*"])
    check("⑪ 条目过 guard（含禁用键检查）", reg.guard(json.dumps(item, ensure_ascii=False), item)[0])

    print("\n自测结果：%d 通过 / %d 失败" % (ok[0], bad[0]))
    return 0 if bad[0] == 0 else 1


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="反馈包生成：把遇到的填表问题整理成可提交的 Issue",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python scripts/report_feedback.py\n"
               "  python scripts/report_feedback.py --save\n")
    ap.add_argument("--field", default="", help="页面字段标签（含星号）")
    ap.add_argument("--system", default="", help="页面所在系统")
    ap.add_argument("--key", default="", help="期望映射到的档案键")
    ap.add_argument("--result", default="", help="实际结果")
    ap.add_argument("--html", default="", help="表单控件的 HTML（可选）")
    ap.add_argument("--note", default="", help="补充说明（可选）")
    ap.add_argument("--from-report", default="", help="从填表报告 JSON 取未命中字段")
    ap.add_argument("--save", action="store_true", help="另存一份 JSON 到本机")
    ap.add_argument("--out", default="", help="JSON 输出路径（配合 --save）")
    ap.add_argument("--selftest", action="store_true", help="自测")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()

    label = args.field
    if not label and args.from_report:
        label = pick_from_report(args.from_report) or ""

    # 给了 --field 就算「非交互」：脚本调用不该停在任何一个问题前等输入。
    # 缺的项一律用安全默认值，**不去问** —— 问就会卡在管道上。
    interactive = not label

    if not label:
        print("=" * 64)
        print("反馈包生成 · 把填不上的框记下来，下一个人就不用再踩")
        print("=" * 64)
        label = _ask("1) 页面上那个填不上的框，叫什么？（连星号一起抄）")

    if not str(label).strip():
        print("[ERR] 字段标签不能空 —— 没有它，这条反馈对别人没有用。")
        return 2

    if interactive:
        sysname = args.system or _ask("2) 这个页面是哪个系统？",
                                      default="不知道", choices=SYSTEMS)
        key = args.key or _ask("3) 你觉得它该对应你档案里的哪一项？",
                               default="不确定", allow_empty=True)
        res = args.result or _ask("4) 发生了什么？", default="未命中", choices=RESULTS)
        html = args.html or _ask("5) （可选）那个框的 HTML，右键→检查→复制外层标签",
                                 allow_empty=True)
    else:
        sysname = args.system or "不知道"
        key = args.key or "不确定"
        res = args.result or "未命中"
        html = args.html
    note = args.note

    payload = {"字段标签": label, "系统": sysname, "期望键": key,
               "实际结果": res, "控件HTML": html, "补充说明": note}

    text = render(payload)
    saved = ""

    if args.save or args.out:
        out = args.out or os.path.join(
            OUT_DIR, "反馈_%s.json" % datetime.datetime.now().strftime("%Y%m%d-%H%M"))
        # 硬门：先反证，再落盘。命中即中止，一个字节都不写。
        ok, msgs = reg.guard(text, payload)
        if not ok:
            print("\n[ERR] 隐私反证失败 —— **已中止，未写盘**：")
            for k, v in msgs[:10]:
                print("   - %s：%r" % (k, v))
            return 3
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        with open(out, "w", encoding="utf-8", newline="") as fh:
            json.dump({"反馈": payload, "问题条目": to_issue(payload),
                       "生成时间": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")},
                      fh, ensure_ascii=False, indent=2)
        saved = out

    # 硬门同样管住「打印」：命中就什么都不显示。
    ok, msgs = reg.guard(text)
    if not ok:
        print("\n[ERR] 隐私反证失败 —— **已中止，未输出**：")
        for k, v in msgs[:10]:
            print("   - %s：%r" % (k, v))
        return 3

    print("\n" + "=" * 64)
    print("复制下面这一段（到分隔线为止），粘贴到 Issue 里：")
    print("=" * 64 + "\n")
    print(text)
    print("=" * 64)
    print("提交地址：%s" % ISSUE_NEW)
    print("（需要 GitHub 账号。没有账号的话，把上面这段发给项目作者也行。）")
    if saved:
        print("本机另存了一份：%s" % os.path.basename(saved))
    return 0


if __name__ == "__main__":
    sys.exit(main())
