#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
inbox.py · 回流收件台（作者侧）
================================
把「别人提的反馈」汇总成一屏，让你一条条判断：**收，还是不收**。

它解决什么
----------
项目自己的问题库（`assets/问题库.jsonl`）由作者维护，考生提的反馈在 GitHub
Issue 上。一条条点开翻太慢 —— 本脚本把 Issue 拉下来、**与现有问题库查重**、
排成一屏，再按编号处置。

数据源（零依赖）
----------------
GitHub 公开仓库的 Issue API，用**标准库 urllib** 匿名读取。
不需要 `gh` CLI，不需要 token —— 公开仓库的 Issue 列表本就匿名可读。

隐私（硬约束）
--------------
收进来的内容先过 `issue_registry.guard()` 反证，命中即**中止、不写盘**。
登记走 `do_add()`，因此自动享有状态机（新条目落在「待试验」）。

用法
----
  python scripts/inbox.py                    拉取并出一屏清单
  python scripts/inbox.py --repo liyang7078/OpenWangShen
  python scripts/inbox.py --accept 1         把第 1 条登记进问题库
  python scripts/inbox.py --drop 1           忽略第 1 条（记录进已忽略清单）
  python scripts/inbox.py --from-file a.json 用本地保存的 Issue JSON（断网降级）
  python scripts/inbox.py --json             机器可读
  python scripts/inbox.py --selftest

许可：MIT
"""

import argparse
import datetime
import json
import os
import re
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import issue_registry as reg  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DROPPED = os.path.join(ROOT, "feedback_out", "已忽略.json")
DEFAULT_REPO = "liyang7078/OpenWangShen"
UA = "OpenWangShen-inbox/1.0 (+https://github.com/%s)" % DEFAULT_REPO

# 抗滥用上界：Issue 正文完全不可信（公开仓库，任何人可提交）
MAX_BODY = 20_000       # 单条正文上限（超出截断）
MAX_ISSUES = 300        # 一次最多处理多少条
MAX_LABEL = 60          # 抽出的字段标签上限


# --------------------------------------------------------------------------
# 拉取
# --------------------------------------------------------------------------
def fetch_issues(repo=DEFAULT_REPO, timeout=15):
    """拉公开仓库的 Issue（匿名）。返回 (issues, err)。"""
    url = ("https://api.github.com/repos/%s/issues"
           "?state=all&per_page=100&sort=created&direction=desc" % repo)
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/vnd.github+json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return [], "仓库不存在或未公开（%s）" % repo
        if e.code in (403, 429):
            return [], "GitHub 限流了（%d）。等几分钟再试，或改用 --from-file。" % e.code
        return [], "HTTP %d：%s" % (e.code, e.reason)
    except Exception as e:                              # noqa: BLE001
        return [], "网络不可达：%s" % e
    if not isinstance(data, list):
        return [], "返回格式不是列表"
    # GitHub 把 PR 也当 Issue，按有没有 pull_request 键区分
    items = [x for x in data if isinstance(x, dict) and "pull_request" not in x]
    return items[:MAX_ISSUES], ""


# --------------------------------------------------------------------------
# 解析 Issue 正文
# --------------------------------------------------------------------------
CELL = re.compile(r"\|\s*([^|]+?)\s*\|\s*([^|]*?)\s*\|")


def parse_body(body):
    """从 Issue 正文里抽出四个关键项。抽不到 → 空串（不猜）。"""
    got = {"字段标签": "", "系统": "", "期望键": "", "实际结果": ""}
    if not body:
        return got
    # 正文不可信：先截断再解析，避免超大 payload 拖死本机
    text = str(body)[:MAX_BODY]
    for k, v in CELL.findall(text):
        k, v = k.strip(), (v or "").strip().strip("`").strip()
        if "字段标签" in k:
            got["字段标签"] = v[:MAX_LABEL]
        elif "系统" in k and not got["系统"]:
            got["系统"] = v[:40]
        elif "期望映射" in k:
            got["期望键"] = v[:MAX_LABEL]
        elif "实际结果" in k:
            got["实际结果"] = v[:40]
    return got


def to_item(issue):
    """Issue → 问题库条目（与 report_feedback.to_issue 同一套映射）。"""
    p = parse_body(issue.get("body") or "")
    lab = (p["字段标签"] or (issue.get("title") or ""))[:MAX_LABEL]
    res = p["实际结果"] or "未命中"
    sysname = p["系统"] or "不知道"
    sev = {"未命中": "中", "映射到了错误的键": "高",
           "选项对不上": "中"}.get(res, "低")
    return {
        "现象": "字段「%s」%s" % (lab, res),
        "站点": [sysname] if sysname and sysname != "不知道" else ["*"],
        "指纹": [],
        "影响": "该字段需手工填写",
        "严重度": sev,
        "路径": [],
        "来源": "考生反馈",
        "来源Issue": issue.get("number"),
        "提交人": "",                       # 不留 GitHub 用户名，免带身份痕迹
    }, p


# --------------------------------------------------------------------------
# 查重
# --------------------------------------------------------------------------
def mark_batch_dups(rows):
    """批内查重：同一批里字段标签相同的，后面那条标成「批内重复」。

    只跟**已有问题库**查重是不够的 —— 三条 Issue 说的都是「联系人手机*」时，
    后两条会都显示「新」，看的人得自己发现它们其实是一件事。
    """
    seen = {}
    for i, r in enumerate(rows):
        if r.get("tag"):
            continue                       # 已被「已收 / 同字段 / 已忽略」判过的不覆盖
        lab = (r.get("parsed", {}).get("字段标签") or "").strip()
        if not lab:
            continue
        if lab in seen:
            r["tag"] = "批内重复"
            r["dup"] = "#%s" % rows[seen[lab]]["issue"].get("number")
        else:
            seen[lab] = i
    return rows


def dup_of(item, existing):
    """已登记过 → ('已收', Q-id)；同字段出现过 → ('同字段', Q-id)；否则 (('', ''))。"""
    n = item.get("来源Issue")
    for x in existing:
        if n and x.get("来源Issue") == n:
            return "已收", x.get("id")
    lab = (item.get("现象") or "")
    m = re.search(r"「(.+?)」", lab)
    if m:
        token = m.group(1)
        for x in existing:
            if token and token in (x.get("现象") or ""):
                return "同字段", x.get("id")
    return "", ""


# --------------------------------------------------------------------------
# 忽略清单（append-only，便于下次不再重复看）
# --------------------------------------------------------------------------
def load_dropped(path=DROPPED):
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, list) else []
    except Exception:                                   # noqa: BLE001
        return []


def save_dropped(rows, path=DROPPED):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=2)


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

    body = ("## 字段信息\n\n| 项 | 内容 |\n|---|---|\n"
            "| 页面字段标签（含星号） | `联系人手机*` |\n"
            "| 页面所在系统 | 北森 |\n"
            "| 期望映射到哪个键 | `手机号` |\n"
            "| 实际结果 | 未命中 |\n")
    p = parse_body(body)
    check("① 抽到字段标签", p["字段标签"] == "联系人手机*", p)
    check("② 抽到系统", p["系统"] == "北森", p)
    check("③ 抽到期望键", p["期望键"] == "手机号", p)
    check("④ 抽到实际结果", p["实际结果"] == "未命中", p)

    issue = {"number": 12, "title": "[字段] 某字段未被识别", "body": body}
    item, _ = to_item(issue)
    check("⑤ 转条目：站点取自系统", item["站点"] == ["北森"])
    check("⑥ 转条目：来源Issue 落上", item["来源Issue"] == 12)
    check("⑦ 转条目：严重度随结果", item["严重度"] == "中")
    check("⑧ 转条目过 guard", reg.guard(json.dumps(item, ensure_ascii=False), item)[0])

    existing = [{"id": "Q-20260921-002", "来源Issue": 12,
                 "现象": "字段「联系人手机*」未命中"}]
    check("⑨ 查重：已收", dup_of(item, existing) == ("已收", "Q-20260921-002"))
    same = [{"id": "Q-20260921-009",
             "现象": "上传控件被隐藏，字段「联系人手机*」也在这一组"}]
    check("⑩ 查重：同字段", dup_of(item, same) == ("同字段", "Q-20260921-009"))
    check("⑪ 查重：新问题不误报", dup_of(item, []) == ("", ""))

    bare = {"number": 13, "title": "[字段] 某某某未被识别", "body": ""}
    it2, _ = to_item(bare)
    check("⑫ 正文为空时退化用标题", "某某某" in it2["现象"], it2["现象"])
    check("⑬ 系统未知 → 通配", it2["站点"] == ["*"])
    check("⑭ 空正文不抛错", True)

    # ---- 批内查重 ----
    rows = [
        {"issue": {"number": 1}, "parsed": {"字段标签": "手机*"}, "tag": "", "dup": ""},
        {"issue": {"number": 2}, "parsed": {"字段标签": "手机*"}, "tag": "", "dup": ""},
        {"issue": {"number": 3}, "parsed": {"字段标签": "邮箱*"}, "tag": "", "dup": ""},
    ]
    mark_batch_dups(rows)
    check("⑮ 批内同字段被标出",
          rows[1]["tag"] == "批内重复" and rows[1]["dup"] == "#1", rows[1])
    check("⑯ 首条与不同字段不误标", rows[0]["tag"] == "" and rows[2]["tag"] == "")
    rows2 = [{"issue": {"number": 1}, "parsed": {"字段标签": "手机*"},
              "tag": "已收", "dup": "Q-1"},
             {"issue": {"number": 2}, "parsed": {"字段标签": "手机*"},
              "tag": "", "dup": ""}]
    mark_batch_dups(rows2)
    check("⑰ 已判决的不被批内查重覆盖", rows2[0]["tag"] == "已收")

    print("\n自测结果：%d 通过 / %d 失败" % (ok[0], bad[0]))
    return 0 if bad[0] == 0 else 1


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="回流收件台：把 GitHub Issue 汇总成一屏，逐条决定收不收",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python scripts/inbox.py\n"
               "  python scripts/inbox.py --accept 1\n")
    ap.add_argument("--repo", default=DEFAULT_REPO, help="owner/repo")
    ap.add_argument("--accept", type=int, default=0, help="把第 N 条登记进问题库")
    ap.add_argument("--drop", type=int, default=0, help="忽略第 N 条")
    ap.add_argument("--from-file", default="", help="从本地 Issue JSON 读（断网降级）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--selftest", action="store_true", help="自测")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()

    # ---- 取数据 ----
    if args.from_file:
        if not os.path.isfile(args.from_file):
            print("[ERR] 找不到文件：%s" % args.from_file)
            return 2
        try:
            with open(args.from_file, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except json.JSONDecodeError as e:
            print("[ERR] 这个文件不是合法 JSON（第 %d 行第 %d 列）：%s"
                  % (e.lineno, e.colno, e.msg))
            print("      期望格式：GitHub /issues 接口返回的 JSON 数组。")
            print("      想自己造几条试试？看 inbox.py --selftest 里的示例。")
            return 2
        except OSError as e:
            print("[ERR] 读不了这个文件：%s" % e)
            return 2
        except UnicodeDecodeError:
            print("[ERR] 这个文件不是 UTF-8 文本（可能是 .zip / .docx 之类）。")
            return 2
        issues = [x for x in (raw if isinstance(raw, list) else [raw])
                  if isinstance(x, dict) and "pull_request" not in x][:MAX_ISSUES]
        err = ""
    else:
        print("正在读取 %s 的 Issue …" % args.repo)
        issues, err = fetch_issues(args.repo)

    if err:
        print("[ERR] %s" % err)
        print("\n降级办法：")
        print("  1) 过一会儿重试")
        print("  2) 浏览器打开 https://github.com/%s/issues ，复制 JSON 存成本地文件，"
              % args.repo)
        print("     再跑：python scripts/inbox.py --from-file <该文件>")
        return 2

    issues.sort(key=lambda x: x.get("number") or 0)
    existing, broken = reg.load_issues()
    dropped = load_dropped()
    dropped_nums = {d.get("issue") for d in dropped}

    rows = []
    for iss in issues:
        item, parsed = to_item(iss)
        tag, hid = dup_of(item, existing)
        if iss.get("number") in dropped_nums and not tag:
            tag = "已忽略"
        rows.append({"issue": iss, "item": item, "parsed": parsed,
                     "tag": tag, "dup": hid,
                     "title": (iss.get("title") or "").strip()})

    mark_batch_dups(rows)

    if args.json:
        print(json.dumps(
            [{"编号": i + 1, "issue": r["issue"].get("number"),
              "标题": r["title"], "查重": r["tag"], "命中": r["dup"],
              "条目": r["item"]} for i, r in enumerate(rows)],
            ensure_ascii=False, indent=2))
        return 0

    # ---- 处置 ----
    if args.accept or args.drop:
        n = args.accept or args.drop
        if not (1 <= n <= len(rows)):
            print("[ERR] 编号 %d 超出范围（共 %d 条）" % (n, len(rows)))
            return 2
        r = rows[n - 1]
        if args.drop:
            dropped.append({"issue": r["issue"].get("number"),
                            "标题": r["title"],
                            "时间": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")})
            save_dropped(dropped)
            print("[OK] 已忽略第 %d 条（#%s）" % (n, r["issue"].get("number")))
            return 0
        # 入库：先过 guard，再交给 do_add（它自己还会再过一次）
        text = json.dumps(r["item"], ensure_ascii=False)
        ok, msgs = reg.guard(text, r["item"])
        if not ok:
            print("[ERR] 隐私反证失败 —— **中止，不写盘**：")
            # 只打印类型与脱敏样本（pii_scan 已脱敏）—— 绝不回显原值
            for k, v in msgs[:10]:
                print("   - %s：%s" % (k, v))
            return 3
        rc, qid = reg.do_add(existing, r["item"])
        if rc == 0:
            print("[OK] #%s → 已登记为 %s（状态：待试验）" % (r["issue"].get("number"), qid))
            print("     下一步：python scripts/wangshen.py trial --issue %s …" % qid)
        return rc

    # ---- 清单 ----
    if broken:
        print("[WARN] 问题库有 %d 行无法解析，已跳过" % broken)
    print("")
    print("回流收件台 ｜ %s ｜ Issue %d 条 ｜ 问题库现有 %d 条"
          % (args.repo, len(rows), len(existing)))
    print("-" * 78)
    if not rows:
        print("  没有待处理的 Issue。")
        print("  （考生用 `python scripts/wangshen.py report` 生成反馈后可提交到")
        print("    https://github.com/%s/issues ）" % args.repo)
        return 0
    print("  %-3s %-5s %-22s %-10s %-8s %s"
          % ("#", "Issue", "字段标签", "系统", "查重", "建议"))
    for i, r in enumerate(rows, 1):
        lab = (r["parsed"].get("字段标签") or r["title"])[:20]
        sysn = (r["parsed"].get("系统") or "—")[:8]
        if r["tag"] == "已收":
            sug, tag = "不必重复收", "已收"
        elif r["tag"] == "同字段":
            sug, tag = "已有解，回帖告知", "同字段"
        elif r["tag"] == "已忽略":
            sug, tag = "上次已忽略", "已忽略"
        elif r["tag"] == "批内重复":
            sug, tag = "与本批 %s 同字段，一起看" % r["dup"], "批内重复"
        else:
            sug, tag = "值得收", "新"
        print("  %-3d %-5s %-22s %-10s %-8s %s"
              % (i, "#%s" % r["issue"].get("number"), lab, sysn, tag, sug))
    print("-" * 78)
    print("收：python scripts/inbox.py --accept N     忽略：--drop N")
    print("（登记后条目落在「待试验」，须经 trial 试赢才能固化进策略库）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
