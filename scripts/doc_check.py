#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
doc_check.py · 文档数字一致性门
================================
盯一件事：**文档里写的数字，必须等于资产里现算出来的数字。**

为什么需要这道门
----------------
本项目因为「数字抄进文档、之后没人核对」翻过车：

  · README 承诺「下载最新版 → releases/latest」，而仓库一个 tag 都没有，
    那条链接是**空承诺**（见 `.git/_本地环境变更记录.md`）
  · 词表从 165 长到 187 时，`docs/快速开始.md` 的「其余选填 116」留在原地，
    而真值已经是 138
  · `examples/` 下两份示例产物**整份过期**：数字过时、`应聘岗位` 的必填标记
    变了、`证件照` 已改叫「照片」、③ 档少列 6 个字段。而 `examples/` 恰恰是
    别人了解「产物长什么样」的第一入口

根因不是「没人细心」，是**判据只写在文档里、没有执行点**。所以这里做两件事：

  ① **散文数字**：断言写成「文件 + 正则 + 事实键」，数字一律从资产现算再比对。
     写错 → 红；声明被整句删掉 → 也红（门不许静默失效）。
  ② **示例产物**：`examples/*.md` 是**生成物** —— 不看数字，直接**重生成再比对**。
     任何漂移（数字、措辞、结构）都会红，不需要人去维护一张数字清单。

用法
----
  python scripts/doc_check.py              # 核对；有漂移则退出码 1
  python scripts/doc_check.py --list       # 只打印事实表
  python scripts/doc_check.py --fix        # 用现算值改写散文数字 + 重生成示例产物
  python scripts/doc_check.py --selftest

许可：MIT
"""

import argparse
import datetime
import io
import json
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core            # noqa: E402
import make_checklist  # noqa: E402
import make_sheet      # noqa: E402

ROOT = core.PROJECT_ROOT

# ---------------------------------------------------------------------------
# 示例产物的固定口径
# ---------------------------------------------------------------------------
# 两份示例产物都带「生成时间」。校验要可复现，所以时间戳**写死在这里**，
# 而不是取 now()；改这里就等于改示例产物（`--fix` 会一起更新）。
#
# ⚠️ 这也意味着：示例产物里的生成时间**不是真实生成时刻**，是示例时间戳。
#    `examples/README.md` 里对此有说明，别当成 bug 去「修」。
SHEET_EXAMPLE = "examples/填写清单_示例.md"
SHEET_STAMP = "2026-09-20 23:38"
SHEET_CTX = {"now": SHEET_STAMP, "company": "示例集团",
             "job": "财务会计岗", "url": "", "resume": ""}

CHECKLIST_EXAMPLE = "examples/网申信息清单_示例.md"
CHECKLIST_STAMP = "2026-09-30"

REPORT_EXAMPLE = "examples/填表报告_示例.json"


# ---------------------------------------------------------------------------
# 事实：全部**现算**，一处来源 —— 文档只是它的一个视图
# ---------------------------------------------------------------------------
def count_test_methods():
    """数测试方法数（== `unittest` 报的「Ran N tests」）。

    用 AST 数，不去跑测试 —— 门本身要快，且不该因为某条用例失败而报成「数字不一致」。
    """
    import ast

    n = 0
    tdir = os.path.join(ROOT, "tests")
    if not os.path.isdir(tdir):
        return 0
    for fn in sorted(os.listdir(tdir)):
        if not fn.endswith(".py"):
            continue
        with io.open(os.path.join(tdir, fn), encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=fn)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test"):
                n += 1
    return n


def read_json(rel):
    try:
        with io.open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def facts():
    """算出全部事实。返回 {事实键: 整数}。"""
    fields = core.load_rules()
    attach = [f for f in fields if core.is_attachment(f["键"])]
    labels = sum(len(f.get("标签") or []) for f in fields)

    raw = read_json("assets/字段规则_通用.json") or {}
    domains = raw.get("值域别名") or {}

    groups = make_checklist.load_groups()
    n_rec = sum(len(g.get("字段") or []) for g in groups)

    n_issue = 0
    with io.open(os.path.join(ROOT, "assets", "问题库.jsonl"), encoding="utf-8") as fh:
        n_issue = sum(1 for ln in fh if ln.strip())

    strat = (read_json("assets/站点策略.json") or {}).get("策略") or []
    seq = strat if isinstance(strat, list) else list(strat.values())
    fps = set()
    for it in seq:
        for x in (it.get("指纹") or []):
            fps.add(x)

    bands = make_checklist.bucket(fields)
    rep = read_json(REPORT_EXAMPLE) or {}

    return {
        "字段总数": len(fields),
        "可填值字段": len(fields) - len(attach),
        "附件类": len(attach),
        "标签写法": labels,
        "值域别名组": len(domains),
        "记录组组数": len(groups),
        "记录组项数": n_rec,
        "问题库条数": n_issue,
        "站点策略条数": len(seq),
        "控件指纹数": len(fps),
        "必填档": len(bands[make_checklist.BAND_REQUIRED]),
        "常用档": len(bands[make_checklist.BAND_COMMON]),
        "其余选填档": len(bands[make_checklist.BAND_REST]),
        "单测方法数": count_test_methods(),
        "示例可自动填": rep.get("可自动填") or 0,
    }


# ---------------------------------------------------------------------------
# 声明表：文件 + 正则（捕获组顺序 = 事实键顺序）
# ---------------------------------------------------------------------------
# 一行一条。正则写得**够紧** —— 只咬住那句话，不咬整段，
# 这样 `--fix` 改写时不会伤到别的字。
CLAIMS = [
    # README.md
    ("README.md", r"字段知识库（(\d+) 字段 / (\d+) 标签 / (\d+) 组值域别名）",
     ("字段总数", "标签写法", "值域别名组")),
    ("README.md", r"本地规则库，(\d+) 字段 / (\d+) 标签）",
     ("字段总数", "标签写法")),
    ("README.md", r"记录组口径（(\d+) 组 / (\d+) 项",
     ("记录组组数", "记录组项数")),
    ("README.md", r"公开子集：(\d+) 条", ("问题库条数",)),
    ("README.md", r"公开子集\*\*（(\d+) 条）", ("问题库条数",)),
    ("README.md", r"（(\d+) 条策略 / (\d+) 个控件指纹）",
     ("站点策略条数", "控件指纹数")),
    ("README.md", r"单元测试（(\d+) 项", ("单测方法数",)),
    ("README.md", r"\*\*(\d+) 个字段自动填入", ("示例可自动填",)),

    # README.en.md
    ("README.en.md", r"\((\d+) fields / (\d+) labels\)",
     ("字段总数", "标签写法")),
    ("README.en.md", r"#\s*(\d+) unit tests", ("单测方法数",)),

    # docs/快速开始.md
    ("docs/快速开始.md", r"必填 (\d+) / 常用 (\d+) / 其余选填 (\d+)",
     ("必填档", "常用档", "其余选填档")),
    ("docs/快速开始.md", r"另加 (\d+) 组记录组", ("记录组组数",)),
    ("docs/快速开始.md", r"规则库当前覆盖 \*\*(\d+) 个字段\*\*", ("字段总数",)),
    ("docs/快速开始.md",
     r"（(\d+) 个可填值字段 \+ (\d+) 类附件）、\*\*(\d+) 种标签写法\*\*",
     ("可填值字段", "附件类", "标签写法")),

    # examples/README.md
    ("examples/README.md", r"① 必填（(\d+) 项）", ("必填档",)),
    ("examples/README.md", r"② 常用（(\d+) 项）", ("常用档",)),
    ("examples/README.md", r"③ 其余选填（(\d+) 项）", ("其余选填档",)),
    ("examples/README.md", r"④ 记录组（(\d+) 组 / (\d+) 项）",
     ("记录组组数", "记录组项数")),
]


def read_text(rel):
    try:
        with io.open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return None


def claims_in(text, pat, keys, fx):
    """纯函数：在一段文本里核对一条声明。

    返回 [(事实键, 文档写的, 实际)]。**没有命中 = 声明消失，也算失配** ——
    否则只要有人把那句话删掉，这道门就永久静默了。
    """
    hits = re.findall(pat, text)
    if not hits:
        return [("/".join(keys), None, "声明消失（正则未命中）")]
    out = []
    for hit in hits:
        vals = hit if isinstance(hit, tuple) else (hit,)
        for k, v in zip(keys, vals):
            if int(v) != int(fx[k]):
                out.append((k, int(v), fx[k]))
    return out


def check_docs(fx):
    """核对全部散文声明。返回 [(文件, 事实键, 文档写的, 实际)]。"""
    bad = []
    for rel, pat, keys in CLAIMS:
        text = read_text(rel)
        if text is None:
            bad.append((rel, "-", "文件读不到", "-"))
            continue
        for k, wrote, real in claims_in(text, pat, keys, fx):
            bad.append((rel, k, wrote, real))
    return bad


# ---------------------------------------------------------------------------
# 示例产物：生成物 → 重生成再比对
# ---------------------------------------------------------------------------
def regenerate_examples():
    """按当前资产重算示例产物。返回 [(相对路径, 内容)]。不做任何写入。"""
    out = []

    fields, domains = make_checklist.load_rules()
    groups = make_checklist.load_groups()
    out.append((CHECKLIST_EXAMPLE,
                make_checklist.render(fields, domains, groups,
                                      today=CHECKLIST_STAMP)))

    fx_dir = os.path.join(ROOT, "fixtures")
    fields2 = core.load_rules()
    profile = core.load_profile(os.path.join(fx_dir, "档案_示例.json"))
    with io.open(os.path.join(fx_dir, "页面字段_示例.txt"), encoding="utf-8") as fh:
        labels = [ln.strip() for ln in fh if ln.strip()]
    content = make_sheet.build(labels, fields2, profile, SHEET_CTX)[0]
    out.append((SHEET_EXAMPLE, content))

    return out


def first_diff(old, new):
    a, b = old.splitlines(), new.splitlines()
    for i in range(max(len(a), len(b))):
        x = a[i].strip() if i < len(a) else "（缺这一行）"
        y = b[i].strip() if i < len(b) else "（多这一行）"
        if x != y:
            return ("第 %d 行不一致 ｜ 已入库：%s ｜ 重生成：%s"
                    % (i + 1, x[:56], y[:56]))
    return "仅行尾空行不同"


def check_examples():
    """示例产物必须与生成器一致。返回 [(文件, 说明)]。"""
    bad = []
    for rel, content in regenerate_examples():
        old = read_text(rel)
        if old is None:
            bad.append((rel, "文件读不到"))
        elif old.rstrip("\n") != content.rstrip("\n"):
            bad.append((rel, first_diff(old, content)))

    # 示例报告是「实拍」的产物，不是脚本生成的 —— 改为核对**内部自洽**
    # 与 README 口径一致（改一处忘另一处，是这文件最容易被改坏的方式）。
    rep = read_json(REPORT_EXAMPLE)
    if rep is None:
        bad.append((REPORT_EXAMPLE, "文件读不到或不是合法 JSON"))
        return bad

    total = rep.get("字段总数")
    auto = rep.get("可自动填")
    gates = len(rep.get("人工门") or [])
    miss = len(rep.get("档案缺项") or [])
    if None in (total, auto) or total != auto + gates + miss:
        bad.append((REPORT_EXAMPLE,
                    "字段总数 %s ≠ 可自动填 %s + 人工门 %s + 档案缺项 %s"
                    % (total, auto, gates, miss)))

    res = rep.get("填写结果") or {}
    done = res.get("已填")
    if done is not None:
        accounted = done + len(res.get("失败") or []) + len(res.get("跳过") or [])
        if accounted != auto:
            bad.append((REPORT_EXAMPLE,
                        "可自动填 %s ≠ 已填 %s + 失败 %s + 跳过 %s"
                        % (auto, done, len(res.get("失败") or []),
                           len(res.get("跳过") or []))))
    return bad


# ---------------------------------------------------------------------------
# 核对 / 修复
# ---------------------------------------------------------------------------
def check(fx=None):
    fx = fx or facts()
    doc_bad = check_docs(fx)
    ex_bad = check_examples()

    print("=" * 60)
    print("文档数字一致性门")
    print("=" * 60)
    print("事实表（全部现算）：")
    for k in sorted(fx):
        print("  %-12s %s" % (k, fx[k]))
    print("")
    print("散文声明：%d 条" % len(CLAIMS))

    for rel, k, wrote, real in doc_bad:
        if wrote is None:
            print("[FAIL] %s" % rel)
        else:
            print("[FAIL] %s ｜ %s 写的是 %s，实际 %s" % (rel, k, wrote, real))
    for rel, why in ex_bad:
        print("[FAIL] %s ｜ %s" % (rel, why))

    total = len(doc_bad) + len(ex_bad)
    print("")
    if total:
        print("结论：FAIL %d 项 —— 文档与资产已分叉。" % total)
        print("      跑 `python scripts/doc_check.py --fix` 可自动改写（改前自动留 .bak）。")
        return 1
    print("结论：全部一致。文档里的数字都能从资产现算出来。")
    return 0


def _backup(path, day):
    dst = "%s.bak%s-docfix" % (path, day)
    shutil.copy2(path, dst)
    return dst


def _rebuild(m, keys, fx):
    """按捕获组的位置，把数字换成现算值 —— 不动这句话的其余任何一个字。"""
    parts = []
    last = m.start(0)
    for i, k in enumerate(keys, 1):
        try:
            a, b = m.span(i)
        except IndexError:
            break
        parts.append(m.string[last:a])
        parts.append(str(fx[k]))
        last = b
    parts.append(m.string[last:m.end(0)])
    return "".join(parts)


def fix(fx=None):
    fx = fx or facts()
    day = datetime.datetime.now().strftime("%Y%m%d")
    changed = []

    for rel, pat, keys in CLAIMS:
        text = read_text(rel)
        if text is None:
            print("[跳过] %s（读不到）" % rel)
            continue
        new = re.sub(pat, lambda m, k=keys: _rebuild(m, k, fx), text)
        if new == text:
            continue
        full = os.path.join(ROOT, rel)
        bak = _backup(full, day)
        core.safe_write(full, new, overwrite=True)
        changed.append((rel, bak))

    for rel, content in regenerate_examples():
        old = read_text(rel)
        if old is not None and old.rstrip("\n") == content.rstrip("\n"):
            continue
        full = os.path.join(ROOT, rel)
        bak = _backup(full, day)
        core.safe_write(full, content, overwrite=True)
        changed.append((rel, bak))

    print("=" * 60)
    if not changed:
        print("无需改动：文档与资产本来就一致。")
        return 0
    print("已改写 %d 个文件（各自留了 .bak 备份）：" % len(changed))
    for rel, bak in changed:
        print("  %s" % rel)
        print("      备份：%s" % os.path.basename(bak))
    print("")
    print("提示：改完请复跑 `python scripts/doc_check.py` 确认归零。")
    return 0


# ---------------------------------------------------------------------------
# 自测
# ---------------------------------------------------------------------------
def selftest():
    ok, bad = [0], [0]

    def check_it(name, cond, extra=""):
        if cond:
            ok[0] += 1
            print("  [OK] %s" % name)
        else:
            bad[0] += 1
            print("  [FAIL] %s %s" % (name, extra))

    fx = facts()

    check_it("① 事实表算得出来，且都是正整数",
             bool(fx) and all(isinstance(v, int) and v > 0 for v in fx.values()),
             fx)
    check_it("② 字段总数 = 可填值字段 + 附件类",
             fx["字段总数"] == fx["可填值字段"] + fx["附件类"], fx)
    check_it("③ 清单三档之和 = 可填值字段",
             fx["必填档"] + fx["常用档"] + fx["其余选填档"] == fx["可填值字段"],
             (fx["必填档"], fx["常用档"], fx["其余选填档"], fx["可填值字段"]))
    check_it("④ 事实表是从资产现算的（不是写死的常量）",
             facts() == fx, "两次现算结果不一致")

    # ---- 门本身必须有效：正例放行、反例必拦、删声明也拦 ----
    rel, pat, keys = CLAIMS[0]
    good = "│   ├── 字段规则_通用.json   字段知识库（%d 字段 / %d 标签 / %d 组值域别名）" % (
        fx["字段总数"], fx["标签写法"], fx["值域别名组"])
    check_it("⑤ 反证：写对了 → 不报", claims_in(good, pat, keys, fx) == [],
             claims_in(good, pat, keys, fx))

    off = fx["字段总数"] + 1
    wrong = "字段知识库（%d 字段 / %d 标签 / %d 组值域别名）" % (
        off, fx["标签写法"], fx["值域别名组"])
    got = claims_in(wrong, pat, keys, fx)
    check_it("⑥ 反证：数字写错 → 必报，且报的是错的那一项",
             len(got) == 1 and got[0][0] == "字段总数" and got[0][1] == off, got)

    check_it("⑦ 反证：整句被删掉 → 也报（门不许静默失效）",
             claims_in("这段文字里没有那句话", pat, keys, fx) != [])

    # ---- 示例产物的重生成必须真的能对上 ----
    pairs = regenerate_examples()
    check_it("⑧ 重生成覆盖两份示例产物",
             [p[0] for p in pairs] == [CHECKLIST_EXAMPLE, SHEET_EXAMPLE],
             [p[0] for p in pairs])
    check_it("⑨ 重生成是纯函数（两次结果一致）",
             [c for _, c in regenerate_examples()] == [c for _, c in pairs])
    check_it("⑩ 示例产物里的生成时间是固定示例戳（可复现）",
             SHEET_STAMP in pairs[1][1] and CHECKLIST_STAMP in pairs[0][1])

    # ---- 事实键不许有声明表里不存在的（防打错键名后静默通过）----
    used = set()
    for _, _, ks in CLAIMS:
        used.update(ks)
    check_it("⑪ 声明表里的事实键都在事实表里（不会有打错的键名）",
             used <= set(fx), sorted(used - set(fx)))

    print("\n自测结果：%d 通过 / %d 失败" % (ok[0], bad[0]))
    return 0 if bad[0] == 0 else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="文档数字一致性门")
    ap.add_argument("--list", action="store_true", help="只打印事实表")
    ap.add_argument("--fix", action="store_true",
                    help="用现算值改写散文数字，并重生成示例产物（改前留 .bak）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    fx = facts()
    if args.list:
        for k in sorted(fx):
            print("%-12s %s" % (k, fx[k]))
        return 0
    if args.fix:
        return fix(fx)
    return check(fx)


if __name__ == "__main__":
    sys.exit(main())
