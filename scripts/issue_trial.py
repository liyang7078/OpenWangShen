# -*- coding: utf-8 -*-
"""
问题试验器 —— 把「同一个坑的多种解法」跑一遍，用断言挑出胜者，再固化
=====================================================================
> 本模块取自 LYSKILL-apply 的真实实测闭环，公开版仅做两处裁剪：
> ① `DEFAULT_REGRESSION` 换成**本仓库内**有自测入口的脚本（私有侧的业务脚本不在本仓库）；
> ② 示例中的本机路径改写为通用路径。逻辑与断言一字未改。
它解决什么
----------
遇到一个新的网申坑，人的本能是「先试一个办法」。但试第一个就跑通、
就不再试第二个 —— 于是永远用的是**第一个想出来的**办法，不是**最好的**办法。
更糟的是：那些失败尝试直接扔掉了，下次别人（或几个月后的自己）还会再踩一遍。

本模块是自进化闭环的**第②层（多路径试验 + 断言择优）**与**第④层（半自动固化）**：

    登记（issue_registry）→ **试（本模块）** → 择优 → **固化（本模块 --promote）**

流程写死为四步，不许跳步：

  1. **多路径试**：给同一个问题登记 N 条候选路径，每条是一条可跑的命令
  2. **断言筛**：每条路径必须**全部断言通过**才算候选；缺指标的断言一律算不过（不猜）
  3. **择优**：在候选里按 `--by` 指定的指标取最大；没有 `--by` 就取第一条候选
  4. **固化**（需显式 `--promote`）：把胜出解法写成 `assets/站点策略.json` 的一条策略，
     **只追加、绝不改动既有条目**；写完立刻跑全量回归；**回归失败自动回滚**

探针契约（每条路径的命令必须遵守）
----------------------------------
  命令 stdout 的**最后一行**是一个 JSON 对象：

      {"指标": {"标签命中数": 22, "把值当标签数": 0}, "成功": true, "证据": "北森实测"}

  · `指标`  数值字典，供断言比较
  · `成功`  探针自身的成败（false 直接判该路径失败，不看断言）
  · `证据`  一句人话，会写进问题库留档

  命令里可用 `{snapshot}` 占位页面快照路径 —— 这样**离线就能复算**，
  不必真上线、也不必提交，符合「测试一律用虚构档案」的红线。

退出码：0 = 有胜出；1 = 全部路径未过断言；2 = 参数/文件错误；
        3 = 零真值反证失败（中止，不写盘）；4 = 固化后回归失败（已自动回滚）
"""

import argparse
import datetime
import json
import os
import re
import shlex
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import issue_registry as reg  # noqa: E402  —— 复用同一套登记/策略读写，绝不另写一份

OPS = {">": lambda a, b: a > b, ">=": lambda a, b: a >= b,
       "<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
       "==": lambda a, b: a == b, "!=": lambda a, b: a != b}
# 断言里的「不能漏」类条件：指标缺失一律算不过 —— 与「静默吞掉失败」同族，不许放过
ASSERT_RX = re.compile(r"^\s*(\S+?)\s*(>=|<=|==|!=|>|<)\s*(-?[\d.]+)\s*$")

# 绝对路径（含本机用户名 / 盘符目录）不许进问题库：那既是隐私面，
# 也让记录绑死在一台机器上。压成「…/文件名」——留可复现信息，去本机痕迹。
ABS_PATH_RX = re.compile(r"(?:[A-Za-z]:[\\/][^\s\"']*)|(?:/(?:Users|home)/[^\s\"']*)")


def sanitize(text):
    """把命令/证据里的绝对路径压成 …/<文件名>。

    为什么必须做：`路径` 里存的是「当时怎么试的」，天然带解释器路径与快照路径。
    原样入库会同时踩两件事 —— ① 零真值反证会把整条记录拦下（该库写不进去）；
    ② 记录绑死本机（换台机器路径全失效）。压成 basename 两边都解决。
    """
    def _sub(m):
        raw = m.group(0).rstrip("\\/")
        parts = re.split(r"[\\/]", raw)
        tail = parts[-1] if parts else ""
        return ("…/" + tail) if tail else "…"
    return ABS_PATH_RX.sub(_sub, text or "")

# 固化后必须全绿才保留策略。只列**本仓库内有自测入口**的脚本，
# 避免把没有自测入口的脚本也拉进来导致误报失败。
#
# ⚠️ 不要把 issue_trial.py 自己放进回归集：它为了验证负路径会**刻意**打印
# `[FAIL]` 行，而 `_run_regression` 一律按「出现 `[FAIL]` 即失败」判定 ——
# 放进去必然自伤假阳性（想跑得更严反而永远过不了门）。
# 更彻底的做法是让回归信号带标记位、不靠文本特征，属独立议题，本版不动。
DEFAULT_REGRESSION = (
    "scripts/issue_registry.py --selftest",
    "scripts/dist_check.py --selftest",
    "scripts/mask_pii.py --selftest",
    "scripts/check_profile.py --selftest",
    "scripts/make_sheet.py --selftest",
)


# ---------------------------------------------------------------- 工具
def _to_num(s):
    try:
        f = float(s)
    except (TypeError, ValueError):
        return s
    return int(f) if f == int(f) else f


def parse_assert(text):
    """'标签命中数 > 0' → (指标名, 比较符, 阈值)。格式不对返回 None。"""
    m = ASSERT_RX.match(text or "")
    if not m:
        return None
    return m.group(1), m.group(2), _to_num(m.group(3))


def eval_asserts(asserts, metrics):
    """逐条评断言。指标缺失**算不过**（绝不当作通过）。返回 (全过?, [说明])。"""
    notes = []
    allok = True
    for a in asserts:
        p = parse_assert(a)
        if not p:
            allok = False
            notes.append("断言格式无法解析：%r" % a)
            continue
        name, op, want = p
        if name not in (metrics or {}):
            allok = False
            notes.append("指标缺失「%s」→ 算不过（不猜）" % name)
            continue
        got = _to_num(metrics[name])
        try:
            ok = OPS[op](got, want)
        except TypeError:
            ok = False
            notes.append("指标「%s」类型不可比：%r %s %r" % (name, got, op, want))
            continue
        if not ok:
            allok = False
            notes.append("断言未过：%s = %r，要求 %s %r" % (name, got, op, want))
    return allok, notes


def parse_probe_output(stdout):
    """取 stdout 里**最后一个**能解析成对象的 JSON 行。解析不出返回 None。"""
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except Exception:                                  # noqa: BLE001
            continue
        if isinstance(d, dict):
            return d
    return None


def _argv(cmd):
    """命令 → argv。**首参是 python* 或 *.py 都补上本机解释器。**

    踩过的坑：回归集写成 `scripts/probe.py --selftest`（不带 python 前缀），
    只在「首参等于 python」时替换时，`toks[0]` 就是个 .py 路径，
    subprocess 直接去执行它 → Windows 报 WinError 193「不是有效的 Win32 应用程序」。
    所以判据必须是「首参是不是脚本」，不是「首参是不是字面 python」。
    """
    toks = shlex.split(cmd, posix=True)
    if not toks:
        return toks
    head = toks[0].lower()
    if head in ("python", "python3", "py"):
        toks[0] = sys.executable
    elif head.endswith(".py"):
        toks.insert(0, sys.executable)
    return toks


def run_probe(cmd, snapshot="", timeout=180, cwd=None):
    """跑一条候选路径。返回 (结果 dict, 说明)。"""
    line = cmd.replace("{snapshot}", snapshot or "")
    try:
        toks = _argv(line)
    except ValueError as exc:
        return None, "命令无法解析：%s" % exc
    if not toks:
        return None, "命令为空"
    t0 = datetime.datetime.now()
    try:
        p = subprocess.run(toks, capture_output=True, text=True, timeout=timeout,
                           cwd=cwd or os.path.dirname(HERE), encoding="utf-8",
                           errors="replace")
    except subprocess.TimeoutExpired:
        return None, "超时（%ds）" % timeout
    except Exception as exc:                               # noqa: BLE001
        return None, "无法执行：%s" % exc
    cost = (datetime.datetime.now() - t0).total_seconds()
    out = (p.stdout or "") + "\n" + (p.stderr or "")
    d = parse_probe_output(p.stdout)
    if d is None:
        return None, "探针没输出合法 JSON（exit=%s）：%s" % (p.returncode, out.strip()[-160:])
    d["_耗时"] = round(cost, 1)
    d["_exit"] = p.returncode
    return d, ""


# ---------------------------------------------------------------- 试验
def trial(issue, paths, asserts, by="", snapshot="", timeout=180, runner=run_probe):
    """逐条试。返回 (结果列表, 胜出项 or None)。**失败路径同样完整留档。**"""
    results = []
    for name, cmd in paths:
        d, err = runner(cmd, snapshot=snapshot, timeout=timeout)
        rec = {"名": name, "命令": sanitize(cmd), "断言": list(asserts)}
        if d is None:
            rec.update({"结果": "失败", "证据": sanitize(err), "指标": {}})
            results.append(rec)
            continue
        metrics = d.get("指标") or {}
        evidence = sanitize(str(d.get("证据") or ""))
        if not d.get("成功"):
            rec.update({"结果": "失败", "证据": "探针自报未成功" + ("：" + evidence if evidence else ""),
                        "指标": metrics, "耗时": d.get("_耗时")})
            results.append(rec)
            continue
        allok, notes = eval_asserts(asserts, metrics)
        rec.update({"结果": "胜出候选" if allok else "失败",
                    "证据": evidence if allok else "；".join(notes)[:200],
                    "指标": metrics, "耗时": d.get("_耗时")})
        results.append(rec)

    cand = [r for r in results if r["结果"] == "胜出候选"]
    winner = None
    if cand:
        if by:
            def key(r):
                v = (r.get("指标") or {}).get(by)
                return _to_num(v) if isinstance(v, (int, float)) else float("-inf")
            winner = max(cand, key=key)
            if (winner.get("指标") or {}).get(by) is None:
                winner = cand[0]                # --by 指标不存在 → 退回第一条候选，不静默
        else:
            winner = cand[0]
    for r in results:
        if r is winner:
            r["结果"] = "胜出"
    return results, winner


# ---------------------------------------------------------------- 固化
def _stamp():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def promote(issue, winner, strategy, spath, regress=(), runner=None, cwd=None):
    """把胜出解法写成一条策略。**只追加**；**先过完所有门才落盘一次**。

    顺序（每一步都不过就返回，**不产生任何副作用**）：
      ① 有胜出 ② 有动作+指纹 ③ 零真值 ④ 只追加（内存里比） ⑤ 回归全绿 → 才写盘

    为什么把回归放在写盘**之前**：早先的写法是先写盘、再跑回归、失败再还原备份，
    结果「策略库原本不存在」时**没有备份可还原** —— 那条「回归：运行中」的幽灵策略
    就留在了库里。这与「静默吞掉失败」同族：库里声称有一条策略，而它其实没过门。
    改成「先试后写」后，失败路径天然无痕，不需要回滚。

    返回 (rc, 说明)。rc: 0 成功 / 2 参数错 / 3 零真值或只追加反证失败 / 4 回归失败
    """
    if not winner:
        return 2, "[ERR] 没有胜出路径，不能固化（没试赢不许进策略库）"
    if winner.get("结果") != "胜出":
        return 2, "[ERR] 只能固化标记为「胜出」的路径"

    sdata = reg.load_strategies(spath)
    old = list(sdata.get("策略") or [])
    old_ids = [x.get("id") for x in old]

    sid = strategy.get("id") or ""
    if not sid:
        n = 0
        for i in old_ids:
            m = re.match(r"^P-(\d+)$", str(i or ""))
            if m:
                n = max(n, int(m.group(1)))
        sid = "P-%03d" % (n + 1)
    if sid in old_ids:
        return 2, "[ERR] 策略 id 已存在：%s" % sid

    if not regress:
        regress = DEFAULT_REGRESSION

    entry = {
        "id": sid,
        "来源问题": issue.get("id"),
        "站点": strategy.get("站点") or issue.get("站点") or ["*"],
        "指纹": strategy.get("指纹") or issue.get("指纹") or [],
        "现象": strategy.get("现象") or (issue.get("现象") or "")[:90],
        "动作": strategy.get("动作") or "",
        "断言": strategy.get("断言") or winner.get("断言") or [],
        "固化": {"时间": _stamp()[:10], "落到": strategy.get("落到") or "（待填）",
                 "回归": "通过（%d 项全绿）" % len(regress)},
        "证据": winner.get("证据") or "",
    }
    if not entry["动作"]:
        return 2, "[ERR] 必须给 --动作（胜出解法要做什么）"
    if not entry["指纹"]:
        return 2, "[ERR] 必须给 --指纹（否则引擎无法命中这条策略）"

    # ---------- 门①②③④：全在内存里过，一个字节都不落盘 ----------
    new = old + [entry]
    payload = dict(sdata)
    payload["策略"] = new
    ok, msgs = reg.guard(json.dumps(payload, ensure_ascii=False), payload)
    if not ok:
        for k, v in msgs[:10]:
            print("   - %s：%r" % (k, v))
        return 3, "[ERR] 零真值反证失败 —— 中止，不写盘"
    if [x.get("id") for x in new] != old_ids + [sid]:
        return 3, "[ERR] 只追加反证失败（内存里既有条目就被改动了）—— 中止，不写盘"

    # ---------- 门⑤：回归。**跑在写盘之前**，失败即无痕 ----------
    print("\n[..] 跑全量回归（%d 项）—— 全绿才落盘" % len(regress))
    fails = []
    for cmd in regress:
        if runner is not None:
            d, err = runner(cmd, snapshot="", timeout=300)
            good = d is not None and bool(d.get("成功"))
            tail = err or str((d or {}).get("证据") or "")
        else:
            good, tail = _run_regression(cmd, cwd)
        print("     [%s] %s%s" % ("OK" if good else "FAIL", cmd,
                                  "" if good else "  ← %s" % tail[:120]))
        if not good:
            fails.append(cmd)
    if fails:
        return 4, ("[ERR] 回归失败 %d 项 → **策略未落盘，无痕退出**：%s"
                   % (len(fails), "；".join(fails)))

    # ---------- 全绿 → 一次性落盘 ----------
    bak = ""
    if os.path.isfile(spath):
        bak = spath + ".bak" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        with open(spath, "r", encoding="utf-8") as src, \
                open(bak, "w", encoding="utf-8", newline="") as dst:
            dst.write(src.read())
    with open(spath, "w", encoding="utf-8", newline="") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    # ---------- 落盘后回读复核（写坏了能发现） ----------
    back = reg.load_strategies(spath).get("策略") or []
    if [x.get("id") for x in back] != old_ids + [sid]:
        _restore(spath, bak)
        return 3, "[ERR] 落盘复核失败（既有条目被改动）—— 已回滚"
    print("[OK] 策略已落盘：%s（既有 %d 条未改动）" % (sid, len(old)))
    print("     动作：%s" % entry["动作"])
    print("     备份：%s" % (bak or "（原本无此文件）"))
    return 0, sid


def _restore(spath, bak):
    if bak and os.path.isfile(bak):
        with open(bak, "r", encoding="utf-8") as src, \
                open(spath, "w", encoding="utf-8", newline="") as dst:
            dst.write(src.read())
        print("[..] 已从备份还原：%s" % os.path.basename(bak))


def _run_regression(cmd, cwd=None):
    toks = _argv(cmd)
    abscwd = cwd or os.path.dirname(HERE)
    try:
        p = subprocess.run(toks, capture_output=True, text=True, timeout=300,
                           cwd=abscwd, encoding="utf-8", errors="replace")
    except Exception as exc:                               # noqa: BLE001
        return False, "无法执行：%s" % exc
    blob = (p.stdout or "") + (p.stderr or "")
    if p.returncode != 0:
        return False, "exit=%s" % p.returncode
    if "失败" in blob and re.search(r"自测结果：\d+ 通过 / (\d+) 失败", blob):
        m = re.search(r"自测结果：\d+ 通过 / (\d+) 失败", blob)
        if int(m.group(1)) != 0:
            return False, "自测有失败：%s" % m.group(0)
    if "[FAIL]" in blob:
        return False, "存在 [FAIL] 断言"
    return True, ""


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

    # ---- 断言求值 ----
    check("① 断言过：命中 22 > 0", eval_asserts(["标签命中数 > 0"], {"标签命中数": 22})[0])
    check("② 断言不过：命中 0 > 0", not eval_asserts(["标签命中数 > 0"], {"标签命中数": 0})[0])
    check("③ 指标缺失 → 算不过（绝不放过）",
          not eval_asserts(["标签命中数 > 0"], {})[0])
    check("④ 缺指标的说明写出来了",
          "指标缺失" in eval_asserts(["x > 0"], {})[1][0])
    check("⑤ 多断言：一条不过即不过",
          not eval_asserts(["a > 0", "b == 0"], {"a": 1, "b": 1})[0])
    check("⑥ 断言格式错 → 不过",
          not eval_asserts(["随便写点什么"], {"a": 1})[0])
    check("⑦ 比较符全集可用",
          all(eval_asserts(["v %s 1" % op], {"v": 1})[0] for op in (">=", "<=", "==")))
    check("⑧ 负数/小数阈值解析",
          parse_assert("错误数 <= -0.5") == ("错误数", "<=", -0.5),
          parse_assert("错误数 <= -0.5"))
    check("⑨ 探针输出取最后一行 JSON",
          parse_probe_output("日志行\n{\"指标\":{\"a\":1}}\n")["指标"]["a"] == 1)
    check("⑩ 非 JSON 输出 → None", parse_probe_output("全是日志") is None)

    # ---- 命令脱敏（端到端演示时发现的真缺陷：绝对路径会让整条记录被反证拦下）----
    _raw = "C:/Users/example/py/python.exe D:/某目录/probe.py --mode a"
    check("⑪a 绝对路径压成 …/文件名（去盘符与本机用户名）",
          sanitize(_raw) == "…/python.exe …/probe.py --mode a", sanitize(_raw))
    check("⑪b 相对路径原样不动",
          sanitize("python scripts/probe.py --mode a") == "python scripts/probe.py --mode a")
    check("⑪c /Users//home 形态也被压掉",
          sanitize("/Users/bob/x.py") == "…/x.py", sanitize("/Users/bob/x.py"))
    check("⑪d 脱敏后不再触发零真值反证",
          not reg.pii_scan(sanitize(_raw)), reg.pii_scan(sanitize(_raw)))

    # ---- argv 组装（端到端演示时发现的真缺陷：回归集不带 python 前缀 → WinError 193）----
    check("⑫a 首参形如 scripts/x.py 也补解释器（而不是直接执行 .py）",
          _argv("scripts/probe.py --selftest") ==
          [sys.executable, "scripts/probe.py", "--selftest"],
          _argv("scripts/probe.py --selftest")[:1])
    check("⑫b 已带 python 前缀 → 换成本机解释器",
          _argv("python scripts/a.py --x") == [sys.executable, "scripts/a.py", "--x"])
    check("⑫c 非脚本命令不动",
          _argv("git status") == ["git", "status"])
    check("⑫d 默认回归集每一条都能组装出解释器",
          all(_argv(c)[0] == sys.executable for c in DEFAULT_REGRESSION),
          [c for c in DEFAULT_REGRESSION if _argv(c)[0] != sys.executable])

    # ---- 择优 ----
    def fake_a(cmd, snapshot="", timeout=0):
        return {"指标": {"命中": 3, "错": 0}, "成功": True, "证据": "虚构A"}, ""

    def fake_b(cmd, snapshot="", timeout=0):
        return {"指标": {"命中": 9, "错": 1}, "成功": True, "证据": "虚构B"}, ""

    def fake_c(cmd, snapshot="", timeout=0):
        return None, "探针崩了"

    res, w = trial({}, [("A", "a"), ("B", "b"), ("C", "c")],
                   ["命中 > 0", "错 == 0"], by="命中", runner=fake_a)
    check("⑪ 全过 → 第一条胜出（未给 --by 之外的干扰）", w and w["名"] == "A")
    res, w = trial({}, [("A", "a"), ("B", "b"), ("C", "c")],
                   ["命中 > 0"], by="命中",
                   runner=lambda c, snapshot="", timeout=0: (fake_a if c == "a"
                                                            else fake_b if c == "b"
                                                            else fake_c)(c))
    check("⑫ 按 --by 指标取最大（B 的命中 9 > A 的 3）", w and w["名"] == "B", w)
    check("⑬ 失败路径也完整留档（3 条都在）", len(res) == 3)
    check("⑭ 崩掉的路径记为失败并带原因",
          any(r["名"] == "C" and r["结果"] == "失败" and "崩" in (r["证据"] or "")
              for r in res))
    res, w = trial({}, [("B", "b")], ["命中 > 0", "错 == 0"], by="命中",
                   runner=lambda c, snapshot="", timeout=0: fake_b(c))
    check("⑮ 断言有一条不过 → 无胜出", w is None)

    # ---- 固化：只追加 + 回归门 + 回滚 ----
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix="trial_")
    try:
        spath = os.path.join(tmp, "站点策略.json")
        seed = {"_说明": "", "策略": [{"id": "P-001", "站点": ["*"], "指纹": ["x"],
                                      "动作": "既有策略，不得被动"}]}
        with open(spath, "w", encoding="utf-8", newline="") as fh:
            json.dump(seed, fh, ensure_ascii=False)

        issue = {"id": "Q-20260922-001", "站点": ["北森"], "指纹": ["file-hidden"],
                 "现象": "虚构：上传控件被隐藏"}
        win = {"结果": "胜出", "名": "豁免可见性", "断言": ["上传控件数 > 0"],
               "证据": "虚构证据：找回 12 个"}
        good = lambda c, snapshot="", timeout=0: ({"成功": True, "证据": "还自测结果：5 通过 / 0 失败"}, "")
        rc, sid = promote(issue, win, {"动作": "对 file 豁免可见性检查", "指纹": ["file-hidden"],
                                       "站点": ["北森"]}, spath,
                          regress=["fake --selftest"], runner=good)
        check("⑯ 固化成功且 id 自动续号 P-002", rc == 0 and sid == "P-002", (rc, sid))
        back = reg.load_strategies(spath)["策略"]
        check("⑰ 只追加：既有 1 条 + 新 1 条 = 2", len(back) == 2, len(back))
        check("⑱ 既有条目逐字未变",
              back[0]["动作"] == "既有策略，不得被动" and back[0]["id"] == "P-001")
        check("⑲ 新条目带来源问题/回归结果",
              back[1].get("来源问题") == "Q-20260922-001"
              and "通过" in (back[1].get("固化") or {}).get("回归", ""),
              back[1].get("固化"))

        # 回归失败 → 不落盘（写盘在回归之后，失败天然无痕）
        bad_run = lambda c, snapshot="", timeout=0: (None, "exit=1")
        rc, msg = promote(issue, win, {"动作": "又一个解法", "指纹": ["file-hidden"]},
                          spath, regress=["fake --selftest"], runner=bad_run)
        check("⑳ 回归失败 → rc=4 且明确说了「未落盘」",
              rc == 4 and "未落盘" in msg, (rc, msg))
        check("㉑ 回归失败后策略数仍为 2（未污染）",
              len(reg.load_strategies(spath)["策略"]) == 2)

        # 这条是端到端演示时发现的真缺陷：原本无策略库时，旧写法「先写盘再回滚」
        # 没有备份可还原 → 留下一条「回归：运行中」的幽灵策略。
        ghost = os.path.join(tmp, "ghost", "站点策略.json")
        os.makedirs(os.path.dirname(ghost), exist_ok=True)
        rc, msg = promote(issue, win, {"动作": "x", "指纹": ["file-hidden"]},
                          ghost, regress=["fake --selftest"], runner=bad_run)
        check("㉑b 原本无策略库 + 回归失败 → **文件不该被创建**（无幽灵策略）",
              rc == 4 and not os.path.isfile(ghost), (rc, os.path.isfile(ghost)))

        # 没胜出 / 缺动作 / 缺指纹 → 一律不许固化
        check("㉒ 无胜出 → 拒绝固化", promote(issue, None, {"动作": "x"}, spath,
                                       regress=[], runner=good)[0] == 2)
        check("㉓ 缺「动作」→ 拒绝固化",
              promote(issue, win, {"指纹": ["file-hidden"]}, spath,
                      regress=[], runner=good)[0] == 2)
        check("㉔a 不传指纹 → 从问题条目继承（file-hidden）",
              promote(issue, win, {"动作": "x"}, spath, regress=["fake --selftest"],
                      runner=good)[0] == 0)
        back2 = reg.load_strategies(spath)["策略"][-1]
        check("㉔b 继承来的指纹确实写进去了",
              back2.get("指纹") == ["file-hidden"], back2.get("指纹"))
        check("㉔c 问题条目也没指纹 → 拒绝固化（否则引擎无法命中）",
              promote({"id": "Q-x", "站点": ["*"]}, win, {"动作": "x"}, spath,
                      regress=["fake --selftest"], runner=good)[0] == 2)

        # 零真值：动作里掺手机号 → 中止不写盘
        before = open(spath, encoding="utf-8").read()
        rc, msg = promote(issue, win, {"动作": "打 13800000000 这个号", "指纹": ["x"]},
                          spath, regress=[], runner=good)
        check("㉕ 零真值反证：掺手机号 → 中止不写盘",
              rc == 3 and open(spath, encoding="utf-8").read() == before, rc)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- 真库一致性 ----
    sd = reg.load_strategies()
    check("㉖ 真策略库仍可读（固化没写坏它）", bool(sd.get("策略")))
    check("㉗ 真策略库零真值", not reg.pii_scan(json.dumps(sd, ensure_ascii=False)))

    print("\n自测结果：%d 通过 / %d 失败" % (ok[0], bad[0]))
    return 0 if bad[0] == 0 else 1


# ---------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="问题试验器：多路径试跑 → 断言择优 → 固化进策略库（零真值）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python scripts/issue_trial.py --issue Q-20260922-013 \\\n"
               "      --path \"几何邻居法=python scripts/probe.py --mode geometric\" \\\n"
               "      --path \"向上找祖先=python scripts/probe.py --mode ancestor\" \\\n"
               "      --assert \"标签命中数 > 0\" --assert \"值当标签数 == 0\" \\\n"
               "      --by 标签命中数 --snapshot page.json\n\n"
               "  加 --promote --动作 \"...\" --指纹 file-hidden 即固化（会跑全量回归）\n")
    ap.add_argument("--issue", default="", help="问题 id（须已在问题库里）")
    ap.add_argument("--path", action="append", default=[],
                    help="候选路径，格式 名=命令（可多次；命令里 {snapshot} 会被替换）")
    ap.add_argument("--assert", dest="asserts", action="append", default=[],
                    help='断言，格式 "指标名 > 0"（可多次；全部通过才算候选）')
    ap.add_argument("--by", default="", help="择优指标名（默认取第一条候选）")
    ap.add_argument("--snapshot", default="", help="页面快照路径（替换 {snapshot}）")
    ap.add_argument("--timeout", type=int, default=180, help="单条路径超时秒数")
    ap.add_argument("--promote", action="store_true", help="把胜出解法固化进策略库")
    ap.add_argument("--动作", default="", help="固化时：这条策略要做什么")
    ap.add_argument("--指纹", default="", help="固化时：逗号分隔的控件指纹")
    ap.add_argument("--站点", default="", help="固化时：逗号分隔的站点（默认 *）")
    ap.add_argument("--落到", default="", help="固化时：落到哪个文件/函数")
    ap.add_argument("--regress", action="append", default=[],
                    help="固化后的回归命令（可多次；默认跑内置全量回归集）")
    ap.add_argument("--issues", default=reg.ISSUES, help="问题库路径")
    ap.add_argument("--strategies", default=reg.STRATEGIES, help="策略库路径")
    ap.add_argument("--dry-run", action="store_true", help="只试不写（不更新问题库/策略库）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出试验结果")
    ap.add_argument("--selftest", action="store_true", help="自测")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()

    if not args.issue or not args.path:
        ap.print_help()
        print("\n[ERR] 需要 --issue 与至少一条 --path。")
        return 2
    if not args.asserts:
        print("[ERR] 至少给一条 --assert —— 没有断言就无法择优，只能凭感觉。")
        return 2

    items, broken = reg.load_issues(args.issues)
    issue = reg.find(items, args.issue)
    if not issue:
        print("[ERR] 问题库里找不到 %s。先用 issue_registry.py --add 登记。" % args.issue)
        return 2

    paths = []
    for spec in args.path:
        if "=" not in spec:
            print("[ERR] --path 格式应为 名=命令：%r" % spec)
            return 2
        nm, cmd = spec.split("=", 1)
        paths.append((nm.strip(), cmd.strip()))

    print("=" * 68)
    print("试验：%s ｜ %s" % (issue.get("id"), (issue.get("现象") or "")[:52]))
    print("候选路径 %d 条 ｜ 断言 %d 条 ｜ 择优指标 %s"
          % (len(paths), len(args.asserts), args.by or "（第一条候选）"))
    print("=" * 68)
    for nm, cmd in paths:
        d, err = run_probe(cmd, snapshot=args.snapshot, timeout=args.timeout)
        tag = "（预检不可执行）" if d is None else "（预检可执行）"
        print("  · %s %s" % (nm, tag))
        if err:
            print("      %s" % err[:140])

    results, winner = trial(issue, paths, args.asserts, by=args.by,
                            snapshot=args.snapshot, timeout=args.timeout)

    for r in results:
        print("\n[%s] %s" % (r["结果"], r["名"]))
        print("     指标：%s" % (r.get("指标") or {}))
        print("     证据：%s" % (r.get("证据") or "—"))
    if not winner:
        print("\n无胜出：%d 条路径都没过全部断言。**不要固化** —— 问题保持「试验中」。" % len(results))
        if not args.dry_run:
            issue["路径"] = (issue.get("路径") or []) + results
            issue["状态"] = "试验中"
            issue["备注"] = "本轮 %d 条路径全部未过断言（%s）" % (
                len(results), datetime.datetime.now().strftime("%Y-%m-%d"))
            okk, msgs, _b = reg.save_issues(items, args.issues)
            if not okk:
                print("[ERR] 零真值反证失败，问题库未写：%s" % msgs[:3])
                return 3
            print("[OK] 失败路径已留档到问题库（下次不必重试同样的路）")
        return 1

    print("\n胜出：%s（%s）" % (winner["名"], winner.get("证据") or ""))
    if not args.promote:
        print("提示：加 --promote 可把它固化成策略（会跑全量回归，失败自动回滚）。")
        if not args.dry_run:
            issue["路径"] = (issue.get("路径") or []) + results
            issue["状态"] = "已胜出"
            issue["胜出"] = winner["名"]
            okk, msgs, _b = reg.save_issues(items, args.issues)
            if not okk:
                print("[ERR] 零真值反证失败，问题库未写：%s" % msgs[:3])
                return 3
            print("[OK] 问题库已更新：状态 → 已胜出")
        return 0

    if args.dry_run:
        print("[DRY-RUN] 不写任何文件。")
        return 0

    strategy = {"动作": args.动作,
                "站点": [x for x in args.站点.split(",") if x.strip()],
                "指纹": [x for x in args.指纹.split(",") if x.strip()],
                "落到": args.落到}
    rc, msg = promote(issue, winner, strategy, args.strategies,
                      regress=args.regress)
    if rc != 0:
        print(msg)
        return rc

    issue["路径"] = (issue.get("路径") or []) + results
    issue["状态"] = "已固化"
    issue["胜出"] = winner["名"]
    issue["固化"] = {"策略": msg, "落到": args.落到, "时间": _stamp()[:10]}
    okk, msgs, _b = reg.save_issues(items, args.issues)
    if not okk:
        print("[ERR] 零真值反证失败，问题库未写（策略已生效）：%s" % msgs[:3])
        return 3
    print("[OK] 问题库已更新：%s → 已固化" % issue.get("id"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
