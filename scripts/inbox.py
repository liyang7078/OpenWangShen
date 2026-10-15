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
import report_feedback as rf  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DROPPED = os.path.join(ROOT, "feedback_out", "已忽略.json")
# 「上次看到哪儿」的状态文件：用来自动提醒「你多久没看收件台了」。
# 它只存时间与计数，**不存任何反馈内容**。
STATE = os.path.join(ROOT, "feedback_out", "收件台状态.json")
DEFAULT_REPO = "liyang7078/OpenWangShen"
UA = "OpenWangShen-inbox/1.0 (+https://github.com/%s)" % DEFAULT_REPO

# 抗滥用上界：Issue 正文完全不可信（公开仓库，任何人可提交）
MAX_BODY = 20_000       # 单条正文上限（超出截断）
MAX_ISSUES = 300        # 一次最多处理多少条
MAX_LABEL = 60          # 抽出的字段标签上限
# 默认「多久没看就提醒」的天数
DEFAULT_REMIND_DAYS = 3

# 严重度 → 处置优先级（数字越小越急）
SEV_RANK = {"高": 1, "中": 2, "低": 3}


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
    """
    从 Issue 正文里抽出关键项。抽不到 → 空串（不猜）。
    新增的四列（站点域名/技术栈/控件类型/缺口类别）在旧版模板里没有 ⇒
    老 Issue 解析出来就是空，**照旧能用**（向后兼容，不因模板升级而读不了旧件）。
    """
    got = {"字段标签": "", "系统": "", "期望键": "", "实际结果": "",
           "站点域名": "", "技术栈": "", "控件类型": "", "缺口类别": ""}
    if not body:
        return got
    # 正文不可信：先截断再解析，避免超大 payload 拖死本机
    text = str(body)[:MAX_BODY]
    for k, v in CELL.findall(text):
        k, v = k.strip(), (v or "").strip().strip("`").strip()
        if v in ("—", "-"):
            v = ""
        if "字段标签" in k:
            got["字段标签"] = v[:MAX_LABEL]
        elif "站点域名" in k:
            got["站点域名"] = v[:80]
        elif "系统" in k and not got["系统"]:
            got["系统"] = v[:40]
        elif "技术栈" in k:
            got["技术栈"] = v[:20]
        elif "控件类型" in k:
            got["控件类型"] = v[:20]
        elif "缺口类别" in k:
            got["缺口类别"] = v[:40]
        elif "期望映射" in k:
            got["期望键"] = v[:MAX_LABEL]
        elif "实际结果" in k:
            got["实际结果"] = v[:40]
    return got


def to_item(issue):
    """
    Issue → 问题库条目。

    ⚠️ 这里**直接调 report_feedback.to_issue**，不再各写一份映射。
    理由：以前两侧各有一套「现象/站点/严重度」的算法，改一处就分叉 ——
    分叉的后果是「考生说的」和「作者看到的」不是一回事，而且没人会发现。
    现在共用同一个函数，**结构上不可能漂移**（P1-5/P1-8 的根治）。
    """
    p = parse_body(issue.get("body") or "")
    lab = (p["字段标签"] or (issue.get("title") or ""))[:MAX_LABEL]
    res = p["实际结果"] or "未命中"
    sysname = p["系统"] or rf.UNKNOWN_SYSTEM
    payload = {"字段标签": lab, "系统": sysname, "实际结果": res,
               "缺口类别": p["缺口类别"], "站点域名": p["站点域名"],
               "技术栈": p["技术栈"], "控件类型": p["控件类型"],
               "期望键": p["期望键"]}
    item = rf.to_issue(payload, source=issue.get("number"))
    item["来源Issue"] = issue.get("number")
    item["提交人"] = ""                  # 不留 GitHub 用户名，免带身份痕迹
    if p["站点域名"]:
        item["站点域名"] = p["站点域名"]
    if p["技术栈"]:
        item["技术栈"] = p["技术栈"]
    if p["控件类型"]:
        item["控件类型"] = p["控件类型"]
    return item, p


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
# 优先级与排序（作者侧体验 · P1-7）
# --------------------------------------------------------------------------
# 一屏几十条时，**先看哪条**比「能不能看到」更决定效率。判据只用两条：
#   · 严重度（高的先看）
#   · 是否已经被处理过（已收/已忽略的沉到底）
# 同一档内按 Issue 号升序（老的先看）—— 稳定、可预期。
TAG_RANK = {"新": 0, "批内重复": 1, "同字段": 2, "已收": 3, "已忽略": 4}


def priority_of(row):
    """
    返回 (档位, 人话)。档位越小越该先看。
      P1 = 高危且是全新问题          P2 = 中危新问题 / 已有同字段的高危
      P3 = 其余待决                    P4 = 已收 / 已忽略（只需扫一眼）
    """
    sev = (row.get("item") or {}).get("严重度") or "中"
    s = SEV_RANK.get(sev, 2)
    tag = row.get("tag") or "新"
    if tag in ("已收", "已忽略"):
        return 4, "已处理"
    if tag == "同字段":
        return 2 if s == 1 else 3, "已有解"
    if tag == "批内重复":
        return 3, "重复"
    return (1, "先看") if s == 1 else (2, "值得收")


def sort_rows(rows):
    """按「优先级档 → 严重度 → 是否新 → Issue 号」稳定排序。"""
    def key(r):
        rank, _ = priority_of(r)
        sev = (r.get("item") or {}).get("严重度") or "中"
        return (rank, SEV_RANK.get(sev, 2),
                TAG_RANK.get(r.get("tag") or "新", 0),
                r.get("issue", {}).get("number") or 0)
    rows.sort(key=key)
    return rows


# --------------------------------------------------------------------------
# 「你多久没看收件台了」提醒（P1-7）
# --------------------------------------------------------------------------
def load_state(path=STATE):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:                                        # noqa: BLE001
        return {}


def save_state(st, path=STATE):
    """状态文件只存「上次查看时间 / 上次未决数」，**不含任何反馈内容**。"""
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            json.dump(st, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass


def stale_days(st, now=None):
    """距上次查看过了几天。没记录过 → None（首次运行不提醒）。"""
    ts = (st or {}).get("上次查看")
    if not ts:
        return None
    try:
        last = datetime.datetime.strptime(ts, "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    return ((now or datetime.datetime.now()) - last).days


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
    # 空正文不抛错 —— 真跑一遍，不是写死 True
    try:
        _item14, p14 = to_item({"number": 14, "title": "", "body": None})
        empty_ok = (all(x == "" for x in p14.values())
                    and _item14["站点"] == ["*"])
    except Exception as esc:                                 # noqa: BLE001
        empty_ok = False
        print("      异常：%s" % esc)
    check("⑭ 正文为 None 不抛错且解析全空", empty_ok)

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

    # ---- 新采样列的解析（向后兼容旧模板）----
    rich = ("| 页面字段标签（含星号） | `籍贯*` |\n"
            "| 页面所在系统 | 自研站 |\n"
            "| 站点域名 | job.example.org |\n"
            "| 页面技术栈 | Vue |\n"
            "| 控件类型 | 地区级联 |\n"
            "| 实际结果 | 控件写不进去 |\n"
            "| 缺口类别 | 自绘控件写不进去（日历/级联） |\n")
    pr = parse_body(rich)
    check("⑱ 新采样列被解析",
          pr["站点域名"] == "job.example.org" and pr["技术栈"] == "Vue"
          and pr["控件类型"] == "地区级联"
          and pr["缺口类别"] == "自绘控件写不进去（日历/级联）", pr)
    check("⑲ 旧模板仍能解析（缺的列为空）",
          parse_body(body)["站点域名"] == "" and parse_body(body)["缺口类别"] == "")
    check("⑳ 「—」占位不当成值", parse_body("| 站点域名 | — |\n")["站点域名"] == "")

    # ---- 两侧映射同源（P1-8 的根治）----
    item_r, pr2 = to_item({"number": 21, "title": "t", "body": rich})
    same = rf.to_issue({"字段标签": "籍贯*", "系统": "自研站",
                        "实际结果": "控件写不进去",
                        "缺口类别": "自绘控件写不进去（日历/级联）"}, source=21)
    check("㉑ 同一份内容两侧条目逐键相等",
          all(item_r.get(k) == same.get(k)
              for k in ("现象", "站点", "指纹", "严重度", "影响", "路径", "缺口类别")),
          (item_r.get("严重度"), same.get("严重度")))

    # ---- 优先级与批量（P1-7）----
    hi = {"issue": {"number": 1}, "parsed": {}, "tag": "新",
          "item": {"严重度": "高"}, "title": "a"}
    mid = {"issue": {"number": 2}, "parsed": {}, "tag": "新",
           "item": {"严重度": "中"}, "title": "b"}
    done = {"issue": {"number": 3}, "parsed": {}, "tag": "已收",
            "item": {"严重度": "高"}, "title": "c"}
    check("㉒ 高危新问题判 P1", priority_of(hi)[0] == 1)
    check("㉓ 已处理的沉底", priority_of(done)[0] == 4)
    ordered = sort_rows([done, mid, hi])
    check("㉔ 排序把 P1 顶到最前、已收沉底",
          [x["issue"]["number"] for x in ordered] == [1, 2, 3],
          [x["issue"]["number"] for x in ordered])
    check("㉕ 批量编号解析（中文逗号/去重/忽略脏值）",
          _parse_picks("1，3, 3 ,x,") == [1, 3], _parse_picks("1，3, 3 ,x,"))

    # 状态与提醒：只认时间，不认内容
    check("㉖ 超过阈值判为「久未查看」",
          stale_days({"上次查看": "2000-01-01 00:00"}) > DEFAULT_REMIND_DAYS)
    check("㉗ 没有记录时不提醒（首次运行）", stale_days({}) is None)

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
    ap.add_argument("--accept", default="",
                    help="把第 N 条登记进问题库；也可一次多条：--accept 1,3,5")
    ap.add_argument("--accept-all", action="store_true",
                    help="把本屏所有「值得收」的一次收掉（已收/同字段/重复的自动跳过）")
    ap.add_argument("--drop", default="", help="忽略第 N 条（可多条：1,3）")
    ap.add_argument("--days", type=int, default=DEFAULT_REMIND_DAYS,
                    help="超过这么多天没看就提醒（默认 %d）" % DEFAULT_REMIND_DAYS)
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
    sort_rows(rows)                       # 优先级排序（高危 + 新问题排前面）

    if args.json:
        print(json.dumps(
            [{"编号": i + 1, "issue": r["issue"].get("number"),
              "标题": r["title"], "查重": r["tag"], "命中": r["dup"],
              "优先级": priority_of(r)[0], "条目": r["item"]}
             for i, r in enumerate(rows)],
            ensure_ascii=False, indent=2))
        return 0

    # ---- 处置（支持批量）----
    picks = _parse_picks(args.accept or args.drop)
    if args.accept_all:
        picks = [i + 1 for i, r in enumerate(rows) if priority_of(r)[0] <= 2]
        if not picks:
            print("[提示] 本屏没有「值得收」的新问题，无需批量收。")
            return 0
    if picks:
        bad = [n for n in picks if not (1 <= n <= len(rows))]
        if bad:
            print("[ERR] 编号超出范围：%s（共 %d 条）"
                  % ("、".join(str(x) for x in bad), len(rows)))
            return 2
        rc = 0
        for n in picks:
            r = rows[n - 1]
            rc = max(rc, _dispose(args, r, n, dropped))
            if rc:
                break                     # 一条失败就停，不继续往下写
        return rc

    # ---- 清单 ----
    if broken:
        print("[WARN] 问题库有 %d 行无法解析，已跳过" % broken)
    _remind(args, rows)
    print("")
    print("回流收件台 ｜ %s ｜ Issue %d 条 ｜ 问题库现有 %d 条"
          % (args.repo, len(rows), len(existing)))
    print("-" * 84)
    if not rows:
        print("  没有待处理的 Issue。")
        print("  （考生用 `python scripts/wangshen.py report` 生成反馈后可提交到")
        print("    https://github.com/%s/issues ）" % args.repo)
        save_state({"上次查看": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "上次未决数": 0})
        return 0
    print("  %-3s %-6s %-4s %-20s %-12s %-9s %s"
          % ("#", "Issue", "优先", "字段标签", "系统", "查重", "建议"))
    for i, r in enumerate(rows, 1):
        lab = (r["parsed"].get("字段标签") or r["title"])[:18]
        sysn = (r["parsed"].get("系统") or "—")[:10]
        rank, sug = priority_of(r)
        if r["tag"] == "已收":
            sug = "不必重复收"
        elif r["tag"] == "同字段":
            sug = "已有解，回帖告知"
        elif r["tag"] == "已忽略":
            sug = "上次已忽略"
        elif r["tag"] == "批内重复":
            sug = "与本批 %s 同字段" % r["dup"]
        print("  %-3d %-6s %-4s %-20s %-12s %-9s %s"
              % (i, "#%s" % r["issue"].get("number"), "P%d" % rank, lab, sysn,
                 r["tag"] or "新", sug))
    print("-" * 84)
    print("收：python scripts/inbox.py --accept N ［可 1,3,5］　一键收完：--accept-all")
    print("忽略：--drop N　（登记后条目落在「待试验」，须经 trial 试赢才能固化进策略库）")
    save_state({"上次查看": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                "上次未决数": len([r for r in rows if priority_of(r)[0] <= 2])})
    return 0


def _parse_picks(s):
    """把 "1,3,5" 解析成 [1,3,5]（顺带去重、忽略非法片段）。"""
    out = []
    for part in str(s or "").replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            n = int(part)
        except ValueError:
            continue
        if n not in out:
            out.append(n)
    return out


def _dispose(args, row, n, dropped):
    """处置单条：忽略 或 登记。返回退出码（0 = 成功）。"""
    if args.drop:
        dropped.append({"issue": row["issue"].get("number"),
                        "标题": row["title"],
                        "时间": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")})
        save_dropped(dropped)
        print("[OK] 已忽略第 %d 条（#%s）" % (n, row["issue"].get("number")))
        return 0
    # 入库：先过 guard，再交给 do_add（它自己还会再过一次）
    text = json.dumps(row["item"], ensure_ascii=False)
    ok, msgs = reg.guard(text, row["item"])
    if not ok:
        print("[ERR] 隐私反证失败 —— **中止，不写盘**：")
        # 只打印类型与脱敏样本（pii_scan 已脱敏）—— 绝不回显原值
        for k, v in msgs[:10]:
            print("   - %s：%s" % (k, v))
        return 3
    items, _ = reg.load_issues()
    rc, qid = reg.do_add(items, row["item"])
    if rc == 0:
        print("[OK] #%s → 已登记为 %s（状态：待试验）" % (row["issue"].get("number"), qid))
        print("     下一步：python scripts/wangshen.py trial --issue %s …" % qid)
    return rc


def _remind(args, rows):
    """「你多久没看收件台了」提醒。只在真有待决项时才响，避免刷屏。"""
    st = load_state()
    d = stale_days(st)
    pending = len([r for r in rows if priority_of(r)[0] <= 2])
    if d is not None and d >= args.days and pending:
        print("[提醒] 距上次查看收件台已 %d 天，现有 %d 条待决（上次未决 %s 条）。"
              % (d, pending, st.get("上次未决数", "—")))
        print("       堆着的反馈会过期 —— 页面改版后，旧的回帖就没人认了。")


if __name__ == "__main__":
    sys.exit(main())
