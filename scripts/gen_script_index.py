#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_script_index.py · 由 CMDS 现算「脚本速查」表（--check 防漂移）
==================================================================

为什么要有它
------------
`SKILL.md` 里那张「脚本速查」表原来是**手写**的，于是它必然滞后 —— 实测：表里列了
**5 个**脚本，仓库里实际有 **20+ 个**。而文档少列一个脚本，读者不会报错，
他只会**以为那个能力不存在**，然后自己去想别的办法。这是最贵的一种文档错误：
它不报错，它只是让人错过。

所以这张表不该手写：从 `wangshen.py` 的 `CMDS` 字典**现算**，插进 SKILL.md 的标记区。
`--check` 只比对、不写入，作为一道门 —— 加了脚本却忘了更新表，它会红。

用法：
    python scripts/gen_script_index.py            # 写入（更新 SKILL.md 里的标记区）
    python scripts/gen_script_index.py --check    # 只检查，漂移则退出码 1

许可：MIT
"""

import argparse
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import wangshen as ws                                      # noqa: E402

DOC = os.path.join(ROOT, "SKILL.md")

BEGIN = "<!-- BEGIN: 脚本速查（由 scripts/gen_script_index.py 生成，勿手改） -->"
END = "<!-- END: 脚本速查 -->"

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def render(cmds):
    """把 CMDS 渲染成表。**纯函数** —— 自测直接喂一个假 CMDS，不必碰文件。"""
    rows = ["| 脚本 | 职责 |", "|---|---|"]
    rows.append("| `scripts/wangshen.py` | 统一入口（下面每个脚本都能通过它调用） |")
    for _name, (script, desc) in sorted(cmds.items(), key=lambda kv: kv[1][0]):
        rows.append("| `scripts/%s` | %s |" % (script, desc))
    return BEGIN + "\n" + "\n".join(rows) + "\n" + END


def splice(text, block):
    """把标记区**整体**换成 `block`。标记区不存在时返回 None（不猜、不追加）。"""
    b = text.find(BEGIN)
    if b < 0:
        return None
    e = text.find(END, b)
    if e < 0:
        return None
    return text[:b] + block + text[e + len(END):]


def main(argv=None):
    ap = argparse.ArgumentParser(description="由 CMDS 生成 SKILL.md 的脚本速查表（--check 防漂移）")
    ap.add_argument("--doc", default=DOC, help="目标文档（默认 SKILL.md）")
    ap.add_argument("--check", action="store_true", help="只检查，不写入（漂移则退出码 1）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    with io.open(args.doc, encoding="utf-8") as fh:
        text = fh.read()

    new = splice(text, render(ws.CMDS))
    if new is None:
        print("[ERR] %s 里找不到标记区：" % args.doc)
        print("      %s" % BEGIN)
        print("      %s" % END)
        return 2

    if args.check:
        if new == text:
            print("[OK] 脚本速查表与 CMDS 一致（%d 个脚本）" % len(ws.CMDS))
            return 0
        print("[FAIL] **漂移**：CMDS 变了但 %s 的速查表没跟上。" % os.path.basename(args.doc))
        print("       修法：python scripts/gen_script_index.py")
        return 1

    if new == text:
        print("[OK] 无变化（速查表已是最新，%d 个脚本）" % len(ws.CMDS))
        return 0

    with io.open(args.doc, "w", encoding="utf-8", newline="") as fh:
        fh.write(new)
    print("[写入] 已更新 %s 的速查表（%d 个脚本）" % (os.path.basename(args.doc), len(ws.CMDS)))
    return 0


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

    fake = {"alpha": ("a.py", "甲"), "beta": ("b.py", "乙")}
    t = render(fake)
    check("① 表里有全部的脚本", "`scripts/a.py`" in t and "`scripts/b.py`" in t, t)
    check("② 表里有职责说明", "甲" in t and "乙" in t)
    check("③ 统一入口单独一行（它不在 CMDS 里，别漏）", "`scripts/wangshen.py`" in t)
    check("④ 表被标记区包住（后续才能整体替换）", t.startswith(BEGIN) and t.endswith(END))

    # ---- splice：只动标记区，前后文一个字都不碰 ----
    doc = "前言\n" + render(fake) + "\n后记"
    fake2 = {"gamma": ("c.py", "丙")}
    out = splice(doc, render(fake2))
    check("⑤ splice 替换后：新内容进来了", "`scripts/c.py`" in out, out)
    check("⑥ splice 替换后：旧内容没了", "`scripts/a.py`" not in out, out)
    check("⑦ **前后文一字未动**", out.startswith("前言\n") and out.endswith("\n后记"), out)
    check("⑧ 标记区不存在 → 返回 None（不猜、不往文末乱追加）",
          splice("没有标记的文档", render(fake)) is None)

    # ---- 反证：CMDS 加一项，表必须跟着变（否则这判据抓不到漂移）----
    fake3 = dict(fake)
    fake3["delta"] = ("d.py", "丁")
    check("⑨ **反证**：CMDS 多一项 → 渲染结果必然不同（抓得住漂移）",
          render(fake) != render(fake3))

    # ---- 与真 CMDS 对得上 ----
    real = render(ws.CMDS)
    check("⑩ 真 CMDS 渲染出的表里，每个脚本都在",
          all(("`scripts/%s`" % s) in real for _n, (s, _d) in ws.CMDS.items()),
          len(ws.CMDS))
    check("⑪ 本次新加的三个脚本都在表里",
          all(("`scripts/%s`" % s) in real
              for s in ("apply_run.py", "split_records.py", "probe_page.py")))

    print("\n自测结果：%d 通过 / %d 失败" % (ok[0], bad[0]))
    return 0 if bad[0] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
