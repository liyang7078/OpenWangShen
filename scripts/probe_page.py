#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probe_page.py · 只读页面探针：看清这一页到底有什么
==================================================

什么时候用它
------------
`fill` 跑不动的时候 —— **别猜，先看清楚**。它回答三个问题：

  1. 这一页到底有多少个输入控件？都是什么类型？
  2. 哪些控件**看得见**、哪些是隐藏的？
  3. 有没有**自绘控件**（日历 / 级联 / 下拉弹层）—— 这类填不上不是你的错，
     是它的形态特殊：值根本不是写在普通输入框里的。

它补的是降级链缺的那一环：

    fill 跑不动 → **探针：看清这页有什么** → report 生成可提交的 Issue
      → 作者收反馈 → 反哺字段规则库

以前这条链中间是断的：只能从「填不上」直接跳到「交清单」，中间**为什么填不上**
永远说不清，于是同一类站点每个人都要重新踩一遍。

三条约束（写死在这里）
----------------------
1. **只读**：只做「查询 DOM」，不点、不填、不删。本文件里没有任何写入调用
   （自测有一条断言盯着这件事）。
2. **零真值**：不接受档案参数 —— 从**输入**上杜绝「把值带出来」。
3. **产物可外发**：输出的是**页面结构**（控件类型、标签文字、可见性）。
   标签文字取自网页本身，不是你的数据 —— 所以这段诊断可以直接贴进 Issue。

用法：
    python scripts/probe_page.py --connect 9222      # 接管已登录的浏览器（推荐）
    python scripts/probe_page.py --url <页面地址>    # 自己开一个（未登录站会停在登录页）
    python scripts/probe_page.py --selftest

许可：MIT
"""

import argparse
import datetime
import io
import json
import os
import sys
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fill_form as ff                                     # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ================================================================== 纯函数：人话诊断
def diagnose(fields, fps=None):
    """把扫描结果翻译成**人话建议**。纯函数 —— 自测直接喂字段列表，不开浏览器。

    每一条都指向**一个具体动作**，不是「可能是网络问题」这类没法执行的话。
    """
    out = []
    total = len(fields or [])
    if total == 0:
        out.append("扫到 **0 个输入控件**。最常见的原因是**页面不对**：停在登录页 / 首页 / "
                   "岗位列表页上，结果都会是 0。请确认已经走到「填写简历」那一页。")
        return out

    vis = [f for f in (fields or []) if f.get("visible")]
    if len(vis) < total:
        out.append("有 **%d 个控件当前不可见**。这类多半藏在「＋ 添加」型的重复模块里 —— "
                   "先跑一次 `--expand`（填表时的参数）把模块点开，能多看到一批。"
                   % (total - len(vis)))

    panel = [f for f in (fields or []) if f.get("panel")]
    if panel:
        out.append("有 **%d 个自绘控件**（日历 / 级联 / 下拉弹层）。普通写入对它们会"
                   "「**假成功**」——值看着写进去了，页面其实没接住（DOM 的 value 根本没动，"
                   "组件把值渲染在自己的面板里）。这类要走「点开面板 → 点中那一项」。" % len(panel))

    ro = [f for f in (fields or []) if f.get("readonly")]
    if ro:
        out.append("有 **%d 个只读框**。只读框通常就是自绘控件（值由页面自己渲染），"
                   "和上一条是同一个坑。" % len(ro))

    nolabel = [f for f in (fields or []) if not (f.get("label") or "").strip()]
    if nolabel:
        out.append("有 **%d 个控件没有可见标签**。没有标签就无法靠文字识别，"
                   "它们会进「规则库未命中」清单 —— 需要人工看一眼是什么。" % len(nolabel))

    frames = {f.get("frame") for f in (fields or []) if f.get("frame") not in (None, "main")}
    if frames:
        out.append("控件分布在 **%d 个 iframe** 里 —— 有些站把表单放在框架内，"
                   "这是正常结构，不必改。" % len(frames))

    if fps:
        out.append("命中的结构指纹：%s。" % "、".join(sorted(fps)))

    if not out:
        out.append("页面结构正常：控件都可见、没发现自绘控件、都有标签。"
                   "若仍然填不上，多半是**字段名对不上**（页面上的标签在规则库里没有条目）——"
                   "把标签原文补进 `assets/字段规则_通用.json` 即可。")
    return out


def build_issue_text(doc):
    """生成一段**可直接贴进 Issue** 的诊断文本。纯函数。

    刻意**不含任何个人值**：只有控件类型、标签原文、可见性这些页面自身的信息。
    """
    s = doc.get("汇总") or {}
    L = ["### 页面探测结果（probe_page.py 自动生成）", ""]
    L.append("| 项 | 值 |")
    L.append("|---|---|")
    L.append("| 页面标题 | %s |" % (doc.get("页面", {}).get("标题") or "(空)"))
    L.append("| 控件总数 | %s |" % s.get("控件总数"))
    L.append("| 其中可见 | %s |" % s.get("可见"))
    L.append("| 自绘控件 | %s |" % s.get("自绘"))
    L.append("| 无标签 | %s |" % s.get("无标签"))
    L.append("| iframe 数 | %s |" % s.get("iframe"))
    L.append("| 探测时间 | %s |" % doc.get("页面", {}).get("探测时间"))
    L.append("")
    if doc.get("指纹"):
        L.append("指纹：%s" % "、".join(doc["指纹"]))
        L.append("")
    if doc.get("分类型"):
        L.append("控件类型分布：%s"
                 % "、".join("%s×%d" % (k, v) for k, v in sorted(doc["分类型"].items())))
        L.append("")
    L.append("**诊断：**")
    L.append("")
    for d in doc.get("诊断") or []:
        L.append("- %s" % d)
    L.append("")
    L.append("**控件清单（标签原文取自网页，不含个人信息）：**")
    L.append("")
    L.append("| # | 标签 | 类型 | 可见 | 自绘 | 只读 | 选项数 |")
    L.append("|---|---|---|---|---|---|---|")
    for c in doc.get("控件") or []:
        L.append("| %d | %s | %s | %s | %s | %s | %s |"
                 % (c["序"], c["标签"] or "（无）", c["类型"],
                    "是" if c["可见"] else "否", "是" if c["自绘"] else "否",
                    "是" if c["只读"] else "否", c["选项数"]))
    L.append("")
    return "\n".join(L)


# ================================================================== 采集（只读）
def _pick(ctx, url=""):
    """在有多个标签页时挑一个：优先 URL 主机名匹配的，否则第一个。"""
    pages = [p for p in ctx.pages if not p.is_closed()]
    if not pages:
        return ctx.new_page()
    if url:
        host = urlparse(url).netloc
        if host:
            for pg in pages:
                try:
                    if host in (pg.url or ""):
                        return pg
                except Exception:                            # noqa: BLE001
                    pass
    return pages[0]


def collect(page, url=""):
    """**只读**采集页面结构。返回一个可直接外发的 dict。"""
    fields, _frames = ff.scan_page(page)                     # 只读：scan_page 只查询
    fps, _why = ff.derive_fingerprints(fields, url=url)

    typed = {}
    for f in fields:
        t = f.get("type") or f.get("tag") or "?"
        typed[t] = typed.get(t, 0) + 1

    rows = []
    for i, f in enumerate(fields, 1):
        rows.append({
            "序": i,
            "标签": (f.get("label") or "").strip(),
            "类型": f.get("type") or f.get("tag") or "?",
            "可见": bool(f.get("visible")),
            "自绘": bool(f.get("panel")),
            "只读": bool(f.get("readonly")),
            "选项数": len(f.get("options") or []),
        })

    try:
        title = page.title()
    except Exception:                                        # noqa: BLE001
        title = ""
    try:
        addr = page.url or ""
    except Exception:                                        # noqa: BLE001
        addr = ""

    return {
        "页面": {
            "标题": title,
            # 只留主机名 + 路径，**丢掉查询串** —— 它可能带会话标识
            "地址": _safe_url(addr or url),
            "探测时间": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
        "汇总": {
            "控件总数": len(fields),
            "可见": len([f for f in fields if f.get("visible")]),
            "自绘": len([f for f in fields if f.get("panel")]),
            "无标签": len([f for f in fields if not (f.get("label") or "").strip()]),
            "iframe": len({f.get("frame") for f in fields
                           if f.get("frame") not in (None, "main")}),
        },
        "分类型": typed,
        "指纹": sorted(fps or []),
        "诊断": diagnose(fields, fps),
        "控件": rows,
    }


def _safe_url(u):
    """地址去掉查询串与片段 —— 查询串里可能带会话 token，不该进可外发产物。"""
    try:
        p = urlparse(u or "")
        if not p.scheme:
            return ""
        return "%s://%s%s" % (p.scheme, p.netloc, p.path)
    except Exception:                                        # noqa: BLE001
        return ""


# ================================================================== 主流程
def main():
    ap = argparse.ArgumentParser(
        description="只读页面探针：看清这一页有哪些控件（不点、不填、不删）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="典型用法：\n"
               "  # 接管你已登录的浏览器（推荐 —— 未登录的页面探不到东西）\n"
               "  python scripts/probe_page.py --connect 9222\n"
               "  # 产物落到文件，并把一段可贴 Issue 的文字打到屏幕\n"
               "  python scripts/probe_page.py --connect 9222 --out 探测.json --issue\n")
    ap.add_argument("--connect", default="", help="接管地址/端口（如 9222 或 http://127.0.0.1:9222）")
    ap.add_argument("--url", default="", help="直接打开这个地址探测（未登录站会停在登录页）")
    ap.add_argument("--out", default="", help="探测结果 JSON 输出路径")
    ap.add_argument("--issue", action="store_true", help="额外打印一段可直接贴进 Issue 的文字")
    ap.add_argument("--timeout", type=int, default=45000, help="单步超时毫秒")
    ap.add_argument("--selftest", action="store_true")
    return _main(ap)


def _main(ap):
    args = ap.parse_args()
    if args.selftest:
        return selftest()

    try:
        from playwright.sync_api import sync_playwright
    except Exception:                                        # noqa: BLE001
        print("[ERR] 没装 playwright。装法：pip install playwright")
        print("      探针需要浏览器才能看页面结构。")
        return 4

    if not args.connect and not args.url:
        print("[ERR] 至少给一个：--connect <端口>（推荐，复用登录态）或 --url <地址>")
        return 2

    with sync_playwright() as p:
        if args.connect:
            cdp = ff.normalize_cdp(args.connect)
            try:
                b = p.chromium.connect_over_cdp(cdp)
            except Exception as e:                           # noqa: BLE001
                print("[ERR] 连不上浏览器（%s）：%s" % (cdp, str(e)[:120]))
                print("      先跑：python scripts/start_browser.py")
                return 3
            ctx = b.contexts[0] if b.contexts else b.new_context()
            page = _pick(ctx, args.url)
            if not page.url or page.url == "about:blank":
                if args.url:
                    page.goto(args.url, timeout=args.timeout)
                    page.wait_for_timeout(1500)
        else:
            b = p.chromium.launch(headless=True)
            ctx = b.new_context()
            page = ctx.new_page()
            try:
                page.goto(args.url, timeout=args.timeout)
                page.wait_for_timeout(2500)
            except Exception as e:                           # noqa: BLE001
                print("[ERR] 打不开 %s：%s" % (args.url, str(e)[:120]))
                return 3

        print("[探测] %s" % (page.url or args.url))
        doc = collect(page, url=args.url)

    s = doc["汇总"]
    print("[结果] 控件 %d（可见 %d ／ 自绘 %d ／ 无标签 %d ／ iframe %d）"
          % (s["控件总数"], s["可见"], s["自绘"], s["无标签"], s["iframe"]))
    print()
    for d in doc["诊断"]:
        print("  · %s" % d)
    print()

    if args.issue:
        print("=" * 56)
        print(build_issue_text(doc))
        print("=" * 56)

    if args.out:
        if os.path.exists(args.out):
            print("[WARN] 同名产物已存在，**不覆盖**：%s" % args.out)
            print("       换个 --out 路径。")
            return 3
        with io.open(args.out, "w", encoding="utf-8", newline="") as fh:
            fh.write(json.dumps(doc, ensure_ascii=False, indent=2))
        print("[产物] %s" % args.out)

    # 0 = 探到了控件；1 = 一个都没探到（页面多半不对，值得按「问题」对待）
    return 0 if s["控件总数"] else 1


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

    def mk(label="姓名", typ="text", visible=True, panel=False, readonly=False,
           opts=None, frame="main"):
        return {"label": label, "type": typ, "visible": visible, "panel": panel,
                "readonly": readonly, "options": opts or [], "frame": frame,
                "tag": "input", "idx": "0"}

    # ---- 诊断：每条都要指向一个具体动作，不能是废话 ----
    d0 = diagnose([])
    check("① 0 控件 → 指向「页面不对」（登录页/列表页）",
          len(d0) == 1 and "0 个输入控件" in d0[0] and "登录页" in d0[0], d0)

    d1 = diagnose([mk()])
    check("② 页面正常 → 给出「字段名对不上」这条出路，而不是干说「没问题」",
          any("字段名对不上" in x for x in d1), d1)

    d2 = diagnose([mk(visible=False)])
    check("③ 有不可见控件 → 指向 `--expand` 这个具体动作",
          any("--expand" in x for x in d2), d2)

    d3 = diagnose([mk(panel=True)])
    check("④ 有自绘控件 → 点明「假成功」并给出「点开面板」的解法",
          any("假成功" in x and "点开面板" in x for x in d3), d3)

    d4 = diagnose([mk(readonly=True)])
    check("⑤ 只读框 → 与自绘控件关联起来（同一个坑）",
          any("只读" in x for x in d4), d4)

    d5 = diagnose([mk(label=""), mk(label="")])
    check("⑥ 无标签控件 → 说明后果（会进未命中清单）",
          any("没有可见标签" in x for x in d5), d5)

    d6 = diagnose([mk(frame="f1_")])
    check("⑦ iframe → 明确说是正常结构，别让人去乱改",
          any("iframe" in x and "正常" in x for x in d6), d6)

    d7 = diagnose([mk()], fps={"readonly-cascader"})
    check("⑧ 有指纹时把指纹报出来",
          any("readonly-cascader" in x for x in d7), d7)

    # ---- Issue 文本：必须可外发（只有结构，没有值）----
    doc = {
        "页面": {"标题": "某某公司校园招聘 - 简历填写", "地址": "https://x/y", "探测时间": "T"},
        "汇总": {"控件总数": 2, "可见": 1, "自绘": 1, "无标签": 0, "iframe": 0},
        "分类型": {"text": 1, "date": 1}, "指纹": ["readonly-cascader"],
        "诊断": ["A", "B"],
        "控件": [{"序": 1, "标签": "姓名", "类型": "text", "可见": True, "自绘": False,
                  "只读": False, "选项数": 0},
                 {"序": 2, "标签": "出生日期", "类型": "date", "可见": True, "自绘": True,
                  "只读": True, "选项数": 0}],
    }
    it = build_issue_text(doc)
    check("⑨ Issue 文本含汇总与诊断", "控件总数" in it and "A" in it and "B" in it)
    check("⑩ Issue 文本含控件清单（标签原文）", "出生日期" in it and "readonly-cascader" in it)

    # ---- 地址脱敏（查询串可能带会话 token，不许进产物）----
    check("⑪ 地址丢掉查询串与片段",
          _safe_url("https://a.b/c?token=secret#frag") == "https://a.b/c",
          _safe_url("https://a.b/c?token=secret#frag"))
    check("⑫ 地址无 scheme 时返回空（不硬编一个）", _safe_url("not a url") == "")

    # ---- ⭐ 只读底线：代码里不许出现任何写入 / 点击调用 ----
    #   这是本文件名副其实的执行点：只要有人往这里加一句 frame.fill，它立刻红。
    #
    #   为什么用 AST 而不是字符串匹配：字符串匹配会被两件事骗到 ——
    #     · **自指**：检查清单里的字面量本身就含那些词，于是「有」永远成立（假红）；
    #     · 注释/文档里的同名文字（假红）。
    #   AST 只看**真的调用了什么**，这些假象一个都不成立。
    import ast
    src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
    code_only = src.split("def selftest()")[0]        # 排除自测自身（它必然含这些名字）
    calls = set()
    for node in ast.walk(ast.parse(code_only)):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Attribute):
                calls.add(fn.attr)
            elif isinstance(fn, ast.Name):
                calls.add(fn.id)
    banned = {"fill", "select_option", "check", "set_input_files", "click",
              "apply_plan", "expand_blocks", "_force_fill", "_panel_fill", "press"}
    hit = sorted(calls & banned)
    check("⑬ **只读底线**：代码里没有任何写入 / 点击调用（AST 判据）", not hit, hit)

    # 反证：这条判据自己**抓得住**写入调用（否则它可能是个永远绿的摆设）
    fake = "def f(frame):\n    frame.fill('#a', 'x')\n"
    _fcalls = set()
    for node in ast.walk(ast.parse(fake)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            _fcalls.add(node.func.attr)
    check("⑬b **反证**：同样的判据喂一句 frame.fill 必须变红",
          bool(_fcalls & banned), sorted(_fcalls))

    check("⑭ 不接受档案参数（从**输入**上杜绝带出真值）",
          "--profile" not in code_only)

    print("\n自测结果：%d 通过 / %d 失败" % (ok[0], bad[0]))
    return 0 if bad[0] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
