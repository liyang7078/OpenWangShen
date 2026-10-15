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
from urllib.parse import quote
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core                # noqa: E402
import issue_registry as reg  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT_DIR = os.path.join(ROOT, "feedback_out")

REPO = "liyang7078/OpenWangShen"
ISSUE_NEW = "https://github.com/%s/issues/new" % REPO
CHANNEL_FILE = os.path.join(ROOT, "assets", "反馈渠道.json")

# ---- 采样选项集（**唯一来源**）------------------------------------------------
# `gen_issue_template.py` 直接 import 这些常量去生成 .github 的 Issue 模板，
# 所以模板与脚本**不可能漂移**（P1-8：以前是两处各写一遍，改一处就分叉）。
#
# 关于「其他」：它曾经是「页面所在系统」的一个选项，结果是**维度被污染** ——
# 几十条反馈全落进「其他」，等于没分类（P1-6）。现在改成：
# 内置名单 + 从问题库/策略库现算的已知站点 + **允许自由输入**，
# 不再提供「其他」这个垃圾桶。
BUILTIN_SYSTEMS = ("北森", "Moka", "大易", "智联", "前程无忧", "猎聘", "牛客")
UNKNOWN_SYSTEM = "不知道"

RESULTS = ("未命中", "映射到了错误的键", "选项对不上",
           "控件写不进去", "页面打不开", "其他")
# 「实际结果」→「缺口类别」：前者是现象，后者是**归因**，后者才排得上工程优先级
GAP_CHOICES = ("标签取不到", "控件不可见或被隐藏", "下拉选不中",
               "自绘控件写不进去（日历/级联）", "表单在 iframe / 弹层里",
               "被验证码等人工门挡住", "页面结构变了", "不确定")
STACK_CHOICES = ("不确定", "Vue", "React", "jQuery", "原生表单", "其他框架")
CONTROL_CHOICES = ("文本框", "多行文本", "下拉框", "单选", "复选",
                   "日期", "地区级联", "上传", "不确定")


def system_choices():
    """
    已知系统名 = 内置名单 ＋ 问题库/策略库里出现过的站点，去重后排序。
    用户可以**自由输入**名单外的名字（名单只是提示，不是白名单）。
    """
    names = set(BUILTIN_SYSTEMS)
    try:
        import issue_registry as _reg
        items, _ = _reg.load_issues()
        for x in items:
            for s in (x.get("站点") or []):
                if s and s not in ("*", UNKNOWN_SYSTEM):
                    names.add(str(s))
        for s in (_reg.load_strategies().get("策略") or []):
            for st in (s.get("站点") or []):
                if st and st != "*":
                    names.add(str(st))
    except Exception:                                        # noqa: BLE001
        pass
    return tuple(sorted(names)) + (UNKNOWN_SYSTEM,)


def feedback_email():
    """
    读 assets/反馈渠道.json 里的「反馈邮箱」。
    没配就返回空 —— **不编造地址**；此时只保留 GitHub Issue 通道。
    """
    try:
        with open(CHANNEL_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return str(d.get("反馈邮箱") or "").strip()
    except (OSError, ValueError):
        return ""


def site_of(url):
    """
    从页面地址里只取**域名**（去掉路径与查询串）。
    为什么要这一条：站点域名是排优先级最有用的一列 ——
    「同一个域名下 5 条反馈」比「5 条孤立的反馈」好处理得多。
    只保留域名也顺带避免了把带参数的 URL（可能含个人信息）寄出去。
    """
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        from urllib.parse import urlsplit
        h = (urlsplit(raw).hostname or "").lower()
        return h[4:] if h.startswith("www.") else h
    except Exception:                                        # noqa: BLE001
        return ""

# 结果 → 严重度（预判，最终由作者在试验阶段复核）
SEVERITY = {
    "未命中": "中",
    "映射到了错误的键": "高",
    "选项对不上": "中",
    "控件写不进去": "高",
    "页面打不开": "高",
    "其他": "低",
}

# 「缺口类别」也能影响严重度：整组字段都填不了的，比单个字段没认出来的更急
GAP_SEVERITY = {
    "表单在 iframe / 弹层里": "高",
    "被验证码等人工门挡住": "中",
    "页面结构变了": "高",
    "自绘控件写不进去（日历/级联）": "中",
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
    """
    渲染成 Issue 正文。结构与 `.github/ISSUE_TEMPLATE/字段识别失败.md` **逐项对齐**，
    且那份模板由 `scripts/gen_issue_template.py` 从本文件导入的选项集生成 ——
    两侧共用同一个来源，不会漂移（P1-8）。

    表的**行数固定**（缺的填「不确定」/「—」），这样作者侧解析不用猜格式。
    """
    p = payload
    L = [
        "## 字段信息", "",
        "| 项 | 内容 |", "|---|---|",
        "| 页面字段标签（含星号） | `%s` |" % p.get("字段标签", ""),
        "| 页面所在系统 | %s |" % (p.get("系统") or UNKNOWN_SYSTEM),
        "| 站点域名 | %s |" % (p.get("站点域名") or "—"),
        "| 页面技术栈 | %s |" % (p.get("技术栈") or "不确定"),
        "| 控件类型 | %s |" % (p.get("控件类型") or "不确定"),
        "| 期望映射到哪个键 | `%s` |" % (p.get("期望键") or "不确定"),
        "| 实际结果 | %s |" % p.get("实际结果", ""),
        "| 缺口类别 | %s |" % (p.get("缺口类别") or "不确定"),
        "",
    ]
    if p.get("控件HTML"):
        L += ["## 表单控件的 HTML", "", "```html", p["控件HTML"].strip(), "```", ""]
    if p.get("补充说明"):
        L += ["## 补充说明", "", p["补充说明"].strip(), ""]
    L += ["## 环境", ""] + env_lines() + [""]
    return "\n".join(L)


def to_issue(payload, source=""):
    """转成问题库条目（`inbox --accept` 用同一套映射，保证两侧一致）。"""
    lab = payload.get("字段标签", "")
    res = payload.get("实际结果", "")
    sysname = payload.get("系统", "") or UNKNOWN_SYSTEM
    gap = payload.get("缺口类别", "") or ""
    sev = GAP_SEVERITY.get(gap) or SEVERITY.get(res, "中")
    item = {
        "现象": "字段「%s」%s" % (lab, res),
        "站点": [sysname] if sysname != UNKNOWN_SYSTEM else ["*"],
        "指纹": [],                      # 考生不懂指纹，留空；试验阶段由作者补
        "影响": "该字段需手工填写",
        "严重度": sev,
        "路径": [],
        "来源": "考生反馈",
        "缺口类别": gap or "不确定",
    }
    if source:
        item["来源Issue"] = source
    return item


# --------------------------------------------------------------------------
# 交互
# --------------------------------------------------------------------------
def _ask(prompt, default="", choices=None, allow_empty=False, free=False):
    """
    问一个问题。

    `free=True`：给的是**提示名单**而不是白名单 —— 用户输入名单外的值照样接受。
    这正是 P1-6 的修法：以前只能从固定选项里挑，结果「其他」变成垃圾桶；
    现在名单只负责提示，命名权还给用户（名单外的新系统名会被收进已知名单）。
    """
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
        if choices and s not in choices and not free:
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
    check("① 渲染出字段表（含新采样列）",
          all(k in txt for k in ("| 页面字段标签（含星号） | `联系人手机*` |",
                                 "| 站点域名 |", "| 页面技术栈 |",
                                 "| 控件类型 |", "| 缺口类别 |")))
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

    # ---- 采样字段扩展（P1-5）----
    rich = {"字段标签": "籍贯*", "系统": "自研站", "期望键": "籍贯",
            "实际结果": "控件写不进去", "缺口类别": "自绘控件写不进去（日历/级联）",
            "站点域名": "job.example.org", "技术栈": "Vue", "控件类型": "地区级联"}
    rt = render(rich)
    check("⑫ 新采样列写进正文",
          "| 站点域名 | job.example.org |" in rt and "| 控件类型 | 地区级联 |" in rt)
    check("⑬ 域名只取主机名（不带路径）",
          site_of("https://job.example.org/apply?id=123#x") == "job.example.org")
    check("⑭ 无地址时域名留空不猜", site_of("") == "" and site_of("不是网址") == "")
    rich_item = to_issue(rich)
    check("⑮ 缺口类别进条目且抬升严重度",
          rich_item["缺口类别"] == "自绘控件写不进去（日历/级联）"
          and rich_item["严重度"] == "中")
    check("⑯ 写不进去类结果判为高严重度",
          to_issue({"字段标签": "x*", "实际结果": "控件写不进去"})["严重度"] == "高")

    # ---- 站点枚举开放化（P1-6）----
    ch = system_choices()
    check("⑰ 站点名单不含「其他」垃圾桶", "其他" not in ch, ch[:6])
    check("⑱ 名单含内置常见 ATS 与「不知道」",
          "北森" in ch and UNKNOWN_SYSTEM in ch)

    # 自由输入是真行为，不是文档承诺 —— 直接把 stdin 换掉跑一遍
    import builtins
    _orig_input = builtins.input
    try:
        builtins.input = lambda *a, **k: "我家自研站"
        got_free = _ask("?", choices=("北森",), free=True)
        builtins.input = lambda *a, **k: "北森"
        got_listed = _ask("?", choices=("北森",), free=True)
    finally:
        builtins.input = _orig_input
    check("⑲ 自由输入：名单外也接受，名单内照收",
          got_free == "我家自研站" and got_listed == "北森",
          (got_free, got_listed))

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
    ap.add_argument("--system", default="", help="页面所在系统（可自由填，不限名单）")
    ap.add_argument("--url", default="", help="出问题的那个页面地址（会只取域名）")
    ap.add_argument("--stack", default="", help="页面技术栈（Vue/React/jQuery…，可跳过）")
    ap.add_argument("--control", default="", help="控件类型（文本框/下拉/日期…，可跳过）")
    ap.add_argument("--gap", default="", help="缺口类别（标签取不到/控件被隐藏…，可跳过）")
    ap.add_argument("--key", default="", help="期望映射到的档案键")
    ap.add_argument("--result", default="", help="实际结果")
    ap.add_argument("--html", default="", help="表单控件的 HTML（可选）")
    ap.add_argument("--note", default="", help="补充说明（可选）")
    ap.add_argument("--from-report", default="", help="从填表报告 JSON 取未命中字段")
    ap.add_argument("--save", action="store_true", help="另存一份 JSON 到本机")
    ap.add_argument("--out", default="", help="JSON 输出路径（配合 --save）")
    ap.add_argument("--overwrite", action="store_true",
                    help="允许覆盖同名 JSON（默认改存带时间戳的新名）")
    ap.add_argument("--mailto", action="store_true",
                    help="额外给一条「一键邮件」链接（零 GitHub 账号通道）")
    ap.add_argument("--to", default="", help="覆盖反馈邮箱（默认读 assets/反馈渠道.json）")
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
        # 前 4 问是核心（缺了这条反馈对别人没用）；后 4 问可跳过。
        # 名单只作提示、可自由输入（P1-6）。
        sysname = args.system or _ask("2) 这个页面是哪个系统？（认不出就写「不知道」，"
                                      "也可直接打名字）",
                                      default=UNKNOWN_SYSTEM,
                                      choices=system_choices(), free=True)
        url = args.url or _ask("3) （可跳过）那个页面的地址，回车跳过", allow_empty=True)
        stack = args.stack or _ask("4) （可跳过）页面技术栈 —— 不确定就回车",
                                   choices=STACK_CHOICES, allow_empty=True)
        control = args.control or _ask("5) （可跳过）是哪种控件？回车跳过",
                                       choices=CONTROL_CHOICES, allow_empty=True)
        key = args.key or _ask("6) （可跳过）你觉得它该对应你档案里的哪一项？",
                               default="不确定", allow_empty=True)
        res = args.result or _ask("7) 发生了什么？", default="未命中", choices=RESULTS)
        gap = args.gap or _ask("8) （可跳过）你觉得是哪一类问题？",
                               choices=GAP_CHOICES, allow_empty=True)
        html = args.html or _ask("9) （可选）那个框的 HTML，右键→检查→复制外层标签",
                                 allow_empty=True)
    else:
        sysname = args.system or UNKNOWN_SYSTEM
        url = args.url
        stack = args.stack
        control = args.control
        key = args.key or "不确定"
        res = args.result or "未命中"
        gap = args.gap
        html = args.html
    note = args.note

    payload = {"字段标签": label, "系统": sysname, "期望键": key,
               "实际结果": res, "控件HTML": html, "补充说明": note,
               "站点域名": site_of(url), "技术栈": stack,
               "控件类型": control, "缺口类别": gap}

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
                print("   - %s：%s" % (k, v))          # v 已脱敏，不回显原值
            return 3
        # 统一 safe_write：默认不覆盖（改存带时间戳），原子替换。
        # 显式 --out 且加 --overwrite 才覆盖同名文件。
        saved = core.safe_write(
            out,
            json.dumps({"反馈": payload, "问题条目": to_issue(payload),
                        "生成时间": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")},
                       ensure_ascii=False, indent=2),
            overwrite=bool(args.out and args.overwrite),
            root=ROOT if not args.out else None)

    # 硬门同样管住「打印」：命中就什么都不显示。
    ok, msgs = reg.guard(text)
    if not ok:
        print("\n[ERR] 隐私反证失败 —— **已中止，未输出**：")
        for k, v in msgs[:10]:
            print("   - %s：%s" % (k, v))          # 已是脱敏样本，不回显原值
        return 3

    print("\n" + "=" * 64)
    print("复制下面这一段（到分隔线为止），粘贴到 Issue 里：")
    print("=" * 64 + "\n")
    print(text)
    print("=" * 64)
    print("提交地址：%s" % ISSUE_NEW)
    print("（这条要 GitHub 账号。）")

    # ---- 零账号通道：一键邮件 ----
    addr = (args.to or feedback_email()).strip()
    if args.mailto or addr:
        if addr:
            subj = "[字段] %s 未被识别" % (payload.get("字段标签") or "未知字段")
            if payload.get("缺口类别"):
                subj += "（%s）" % payload["缺口类别"]
            url = "mailto:%s?subject=%s&body=%s" % (
                addr, quote(subj), quote(text))
            print("\n没有 GitHub 账号？用这条 —— 会打开你的邮件客户端，正文已填好：")
            print(url[:200] + ("…" if len(url) > 200 else ""))
            if args.mailto:
                print("（把上面整行复制到浏览器地址栏打开即可）")
        else:
            print("\n[提示] 本项目尚未配置反馈邮箱，零账号通道暂时不可用。")
            print("       作者可在 assets/反馈渠道.json 填入「反馈邮箱」后自动启用。")
            print("       在此之前，可把上面那段文字直接发给项目作者。")
    if saved:
        print("本机另存了一份：%s" % saved)
    return 0


if __name__ == "__main__":
    sys.exit(main())
