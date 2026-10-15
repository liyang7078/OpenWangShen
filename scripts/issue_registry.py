# -*- coding: utf-8 -*-
"""
问题登记库 —— 让「每遇到一个新麻烦」都留下可复用的一条记录
=====================================================================
它解决什么
----------
网申的坑是**一次一次踩出来的**：今天遇到 ATS 的 portal、明天遇到自研站的假成功。
过去这些经验只活在当次对话里，**下次换个站点又从头摸一遍** —— 这正是
「每换一个自建站都要重来」的真根因。

本模块是**自进化闭环的第①层（登记）与第③层（策略读取）**：

  ① 登记   `assets/问题库.jsonl`（追加式，一行一个问题）
     记录：站点 / 控件指纹 / 现象 / 影响 / 严重度 / 试过哪些路径（**失败的也留**）
           / 哪条胜出 / 固化到哪
  ③ 策略   `assets/站点策略.json`（胜出解法的运行时契约）
     引擎遇到控件时调 `match(site, fingerprints)`：命中则按「动作」执行。

中间的第②层（多路径试验 + 断言择优 + 固化）在 `issue_trial.py`。

状态机（写死，不允许跳级）
--------------------------
    待试验 → 试验中 → 已胜出 → 已固化
                  ↘ 已绕过
    任意 → 已知未解（暂时无解，但**必须留档**）
  规则：**只有「已胜出」才能「已固化」** —— 没试赢过的东西不许进策略库。

隐私设计（**硬约束**）
----------------------
问题库与策略库都是**问题级**产物：只含站点、控件指纹、现象、路径名，
**不含任何考生真值**。写盘前跑 `pii_scan` 反证（手机号/邮箱/身份证/绝对路径），
命中即**中止、不写盘**。所以这两份文件可以安全交给 AI 读。

用法
----
  python scripts/issue_registry.py --list
  python scripts/issue_registry.py --show Q-20260922-009
  python scripts/issue_registry.py --report --out 问题库报告.md
  python scripts/issue_registry.py --match 北森:label-missing,portal-to-body
  python scripts/issue_registry.py --add '{"现象":"...","站点":["北森"],...}'
  python scripts/issue_registry.py --set-status Q-20260922-012=已知未解

退出码：0 = 正常；2 = 参数/文件错误；3 = 零真值反证失败（**中止，不写盘**）
"""

import argparse
import datetime
import json
import os
import re
import sys
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "..", "assets")
ISSUES = os.path.join(ASSETS, "问题库.jsonl")
STRATEGIES = os.path.join(ASSETS, "站点策略.json")

# ---------------------------------------------------------------- 状态机
STATUSES = ("待试验", "试验中", "已胜出", "已固化", "已绕过", "已知未解")
TRANSITIONS = {
    "待试验": ("试验中", "已知未解", "已绕过"),
    "试验中": ("已胜出", "已绕过", "已知未解", "待试验"),
    "已胜出": ("已固化", "已知未解"),
    "已固化": ("已知未解",),          # 已固化是正常终态；退回只允许标成已知未解
    "已绕过": ("已知未解",),
    "已知未解": ("待试验", "试验中"),
}
FINAL_OK = ("已固化",)

# ---------------------------------------------------------------- 零真值反证
# 只认「值形态」，不认词 —— 「姓名」是字段标签，出现在问题描述里完全正常；
# 真出问题的是「姓名的**取值**」，那必然是手机号/邮箱/身份证/路径这类形态。
PII_PATTERNS = (
    ("手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("邮箱", re.compile(r"[\w.+-]+@[\w-]+\.[A-Za-z]{2,}")),
    ("身份证", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
    ("银行卡", re.compile(r"(?<!\d)\d{16,19}(?!\d)")),
    ("本机绝对路径", re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]")),
    ("用户目录路径", re.compile(r"/(?:Users|home)/[A-Za-z0-9_.-]+/")),
)

# 逐字扫描（不压缩）时用的形态：这些一旦被压缩就会失去可辨识边界
PII_RAW = (
    ("邮箱", re.compile(r"[\w.+-]+@[\w-]+\.[A-Za-z]{2,}")),
    ("本机绝对路径", re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]")),
    ("用户目录路径", re.compile(r"/(?:Users|home)/[A-Za-z0-9_.-]+/")),
    # 详细住址：必须同时出现「区/县/镇/乡」+ 门牌数字 + 「号/弄/栋/幢/单元/室」
    # 才判定为真实地址 —— 只有区县名（如「历下区」）不算，避免误杀字段名
    ("详细住址", re.compile(
        r"[\u4e00-\u9fa5]{1,12}(?:区|县|镇|乡)"
        r"[\u4e00-\u9fa5\d]{0,24}?\d{1,5}\s*(?:号|弄|栋|幢|单元|室)")),
)

# 压缩（去空白/连字符/点号）**之后**再扫的形态：
# 这一组专治全角（１３８…）与分隔符变形（138 0013 8000）
PII_SQUEEZED = (
    ("手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("身份证", re.compile(r"(?<![0-9Xx])\d{17}[\dXx](?![0-9Xx])")),
    ("银行卡", re.compile(r"(?<!\d)\d{16,19}(?!\d)")),
)

_SEP_RE = re.compile(r"[\s\u3000\-_.·・—–~]")

# 结构上禁止出现的键（万一有人把带真值的报告整段塞进来）
FORBIDDEN_KEYS = ("填写结果", "回读", "附件结果", "档案值", "profile", "本机档案")


def _squeeze(text):
    """
    NFKC 归一 + 去分隔符。
    「１３８００１３８０００」「138 0013 8000」「138-0013-8000」→ 同一个形态。
    """
    t = unicodedata.normalize("NFKC", text or "")
    return _SEP_RE.sub("", t)


def _mask(value):
    """
    脱敏展示：只留首尾各 1 个字符。
    **绝不把命中值原样返回** —— 否则调用方一 print，真值就上了终端。
    """
    v = value or ""
    if len(v) <= 2:
        return "*" * len(v)
    return "%s%s%s" % (v[0], "*" * (len(v) - 2), v[-1])


def pii_scan(text):
    """
    扫「值形态」的隐私。返回 [(类型, 脱敏样本)]。

    两轮扫描：
      ① 原样  —— 邮箱 / 路径 / 详细住址（有边界语义，压缩会破坏）
      ② 压缩后 —— 手机号 / 身份证 / 银行卡（治全角与空格/连字符变形）
    """
    raw = unicodedata.normalize("NFKC", text or "")
    sq = _squeeze(text)
    hits = []
    for name, rx in PII_RAW:
        for m in rx.finditer(raw):
            hits.append((name, _mask(m.group(0))))
    for name, rx in PII_SQUEEZED:
        for m in rx.finditer(sq):
            hits.append((name, _mask(m.group(0))))
    return hits


def key_scan(obj, trail=""):
    """递归找禁用键（含真值的三段）。返回 [路径]。"""
    bad = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k) in FORBIDDEN_KEYS:
                bad.append("%s/%s" % (trail, k))
            bad += key_scan(v, "%s/%s" % (trail, k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            bad += key_scan(v, "%s[%d]" % (trail, i))
    return bad


def guard(payload_text, payload_obj=None):
    """写盘前的总闸。返回 (ok, 说明列表)。"""
    msgs = [("PII:%s" % t, v) for t, v in pii_scan(payload_text)]
    if payload_obj is not None:
        msgs += [("禁用键", p) for p in key_scan(payload_obj)]
    return (not msgs), msgs


# ---------------------------------------------------------------- 读写
def load_issues(path=ISSUES):
    """读 jsonl。坏行**跳过并计数**，不因一行坏掉整库。"""
    if not os.path.isfile(path):
        return [], 0
    items, broken = [], 0
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:                              # noqa: BLE001
                broken += 1
                continue
            if isinstance(d, dict):
                d.setdefault("状态", "待试验")
                d.setdefault("路径", [])
                d.setdefault("站点", [])
                d.setdefault("指纹", [])
                items.append(d)
            else:
                broken += 1
    return items, broken


def save_issues(items, path=ISSUES, backup=True):
    """整库重写（状态更新需要）。**先备份**，且写盘前跑零真值反证。"""
    text = "\n".join(json.dumps(x, ensure_ascii=False) for x in items) + "\n"
    ok, msgs = guard(text, items)
    if not ok:
        return False, msgs, ""
    bak = ""
    if backup and os.path.isfile(path):
        bak = path + ".bak" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        with open(path, "r", encoding="utf-8") as src, \
                open(bak, "w", encoding="utf-8", newline="") as dst:
            dst.write(src.read())
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    return True, [], bak


def load_strategies(path=STRATEGIES):
    if not os.path.isfile(path):
        return {"策略": []}
    with open(path, "r", encoding="utf-8") as fh:
        d = json.load(fh)
    d.setdefault("策略", [])
    return d


# ---------------------------------------------------------------- 查询/匹配
def find(items, qid):
    for x in items:
        if x.get("id") == qid:
            return x
    return None


def next_id(items, today=None):
    """生成 Q-YYYYMMDD-NNN（当日序号递增）。"""
    day = today or datetime.datetime.now().strftime("%Y%m%d")
    pre = "Q-%s-" % day
    n = 0
    for x in items:
        i = str(x.get("id") or "")
        if i.startswith(pre):
            try:
                n = max(n, int(i[len(pre):]))
            except ValueError:
                pass
    return "%s%03d" % (pre, n + 1)


def match(site, fingerprints, path=STRATEGIES):
    """策略匹配：**站点精确优先，再通配**；同档按指纹重叠数降序。

    返回命中的策略列表（按优先级）。引擎拿第一条即可；要全部也可以。
    什么都不命中 → 空列表（引擎走内置默认逻辑），**不报错、不猜**。
    """
    strat = load_strategies(path).get("策略") or []
    site = (site or "").strip()
    fps = set(fingerprints or [])
    got = []
    for s in strat:
        sites = s.get("站点") or []
        exact = site and site in sites
        wild = "*" in sites
        if not (exact or wild):
            continue
        overlap = len(fps & set(s.get("指纹") or []))
        if fps and overlap == 0:
            continue                      # 指定了指纹就必须有交集，否则不算命中
        got.append((0 if exact else 1, -overlap, s))
    got.sort(key=lambda x: (x[0], x[1]))
    return [x[2] for x in got]


def stats(items):
    by_status, by_site, by_fp = {}, {}, {}
    for x in items:
        by_status[x.get("状态") or "?"] = by_status.get(x.get("状态") or "?", 0) + 1
        for s in (x.get("站点") or ["?"]):
            by_site[s] = by_site.get(s, 0) + 1
        for f in (x.get("指纹") or []):
            by_fp[f] = by_fp.get(f, 0) + 1
    done = sum(v for k, v in by_status.items() if k in FINAL_OK)
    return {"总数": len(items), "按状态": by_status, "按站点": by_site,
            "按指纹": by_fp, "已固化": done,
            "固化解率": ("%d/%d" % (done, len(items))) if items else "0/0"}


def render_report(items, path=ISSUES):
    st = stats(items)
    L = ["# 网申问题库报告（问题级 · 零真值）", "",
         "- 生成时间：%s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
         "- 库文件：`assets/问题库.jsonl`（追加式，一行一个问题）",
         "- 口径：只含站点、控件指纹、现象与解法，**不含你的任何真实取值**", "",
         "## 一、总览", "",
         "| 指标 | 值 |", "|---|---|",
         "| 问题总数 | %d |" % st["总数"],
         "| 已固化 | %s |" % st["固化解率"]]
    for k in STATUSES:
        if st["按状态"].get(k):
            L.append("| 状态：%s | %d |" % (k, st["按状态"][k]))
    L += ["", "## 二、按状态分列", ""]
    for k in STATUSES:
        grp = [x for x in items if (x.get("状态") or "?") == k]
        if not grp:
            continue
        L += ["### %s（%d 条）" % (k, len(grp)), "",
              "| ID | 站点 | 现象 | 胜出路径 | 固化到 |", "|---|---|---|---|---|"]
        for x in grp:
            L.append("| %s | %s | %s | %s | %s |" % (
                x.get("id"), "、".join(x.get("站点") or []) or "—",
                (x.get("现象") or "")[:60], x.get("胜出") or "—",
                ((x.get("固化") or {}).get("落到") or "—")[:40]))
        L.append("")
    L += ["## 三、指纹分布（这些特征出现得越多，越该优先工程化）", "",
          "| 控件指纹 | 出现次数 |", "|---|---|"]
    for f, n in sorted(st["按指纹"].items(), key=lambda x: -x[1]):
        L.append("| %s | %d |" % (f, n))
    L += ["", "---", "",
          "**注意**：本表是**问题清单**，不是答案。要改引擎请先跑 `issue_trial.py` 试赢，",
          "再从「已胜出」固化为 `assets/站点策略.json` 的一条策略。"]
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- CLI 动作
def do_add(items, obj, path=ISSUES, today=None):
    obj = dict(obj)
    obj.setdefault("首次发现", datetime.datetime.now().strftime("%Y-%m-%d"))
    obj.setdefault("状态", "待试验")
    obj.setdefault("路径", [])
    obj["id"] = obj.get("id") or next_id(items, today=today)
    if find(items, obj["id"]):
        return 2, "[ERR] id 已存在：%s" % obj["id"]
    items.append(obj)
    ok, msgs, bak = save_issues(items, path)
    if not ok:
        print("[ERR] 零真值反证失败 —— **中止，不写盘**：")
        # 只打印类型与脱敏样本（pii_scan 已脱敏）—— 绝不回显原值
        for k, v in msgs[:10]:
            print("   - %s：%s" % (k, v))
        return 3, ""
    print("[OK] 已登记 %s%s" % (obj["id"], "（已备份 %s）" % bak if bak else ""))
    return 0, obj["id"]


def do_set_status(items, qid, new, path=ISSUES):
    it = find(items, qid)
    if not it:
        return 2, "[ERR] 找不到 %s" % qid
    if new not in STATUSES:
        return 2, "[ERR] 非法状态 %s（可选：%s）" % (new, "/".join(STATUSES))
    cur = it.get("状态") or "待试验"
    if cur == new:
        return 0, "[SKIP] %s 已是 %s" % (qid, new)
    if new not in TRANSITIONS.get(cur, ()):
        return 2, "[ERR] 不允许 %s → %s（合法去向：%s）" % (cur, new, "/".join(TRANSITIONS.get(cur, ())))
    it["状态"] = new
    it["状态更新"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    ok, msgs, bak = save_issues(items, path)
    if not ok:
        print("[ERR] 零真值反证失败 —— **中止，不写盘**：")
        # 只打印类型与脱敏样本（pii_scan 已脱敏）—— 绝不回显原值
        for k, v in msgs[:10]:
            print("   - %s：%s" % (k, v))
        return 3, ""
    print("[OK] %s：%s → %s%s" % (qid, cur, new, "（已备份）" if bak else ""))
    return 0, new


# ---------------------------------------------------------------- 自测
def _selftest():
    ok, bad = [0], [0]

    def check(name, cond, extra=""):
        if cond:
            ok[0] += 1
            print("  [OK] %s" % name)
        else:
            bad[0] += 1
            print("  [FAIL] %s %s" % (name, extra))

    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix="issue_")
    try:
        ipath = os.path.join(tmp, "q.jsonl")
        spath = os.path.join(tmp, "s.json")

        # ---- 零真值反证 ----
        check("① 手机号被扫出", any(t == "手机号" for t, _ in pii_scan("联系13800000000")))
        check("② 邮箱被扫出", any(t == "邮箱" for t, _ in pii_scan("example@example.com")))
        check("③ 身份证被扫出",
              any(t == "身份证" for t, _ in pii_scan("110101199901011234")))
        check("④ 本机路径被扫出", any("路径" in t for t, _ in pii_scan("D:/私密/简历.pdf")))
        check("⑤ 正常问题描述不误报",
              not pii_scan("北森上传控件被隐藏，标签取不到"))
        check("⑥ 禁用键被扫出（含真值三段不得入库）",
              key_scan({"填写结果": [{"a": 1}]}) == ["/填写结果"])
        dirty_ok, _ = guard("手机 13800000000")
        check("⑦ 总闸：掺真值即拒绝", dirty_ok is False)

        # ---- 登记 + id 递增 ----
        items = []
        n0 = next_id(items, today="20260922")
        check("⑧ id 从 001 起（Q-YYYYMMDD-NNN）", n0 == "Q-20260922-001", n0)
        rc, qid = do_add(items, {"现象": "虚构：portal 后取不到标签",
                                 "站点": ["北森"], "指纹": ["portal-to-body"]}, ipath,
                         today="20260922")   # 与 ⑫ 同日，序号才连贯
        check("⑨ 登记成功并落盘", rc == 0 and os.path.isfile(ipath), "rc=%s" % rc)
        items2, broken = load_issues(ipath)
        check("⑩ 回读一致且无坏行", len(items2) == 1 and broken == 0)
        check("⑪ 默认状态=待试验", items2[0]["状态"] == "待试验")
        rc, qid2 = do_add(items, {"现象": "虚构：另一个问题"}, ipath,
                          today="20260922")   # 必须注入：否则按真实当天算，断言隔天即失效
        check("⑫ 当日序号递增到 002", qid2 == "Q-20260922-002", qid2)

        # ---- 状态机 ----
        check("⑬ 待试验 → 已固化 被拒（没试赢不许固化）",
              do_set_status(items, qid, "已固化", ipath)[0] == 2)
        check("⑭ 待试验 → 试验中 允许",
              do_set_status(items, qid, "试验中", ipath)[0] == 0)
        check("⑮ 试验中 → 已胜出 允许",
              do_set_status(items, qid, "已胜出", ipath)[0] == 0)
        check("⑯ 已胜出 → 已固化 允许",
              do_set_status(items, qid, "已固化", ipath)[0] == 0)
        check("⑰ 非法状态被拒", do_set_status(items, qid, "随便写", ipath)[0] == 2)
        check("⑱ 状态更新落盘", find(load_issues(ipath)[0], qid)["状态"] == "已固化")

        # ---- 策略匹配 ----
        strat = {"策略": [
            {"id": "S-WILD", "站点": ["*"], "指纹": ["file-hidden"], "动作": "通配"},
            {"id": "S-EXACT", "站点": ["北森"], "指纹": ["file-hidden"], "动作": "精确"},
            {"id": "S-OTHER", "站点": ["智联"], "指纹": ["file-hidden"], "动作": "别家"},
        ]}
        with open(spath, "w", encoding="utf-8", newline="") as fh:
            json.dump(strat, fh, ensure_ascii=False)
        got = match("北森", ["file-hidden"], spath)
        check("⑲ 站点精确排在通配之前", [x["id"] for x in got][:2] == ["S-EXACT", "S-WILD"],
              [x["id"] for x in got])
        check("⑳ 别家站点不进结果", "S-OTHER" not in [x["id"] for x in got])
        check("㉑ 指纹无交集 → 不命中（不猜）", match("北森", ["readonly-cascader"], spath) == [])
        check("㉒ 站点未知但通配指纹命中 → 仍可用",
              [x["id"] for x in match("没见过的站", ["file-hidden"], spath)] == ["S-WILD"])
        check("㉓ 指定指纹就必须有交集，不看站点通配就放过",
              match("没见过的站", ["不存在的指纹"], spath) == [])

        # ---- 真库自检 ----
        real, broken = load_issues()
        check("㉔ 真问题库可读且无坏行", bool(real) and broken == 0,
              "条数 %d 坏行 %d" % (len(real), broken))
        check("㉕ 真问题库零真值（值形态扫描）",
              not pii_scan(json.dumps(real, ensure_ascii=False)))
        check("㉖ 真问题库无禁用键",
              not key_scan(real))
        check("㉗ 真库里「已固化」的条目都写明了固化去向",
              all((x.get("固化") or {}).get("落到")
                  for x in real if x.get("状态") == "已固化"),
              [x.get("id") for x in real if x.get("状态") == "已固化"
               and not (x.get("固化") or {}).get("落到")])
        check("㉘ 真库里「已胜出」的条目必须有胜出路径（且已试过）",
              all(x.get("胜出") and x.get("路径")
                  for x in real if x.get("状态") in ("已胜出", "已固化")),
              [x.get("id") for x in real if x.get("状态") in ("已胜出", "已固化")
               and not (x.get("胜出") and x.get("路径"))])
        st = stats(real)
        check("㉙ 统计可算（固化解率）", "/" in st["固化解率"], st)
        rp = render_report(real)
        check("㉚ 报告含总览与指纹分布", "## 一、总览" in rp and "指纹分布" in rp)
        check("㉛ 报告零真值", not pii_scan(rp) and "不含你的任何真实取值" in rp)

        # ---- 策略库自检 ----
        sd = load_strategies()
        check("㉜ 真策略库可读", bool(sd.get("策略")), len(sd.get("策略") or []))
        vocab = set((sd.get("_指纹词表") or {}).keys())
        undeclared = sorted({f for s in sd.get("策略") or []
                             for f in (s.get("指纹") or [])} - vocab)
        check("㉝ 策略里用到的指纹都在词表里声明过（%d 个未声明）" % len(undeclared),
              not undeclared, undeclared)
        ids = [s.get("id") for s in sd.get("策略") or []]
        check("㉞ 策略 id 唯一", len(ids) == len(set(ids)))
        check("㉟ 策略库零真值", not pii_scan(json.dumps(sd, ensure_ascii=False)))
        qids = {x.get("id") for x in real}
        dangling = [s.get("来源问题") for s in sd.get("策略") or []
                    if s.get("来源问题") and s.get("来源问题") not in qids]
        check("㊱ 策略的「来源问题」都能在问题库里找到（%d 悬空）" % len(dangling),
              not dangling, dangling)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n自测结果：%d 通过 / %d 失败" % (ok[0], bad[0]))
    return 0 if bad[0] == 0 else 1


# ---------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="网申问题登记库（问题级零真值：登记 / 状态机 / 策略匹配）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python scripts/issue_registry.py --list\n"
               "  python scripts/issue_registry.py --match 北森:label-missing,portal-to-body\n"
               "  python scripts/issue_registry.py --report --out 问题库报告.md\n")
    ap.add_argument("--list", action="store_true", help="列出全部问题")
    ap.add_argument("--status", default="", help="配合 --list：只看某状态")
    ap.add_argument("--show", default="", help="看某条问题的全部路径明细")
    ap.add_argument("--report", action="store_true", help="生成问题库报告（md）")
    ap.add_argument("--out", default="", help="报告输出路径")
    ap.add_argument("--add", default="", help="登记一条问题（JSON 字符串）")
    ap.add_argument("--file", default="", help="从 json/jsonl 文件批量登记")
    ap.add_argument("--set-status", default="", help="ID=状态（如 Q-...-012=已知未解）")
    ap.add_argument("--match", default="", help="策略匹配：站点:指纹1,指纹2")
    ap.add_argument("--issues", default=ISSUES, help="问题库路径")
    ap.add_argument("--strategies", default=STRATEGIES, help="策略库路径")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出")
    ap.add_argument("--selftest", action="store_true", help="自测")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()

    items, broken = load_issues(args.issues)
    if broken:
        print("[WARN] 问题库有 %d 行无法解析，已跳过" % broken)

    if args.add:
        try:
            obj = json.loads(args.add)
        except Exception as exc:                           # noqa: BLE001
            print("[ERR] --add 不是合法 JSON：%s" % exc)
            return 2
        if not isinstance(obj, dict):
            print("[ERR] --add 应为对象")
            return 2
        return do_add(items, obj, args.issues)[0]

    if args.file:
        if not os.path.isfile(args.file):
            print("[ERR] 文件不存在：%s" % args.file)
            return 2
        with open(args.file, "r", encoding="utf-8") as fh:
            raw = fh.read()
        batch = []
        try:
            d = json.loads(raw)
            batch = d if isinstance(d, list) else [d]
        except Exception:                                  # noqa: BLE001
            for line in raw.splitlines():
                if line.strip():
                    batch.append(json.loads(line))
        rc = 0
        for b in batch:
            rc = max(rc, do_add(items, b, args.issues)[0])
        return rc

    if args.set_status:
        if "=" not in args.set_status:
            print("[ERR] --set-status 格式应为 ID=状态")
            return 2
        qid, new = args.set_status.split("=", 1)
        return do_set_status(items, qid.strip(), new.strip(), args.issues)[0]

    if args.match:
        site, _, fps = args.match.partition(":")
        got = match(site, [x for x in fps.split(",") if x.strip()], args.strategies)
        if args.json:
            print(json.dumps(got, ensure_ascii=False, indent=2))
            return 0
        if not got:
            print("（无命中策略）站点 %s ｜ 指纹 %s" % (site or "—", fps or "—"))
            print("引擎将走内置默认逻辑。要新增策略，先跑 issue_trial.py 试赢。")
            return 0
        print("命中 %d 条（优先级降序）：" % len(got))
        for s in got:
            print("  · [%s] %s" % (s.get("id"), (s.get("现象") or "")[:60]))
            print("     动作：%s" % s.get("动作"))
        return 0

    if args.show:
        it = find(items, args.show)
        if not it:
            print("[ERR] 找不到 %s" % args.show)
            return 2
        if args.json:
            print(json.dumps(it, ensure_ascii=False, indent=2))
            return 0
        print("=" * 68)
        print("%s ｜ %s ｜ 站点：%s" % (it.get("id"), it.get("状态"),
                                       "、".join(it.get("站点") or []) or "—"))
        print("指纹：%s ｜ 严重度：%s ｜ 首次发现：%s"
              % ("、".join(it.get("指纹") or []) or "—", it.get("严重度") or "—",
                 it.get("首次发现") or "—"))
        print("=" * 68)
        print("现象：%s" % it.get("现象"))
        print("影响：%s" % (it.get("影响") or "—"))
        print("\n试过的路径（失败的也留档）：")
        for i, p in enumerate(it.get("路径") or [], 1):
            print("  %d. [%s] %s" % (i, p.get("结果") or "?", p.get("名") or "—"))
            print("     动作：%s" % (p.get("动作") or "—"))
            print("     断言：%s ｜ 证据：%s" % (p.get("断言") or "—", p.get("证据") or "—"))
        print("\n胜出：%s" % (it.get("胜出") or "—"))
        g = it.get("固化") or {}
        print("固化：%s ｜ 策略 %s ｜ %s"
              % (g.get("落到") or "—", g.get("策略") or "—", g.get("时间") or "—"))
        if it.get("备注"):
            print("备注：%s" % it["备注"])
        return 0

    if args.report:
        text = render_report(items, args.issues)
        ok, msgs = guard(text)
        if not ok:
            print("[ERR] 零真值反证失败 —— **中止，不写盘**：")
            for k, v in msgs[:10]:
                print("   - %s：%r" % (k, v))
            return 3
        if args.out:
            with open(args.out, "w", encoding="utf-8", newline="") as fh:
                fh.write(text)
            print("已写入：%s" % args.out)
        else:
            print(text)
        return 0

    # --list（默认动作）
    show = [x for x in items if not args.status or x.get("状态") == args.status]
    st = stats(items)
    if args.json:
        print(json.dumps({"统计": st, "问题": show}, ensure_ascii=False, indent=2))
        return 0
    print("问题库：%d 条 ｜ 已固化 %s" % (st["总数"], st["固化解率"]))
    print("-" * 68)
    for x in show:
        print("  [%s] %s ｜ %s ｜ %s"
              % (x.get("状态"), x.get("id"), "、".join(x.get("站点") or []) or "—",
                 (x.get("现象") or "")[:52]))
    if not show:
        print("  （无）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
