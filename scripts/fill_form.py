#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fill_form.py · 网申表单自动填写
================================
用浏览器打开岗位页 → 自动识别表单字段 → 从本机档案取值写入 → 截图 → 停在提交前。

**三条不可越过的线**
  1. 绝不点提交（submit / 保存并提交 / 确认投递 一律不碰）
  2. 绝不填密码框、绝不碰验证码（识别到即标记为「人工门」）
  3. 绝不把真值打印到终端（真值只在本进程内存里流转，直接写进浏览器）

依赖
----
  playwright（可选）：pip install playwright && playwright install chromium
  若未安装 → 自动降级提示改用 make_sheet.py 生成填写清单。

每站预算
--------
  默认 **300 秒**（`--budget`，0 = 不限）。到点没跑完，或识别率低于 20%，
  脚本会**停手**并给出一条「改用可复制清单页」的路：

      python scripts/wangshen.py sheethtml --profile <档案> --labels-file 页面字段.txt

  这不是失败，是**提前止损** —— 与其在一个不认的表单上耗到底，不如把已经
  拿到的交给你，你照着粘。加 `--html <路径>` 可在停手时直接生成那张可复制页。

退出码
------
  0 = 正常跑完（已停在提交前）   4 = 缺 playwright   5 = **提前停手**（见上）

用法
----
  python scripts/fill_form.py --profile profile.json --url https://xxx --dry-run
  python scripts/fill_form.py --profile profile.json --url https://xxx \
      --channel msedge --screenshot out.png --out 填表报告.json

许可：MIT
"""

import argparse
import datetime
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core  # noqa: E402
import issue_registry as reg  # noqa: E402

# 视为「人工门」的字段类型：程序永不代填
HUMAN_GATE_TYPES = {"password", "file"}
CAPTCHA_HINTS = ("验证码", "captcha", "verify code", "短信验证", "动态码")


# --------------------------------------------------------------------------
# 控件指纹推导 → 站点策略匹配（2026-10-15 接线）
# --------------------------------------------------------------------------
# 为什么按「指纹」而不是按「站点」组织经验
# ----------------------------------------
# 「portal 到 body」「上传框是隐藏的」这类坑**不认站点**：今天是 A 站，明天是
# B 站，形态一模一样。按站点记等于同一件事记 N 遍；按指纹记，**一次登记换 N 站复用**。
# 这正是「作者自己实测的网站太少」的解法。
def derive_fingerprints(fields, url="", meta=None):
    """
    从**页面实测结构**推导控件指纹。返回 (指纹集合, 依据说明列表)。

    只推导「词表里声明过」的指纹，避免造出无人认领的黑话。
    """
    fps, why = set(), []
    if not fields:
        return fps, why

    no_label = [f for f in fields if not (f.get("label") or "").strip()]
    if no_label:
        fps.add("label-missing")
        why.append("label-missing：%d 个控件取不到标签" % len(no_label))
        if any(not f.get("inForm") for f in no_label):
            fps.add("portal-to-body")
            why.append("portal-to-body：无标签控件不在任何 <form> 内（祖先链已断）")

    hidden_files = [f for f in fields
                    if f.get("type") == "file" and not f.get("visible")]
    if hidden_files:
        fps.add("file-hidden")
        why.append("file-hidden：%d 个上传框不可见" % len(hidden_files))

    if any(f.get("readonly") for f in fields):
        fps.add("readonly-cascader")
        why.append("readonly-cascader：存在只读控件（自绘日历/级联）")

    m = meta or {}
    if m.get("假成功数"):
        fps.add("value-equals-label")
        why.append("value-equals-label：%d 个控件写入后值等于标签" % m["假成功数"])
    if m.get("别名救回数"):
        fps.add("dropdown-value-mismatch")
        why.append("dropdown-value-mismatch：%d 个下拉靠值域别名才选中" % m["别名救回数"])
    if m.get("区段标题数"):
        fps.add("section-title-as-label")
        why.append("section-title-as-label：%d 处标签命中区段标题已被弃用"
                   % m["区段标题数"])
    return fps, why


def site_of(url):
    """URL → 站点标识（主机名，去掉 www.）。"""
    try:
        from urllib.parse import urlsplit
        h = (urlsplit(url).hostname or "").lower()
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""


def normalize_cdp(v):
    """
    `--connect 9222` / `--connect http://127.0.0.1:9222` / `--connect ws://…` 都收。
    返回可用的 CDP 端点；空串 = 不接管。
    """
    s = str(v or "").strip()
    if not s:
        return ""
    if s.isdigit():
        return "http://127.0.0.1:%s" % s
    if s.startswith(("http://", "https://", "ws://", "wss://")):
        return s
    return ""


def _pick_page(ctx, url):
    """
    在已开着的浏览器里挑一个页签复用。
    优先挑「已经在目标站点上」的 —— 这样不会多开一个标签、也不会丢掉
    考生已经填了一半的内容（站点策略库 P-011：page_key 重试）。
    """
    host = site_of(url)
    pages = []
    for p in ctx.pages:
        try:
            pages.append(p)
        except Exception:
            continue
    if not pages:
        return None
    if host:
        for p in pages:
            try:
                if host in (p.url or ""):
                    return p
            except Exception:
                continue
    for p in pages:
        try:
            if "about:blank" not in (p.url or "") and not (p.url or "").startswith("devtools"):
                return p
        except Exception:
            continue
    return pages[0]


def match_strategies(url, fps):
    """按站点 + 指纹查策略库。返回命中列表（可能为空 —— 空就是空，不凑）。"""
    try:
        return reg.match(site_of(url), sorted(fps))
    except Exception:
        return []


# --------------------------------------------------------------------------
# 页面扫描：提取表单字段
# --------------------------------------------------------------------------
SCAN_JS = r"""
() => {
  const SKIP = new Set(['hidden', 'submit', 'button', 'image', 'reset']);

  // 区段标题不是字段名（站点策略库 P-008）：
  // 「一、教育经历」这种是分组头，被当成标签会让整组的字段全部映射错。
  function isSectionTitle(node, txt) {
    const tag = (node.tagName || '').toUpperCase();
    if (/^H[1-6]$/.test(tag) || tag === 'LEGEND' || tag === 'FIELDSET') return true;
    if (node.getAttribute && node.getAttribute('role') === 'heading') return true;
    return /^[（(]?[一二三四五六七八九十\d]{1,3}[）)、.．]/.test(txt || '');
  }

  // placeholder 里的「请选择/请输入」是提示，不是字段名（否则「请选择」会撞上「性别」）
  function stripHint(t) {
    return (t || '').replace(/^\s*(请输入|请选择|请填写|请上传|请注明|请录入|请提供)/, '').trim();
  }

  function visible(el) {
    return !!(el.offsetParent !== null ||
              (el.getClientRects && el.getClientRects().length));
  }

  // 候选标签节点先算一次，避免每个控件都全页扫一遍
  const CAND = [...document.querySelectorAll('label,span,div,p,td,th,dt,dd')];

  // 几何邻居法（站点策略库 P-001）：组件库把控件 portal 到 body 之后，
  // DOM 祖先链断裂，「向上找标签」会全部失效 —— 改按**视觉位置**就近取标签。
  function geometricLabel(el) {
    const r = el.getBoundingClientRect();
    if (!r.width && !r.height) return '';
    let best = '', bestDx = 1e9;
    for (const n of CAND) {
      if (n.contains(el) || el.contains(n)) continue;
      if (n.querySelector('input,select,textarea')) continue;
      const t = (n.innerText || '').trim();
      if (!t || t.length > 24) continue;
      if (isSectionTitle(n, t)) continue;
      const nr = n.getBoundingClientRect();
      if (!nr.width && !nr.height) continue;
      const dy = Math.abs((nr.top + nr.bottom) / 2 - (r.top + r.bottom) / 2);
      if (dy > 8) continue;                    // 必须同一视觉行
      const dx = r.left - nr.right;            // 且位于控件左侧
      if (dx < -4 || dx > 280) continue;
      if (dx < bestDx) { bestDx = dx; best = t; }
    }
    return best;
  }

  function labelOf(el) {
    // 1) 原生 labels
    if (el.labels && el.labels.length) {
      const t = (el.labels[0].innerText || '').trim();
      if (t && !isSectionTitle(el.labels[0], t)) return t;
    }
    // 2) label[for=id]
    if (el.id) {
      const l = document.querySelector('label[for="' + el.id + '"]');
      if (l && l.innerText) {
        const t = l.innerText.trim();
        if (t && !isSectionTitle(l, t)) return t;
      }
    }
    // 3) aria-label / aria-labelledby
    const aria = el.getAttribute('aria-label');
    if (aria && aria.trim()) return aria.trim();
    const alb = el.getAttribute('aria-labelledby');
    if (alb) {
      const parts = alb.split(/\s+/).map(id => {
        const n = document.getElementById(id);
        return n ? (n.innerText || '').trim() : '';
      }).filter(Boolean);
      if (parts.length) return parts.join('');
    }
    // 4) placeholder（剥掉「请输入/请选择」提示前缀）
    const ph = stripHint(el.getAttribute('placeholder'));
    if (ph) return ph;
    // 5) 向上找容器里第一段短文本（跳过区段标题）
    let node = el.parentElement, depth = 0;
    while (node && depth < 3) {
      const t = (node.innerText || '').trim();
      if (t && t.length <= 40 && !isSectionTitle(node, t)) return t;
      node = node.parentElement; depth++;
    }
    // 6) 几何兜底
    return geometricLabel(el);
  }

  // 单选/复选专用：选项 label 里只有选项文字（"男"/"是"），
  // 真正的问题是「这组叫什么」。向上找容器里第一个**不含输入控件**的 label —— 那才是字段名。
  function groupLabelOf(el) {
    let node = el.parentElement, depth = 0;
    while (node && depth < 4) {
      const labs = [...node.querySelectorAll('label')]
        .filter(l => !l.querySelector('input,select,textarea'));
      for (const l of labs) {
        const t = (l.innerText || '').trim();
        if (t && t.length <= 40 && !isSectionTitle(l, t)) return t;
      }
      const al = node.getAttribute && node.getAttribute('aria-label');
      if (al) return al.trim();
      node = node.parentElement; depth++;
    }
    return geometricLabel(el);
  }

  const out = [];
  let i = 0;
  document.querySelectorAll('input,select,textarea').forEach(el => {
    const t = (el.type || el.tagName).toLowerCase();
    if (SKIP.has(t)) return;
    const idx = i++;
    el.setAttribute('data-ows-idx', String(idx));

    let label = '';
    let viaGroup = false;
    if (t === 'radio' || t === 'checkbox') {
      label = groupLabelOf(el);
      viaGroup = !!label;
    }
    if (!label) label = labelOf(el);

    let options = null;
    if (el.tagName.toLowerCase() === 'select') {
      options = [...el.options].map(o => (o.text || '').trim()).filter(Boolean);
    }
    out.push({
      idx: idx,
      tag: el.tagName.toLowerCase(),
      type: t,
      label: label || el.name || el.id || '',
      name: el.name || '',
      id: el.id || '',
      placeholder: el.getAttribute('placeholder') || '',
      options: options,
      visible: visible(el),
      inForm: !!el.closest('form'),
      readonly: !!el.readOnly,
      viaGroup: viaGroup
    });
  });
  return out;
}
"""


def scan_page(page):
    """扫描页面表单字段。"""
    return page.evaluate(SCAN_JS)


def is_captcha(field):
    blob = " ".join([field.get("label", ""), field.get("name", ""),
                     field.get("id", ""), field.get("placeholder", "")]).lower()
    return any(h in blob for h in CAPTCHA_HINTS)


def plan_mapping(fields, labels, profile, index, rules):
    """
    规划：页面字段 → 档案键 → 值。
    返回 (plan, human_gates, unmatched, notes)
      plan  = [{idx, label, key, type, value, how, 状态}]
      notes = {"歧义拒写": [...], "只读控件": [...], "别名救回": [...]}
    """
    plan, gates, unmatched = [], [], []
    notes = {"歧义拒写": [], "只读控件": [], "别名救回": []}

    # 逐字段映射（用 label，退化用 placeholder）
    for f in fields:
        lab = f.get("label") or f.get("placeholder") or ""

        # 人工门优先判定：密码框 / 验证码 / 附件上传 —— 无论能否映射，程序都不碰
        if f["type"] in HUMAN_GATE_TYPES or is_captcha(f):
            key_now, _ = core.map_one(lab, index, rules)
            gates.append({"idx": f["idx"], "label": lab, "key": key_now,
                          "reason": "密码框" if f["type"] == "password" else
                                    ("附件上传" if f["type"] == "file" else "疑为验证码")})
            continue

        # 严格映射：并列最长 ⇒ 歧义 ⇒ 拒写（交回人工），不再掷骰子
        key, how = core.map_one_strict(lab, index, rules)
        if how == "ambiguous":
            notes["歧义拒写"].append(lab)
            unmatched.append(f)
            continue
        if not key:
            # 再试 placeholder / name
            for alt in (f.get("placeholder"), f.get("name"), f.get("id")):
                if not alt:
                    continue
                key, how = core.map_one_strict(alt, index, rules)
                if key:
                    how = "partial"
                    break
                if how == "ambiguous":
                    notes["歧义拒写"].append(alt)
                    break

        if not key:
            unmatched.append(f)
            continue

        val = core.value_of(profile, key)
        if val in ("", None) or isinstance(val, (list, dict)):
            plan.append({"idx": f["idx"], "label": lab, "key": key,
                         "type": f["type"], "value": "", "how": how,
                         "状态": "档案缺项"})
            continue

        # 下拉/单选的选项对不上时，靠值域别名救回 —— 记下来，喂给指纹
        if f.get("options"):
            opts = f["options"]
            exact = any(core.norm(o) == core.norm(str(val)) for o in opts)
            if not exact and core.match_option(val, opts) is not None:
                notes["别名救回"].append(lab)

        if f.get("readonly"):
            notes["只读控件"].append(lab)

        plan.append({"idx": f["idx"], "label": lab, "key": key,
                     "type": f["type"], "value": val, "how": how,
                     "状态": "待填"})

    # 按字段优先级重排：预算到点时，先填的必须是高频字段
    plan = core.sort_by_priority(plan)
    return plan, gates, unmatched, notes


def _norm_date(v):
    """
    把档案里的日期归一化成 date 控件要的 YYYY-MM-DD。
    返回 (值 or None, 是否做了补全)
      · "2026-06-30" → 原样
      · "2026-07"    → "2026-07-01"（补全，需提示核对）
      · "2026年7月"  → "2026-07-01"（补全）
    解析不了 → (None, False)，交人工。
    """
    s = str(v).strip()
    for ch in ("年", "月", "日", "/", "."):
        s = s.replace(ch, "-")
    s = s.strip("-")
    parts = [p for p in s.split("-") if p.strip()]
    try:
        if len(parts) == 3:
            return "%04d-%02d-%02d" % (int(parts[0]), int(parts[1]), int(parts[2])), False
        if len(parts) == 2:
            return "%04d-%02d-01" % (int(parts[0]), int(parts[1])), True
        if len(parts) == 1 and len(parts[0]) == 4:
            return "%04d-01-01" % int(parts[0]), True
    except (ValueError, IndexError):
        return None, False
    return None, False


def apply_plan(page, plan, fields_by_idx, deadline=None):
    """
    执行填写。返回 {已填, 失败, 跳过, 日期补全, 超时中止}。
    **本函数不触碰任何提交类按钮。**

    `deadline` = 绝对时间戳（time.time() 口径）。到点即停手 —— 已完成的部分
    照常保留在页面上，未完成的不再往下试，交给「可复制清单页」。
    """
    filled, failed, skipped, adjusted = 0, [], [], []
    timed_out = False
    fake = 0                     # 假成功计数（写入返回成功、回读为空/值=标签）

    for item in plan:
        if deadline is not None and time.time() > deadline:
            timed_out = True
            break
        if item["状态"] == "档案缺项":
            skipped.append({"label": item["label"], "原因": "档案缺项"})
            continue

        sel = '[data-ows-idx="%d"]' % item["idx"]
        f = fields_by_idx.get(item["idx"], {})
        try:
            if item["type"] in ("radio", "checkbox"):
                ok = _fill_choice(page, sel, item, f)
            elif item["type"] == "select" or f.get("tag") == "select":
                ok = _fill_select(page, item, f)
            else:
                v = item["value"]
                if item["type"] == "date":
                    nd, completed = _norm_date(v)
                    if nd is None:
                        failed.append({"label": item["label"],
                                       "原因": "日期格式无法解析，请手填"})
                        continue
                    v = nd
                    if completed:
                        adjusted.append({"label": item["label"], "说明": "按月补全为 -01，请核对"})
                ok = _force_fill(page, sel, v)

            if not ok:
                failed.append({"label": item["label"], "原因": "选项未匹配 / 控件不可写"})
                continue

            # ---- 回读验证：写入返回 ≠ 真的写进去了 ----
            got = _read_back(page, item["idx"], item["type"])
            if not got:
                fake += 1
                failed.append({"label": item["label"],
                               "原因": "写入后回读为空（自绘控件把值丢了）"})
                continue
            if item["type"] not in ("radio", "checkbox") \
                    and core.norm(got) == core.norm(item["label"]):
                # value === label ⇒ 其实是占位提示，没真填上（站点策略库 P-006）
                fake += 1
                failed.append({"label": item["label"],
                               "原因": "回读值等于标签（占位提示，未真正写入）"})
                continue
            filled += 1
        except Exception as e:
            failed.append({"label": item["label"], "原因": str(e).split("\n")[0][:80]})
    return {"已填": filled, "失败": failed, "跳过": skipped,
            "假成功": fake, "超时中止": timed_out, "日期补全": adjusted}


def _fill_choice(page, sel, item, field):
    """单选/复选：按选项文本匹配（含值域别名）。"""
    val = str(item["value"]).strip()
    name = field.get("name") or ""
    if name:
        opts = page.query_selector_all('input[name="%s"]' % name)
    else:
        opts = [page.query_selector(sel)]

    pairs = []                     # [(选项文字, 元素)]
    for o in opts:
        if not o:
            continue
        oid = o.get_attribute("id") or ""
        lab = ""
        if oid:
            l = page.query_selector('label[for="%s"]' % oid)
            if l:
                lab = (l.inner_text() or "").strip()
        if not lab:
            lab = (o.get_attribute("value") or "").strip()
        if lab:
            pairs.append((lab, o))

    for lab, o in pairs:
        if core.norm(lab) == core.norm(val):
            o.check()
            return True
    # 值域别名：档案写「本科」，页面写「大学本科」—— 以前这里直接判失败
    hit = core.match_option(val, [lab for lab, _ in pairs])
    if hit:
        for lab, o in pairs:
            if lab == hit:
                o.check()
                return True
    return False


def _fill_select(page, item, field):
    """下拉框：精确 → 值 → 值域别名 → 模糊。"""
    val = str(item["value"]).strip()
    sel = '[data-ows-idx="%d"]' % item["idx"]
    try:
        page.select_option(sel, label=val)
        return True
    except Exception:
        pass
    try:
        page.select_option(sel, value=val)
        return True
    except Exception:
        pass

    opts = field.get("options") or []
    hit = core.match_option(val, opts)
    if hit:
        try:
            page.select_option(sel, label=hit)
            return True
        except Exception:
            pass
    for o in opts:
        if val and (val in o or o in val):
            try:
                page.select_option(sel, label=o)
                return True
            except Exception:
                continue
    return False


def _force_fill(page, sel, value):
    """
    写入：先常规 fill；失败（只读框、自绘控件）再走 JS 直写并派发 input/change。
    站点策略库 P-003/P-006：只读日历/级联用 page.fill 会直接失败或超时。
    返回值：True = 已写入（**读回验证在调用方做**）。
    """
    try:
        page.fill(sel, value)
        return True
    except Exception:
        pass
    try:
        page.eval_on_selector(sel, """(el, v) => {
            el.removeAttribute('readonly');
            el.focus();
            el.value = v;
            el.dispatchEvent(new Event('input',  {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
        }""", value)
        return True
    except Exception:
        return False


def _read_back(page, idx, kind):
    """
    回读控件当前值。**这是「已填」的唯一判据** ——
    以前 page.fill 返回就算成功，自绘控件把值丢掉也照样记「已填」（假成功）。
    """
    sel = '[data-ows-idx="%d"]' % idx
    try:
        if kind in ("radio", "checkbox"):
            return "1" if page.eval_on_selector(sel, "el => !!el.checked") else ""
        v = page.eval_on_selector(sel, "el => (el.value == null ? '' : String(el.value))")
        return (v or "").strip()
    except Exception:
        return ""


# 收尾保险丝。个别环境下驱动会卡在 browser.close()／驱动退出上，
# 那会把「已经填完」的退出码污染成超时码（124），让上层无法判断成败。
# 正常的关闭是毫秒级，所以这道保险只在收尾真卡住时才生效。
CLOSE_GUARD_SEC = 15


def arm_close_guard(release, seconds=CLOSE_GUARD_SEC, rc=0):
    """守护线程：收尾若在 seconds 秒内没完成，就以 rc 强制结束进程。

    填写结果在收尾之前就已定妥，因此强制结束不会丢结果。
    """
    def _watch():
        if release.wait(seconds):
            return
        print("      ⚠️ 收尾超时（%ds），已结束进程 —— 填写结果不受影响" % seconds)
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(rc)

    threading.Thread(target=_watch, daemon=True).start()


def _write_manual_html(args, fields, profile):
    """提前停手时，直接把已扫到的字段转成「可复制清单页」。

    它是**辅助**，不是主流程 —— 任何失败都只提示、不抛错，绝不影响填写结果。
    """
    try:
        import sheet_html                                  # noqa: E402
        labels = []
        for f in fields:
            lab = (f.get("label") or f.get("name") or "").strip()
            if lab and lab not in labels:
                labels.append(lab)
        if not labels:
            return ""
        md = sheet_html.md_from_profile(args.profile, labels, args.rules)
        ready, miss, meta = sheet_html.parse_md(md)
        out = args.html
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        with open(out, "w", encoding="utf-8", newline="") as fh:
            fh.write(sheet_html.render_html(ready, miss, meta))
        return out
    except Exception as exc:                              # noqa: BLE001
        print("   （可复制页没能生成：%s）" % str(exc).split("\n")[0][:60])
        return ""


def run(args):
    # ---- URL 门禁（在加载档案之前，失败要尽早）----
    ok, why = core.url_gate(args.url, allow_local=getattr(args, "allow_local", False))
    if not ok:
        print("[拒绝] %s" % why)
        print("       本工具会把**你档案里的真实信息**写进这个页面，")
        print("       所以只允许打开 http / https 的公网站点。")
        return 2
    print("[门禁] 即将把档案真值填入：%s" % args.url)
    print("       请确认这是你信任的招聘页面。程序不会点提交。")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[降级] 未检测到 playwright —— 自动填表不可用。")
        print("       请改用：python scripts/make_sheet.py --profile <档案> --labels-file <字段>")
        print("       或安装：pip install playwright && playwright install chromium")
        return 4

    rules = core.load_rules(args.rules)
    index = core.build_index(rules)
    profile = core.load_profile(args.profile)

    t0 = time.time()
    deadline = (t0 + args.budget) if args.budget and args.budget > 0 else None
    if deadline:
        print("[预算] 本站上限 %d 秒 —— 到点即停手，转「可复制清单页」" % args.budget)

    launch_kw = {"headless": not args.headed}
    if args.channel:
        launch_kw["channel"] = args.channel
    if args.proxy:
        launch_kw["proxy"] = {"server": args.proxy}

    report = {"url": args.url, "时间": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}

    release = None
    with sync_playwright() as pw:
        cdp_url = normalize_cdp(args.connect) if getattr(args, "connect", "") else ""
        if cdp_url:
            # ---- 接管模式：复用你已经登录好的浏览器 ----
            # 真实网申站**全部需要登录**，只靠裸开无头浏览器会扫到 0 个字段。
            print("[0/5] 接管已开着的浏览器（复用登录态）：%s" % cdp_url)
            try:
                browser = pw.chromium.connect_over_cdp(cdp_url)
            except Exception as e:
                print("[ERR] 连不上 %s：%s" % (cdp_url, str(e).split("\n")[0][:80]))
                print("      请先开一个带调试端口的浏览器：")
                print("        python scripts/start_browser.py")
                return 2
            ctx = (browser.contexts[0] if browser.contexts
                   else browser.new_context(viewport={"width": 1440, "height": 900}))
            page = _pick_page(ctx, args.url) or ctx.new_page()
        else:
            browser = pw.chromium.launch(**launch_kw)
            ctx = browser.new_context(viewport={"width": 1440, "height": 900})
            page = ctx.new_page()
        # 单步超时不得越过剩余预算 —— 否则 goto / 扫描会整段跑在预算之外，
        # 「300 秒上限」就变成了只管填写阶段的假上限。
        step_to = args.timeout
        if deadline:
            step_to = min(args.timeout,
                          max(1000, int((deadline - time.time()) * 1000)))
        page.set_default_timeout(step_to)

        if cdp_url and site_of(args.url) and site_of(args.url) in (page.url or ""):
            # 已经在目标页上了：**不要再 goto** —— 会把你已填/已登录的状态刷掉
            print("[1/5] 复用当前页签：%s" % (page.url or "")[:100])
        else:
            print("[1/5] 打开页面：%s" % args.url)
            page.goto(args.url, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        report["接管模式"] = bool(cdp_url)

        print("[2/5] 扫描表单字段")
        fields = scan_page(page)
        report["页面标题"] = page.title()
        report["字段总数"] = len(fields)
        print("      发现 %d 个输入控件" % len(fields))

        fields_by_idx = {f["idx"]: f for f in fields}
        labels = [f["label"] for f in fields if f.get("label")]
        plan, gates, unmatched, notes = plan_mapping(fields, labels, profile, index, rules)

        report["可自动填"] = len([p for p in plan if p["状态"] == "待填"])
        report["档案缺项"] = [p["label"] for p in plan if p["状态"] == "档案缺项"]
        report["人工门"] = gates
        report["规则库未命中"] = [f.get("label") or f.get("name") for f in unmatched]
        report["歧义拒写"] = notes["歧义拒写"]
        report["只读控件"] = notes["只读控件"]

        print("[3/5] 映射结果：可填 %d ／ 档案缺项 %d ／ 人工门 %d ／ 未命中 %d"
              % (report["可自动填"], len(report["档案缺项"]),
                 len(gates), len(report["规则库未命中"])))
        if notes["歧义拒写"]:
            print("      拒写 %d 个（标签歧义，宁可交给你手填）：%s"
                  % (len(notes["歧义拒写"]), "、".join(notes["歧义拒写"][:5])))
        if notes["别名救回"]:
            print("      值域别名救回 %d 个下拉：%s"
                  % (len(notes["别名救回"]), "、".join(notes["别名救回"][:5])))

        # ---- 控件指纹 → 站点策略库（以前库建了、引擎不读）----
        fps, fps_why = derive_fingerprints(fields, args.url)
        strategies = match_strategies(args.url, fps) if fps else []
        report["指纹"] = sorted(fps)
        report["命中策略"] = [s.get("id") for s in strategies]
        if fps:
            print("      指纹：%s" % "、".join(sorted(fps)))
            for line in fps_why:
                print("        · %s" % line)
        if strategies:
            print("      命中的站点策略（共 %d 条，来自问题库的已固化经验）："
                  % len(strategies))
            for s in strategies[:4]:
                print("        [%s] %s" % (s.get("id"), (s.get("动作") or "")[:60]))
        elif fps:
            print("      策略库暂无对应条目 —— 这正是值得回传的一条经验"
                  "（python scripts/wangshen.py report）")

        # ---- 提前止损：超预算 或 识别率过低 → 停手，转可复制清单页 ----
        rate = (report["可自动填"] / len(fields)) if fields else 0.0
        report["识别率"] = round(rate, 3)
        stop_reason = ""
        if deadline and time.time() > deadline:
            stop_reason = "超预算（%d 秒）" % args.budget
        elif not fields:
            # 扫到 0 个控件却继续往下走，最后以退出码 0 报"已停在提交前"
            # —— 那是假成功，用户白等。fail-visible：必须停手并说明原因。
            stop_reason = ("未扫到任何输入控件（多半是：还没登录 / 页面没加载完 / "
                           "该站用自绘组件）")
        elif report["可自动填"] == 0:
            stop_reason = ("没有任何字段能自动填（档案缺项 %d ／ 规则未命中 %d）"
                           % (len(report["档案缺项"]), len(report["规则库未命中"])))
        elif len(fields) >= 5 and rate < 0.2:
            stop_reason = "识别率过低（%.0f%%，低于 20%%）" % (rate * 100)
        if stop_reason:
            report["停止原因"] = stop_reason
            print("      ⚠️ %s —— 停手，不再往下填" % stop_reason)

        if args.dry_run:
            print("[4/5] --dry-run：不写入任何内容")
            res = {"已填": 0, "失败": [], "跳过": [], "日期补全": [],
                   "假成功": 0, "超时中止": False}
        elif stop_reason:
            print("[4/5] 已停手，不写入")
            res = {"已填": 0, "失败": [], "跳过": [], "日期补全": [],
                   "假成功": 0, "超时中止": True, "原因": stop_reason}
        else:
            print("[4/5] 写入表单（不提交）")
            res = apply_plan(page, plan, fields_by_idx, deadline=deadline)
            print("      已填 %d ／ 失败 %d ／ 跳过 %d"
                  % (res["已填"], len(res["失败"]), len(res["跳过"])))
            if res.get("假成功"):
                print("      ⚠️ 其中 %d 个是「写进去了又被丢掉」（回读才发现）——"
                      " 已从「已填」里剔除，请手工确认这几项：" % res["假成功"])
                for f in res["失败"][:6]:
                    print("         · %s（%s）" % (f["label"], f["原因"]))
            for a in res.get("日期补全", []):
                print("      ⚠️ %s：%s" % (a["label"], a["说明"]))
            if res.get("超时中止"):
                report["停止原因"] = "填写途中到点（%d 秒）" % args.budget
                print("      ⚠️ %s —— 已停手" % report["停止原因"])

        report["填写结果"] = res

        # 填写后才出现的指纹（假成功 / 值域别名救回）—— 结构指纹在填写前已推过一遍
        fps2, why2 = derive_fingerprints(
            fields, args.url,
            meta={"假成功数": res.get("假成功", 0),
                  "别名救回数": len(notes["别名救回"])})
        new_fps = fps2 - fps
        if new_fps:
            report["指纹后验"] = sorted(new_fps)
            print("      后验指纹：%s" % "、".join(sorted(new_fps)))
            for s in match_strategies(args.url, new_fps):
                print("        → 命中策略 [%s] %s" % (s.get("id"), (s.get("动作") or "")[:56]))

        if args.screenshot:
            os.makedirs(os.path.dirname(os.path.abspath(args.screenshot)), exist_ok=True)
            page.screenshot(path=args.screenshot, full_page=True)
            print("[5/5] 已截图：%s" % args.screenshot)
        else:
            print("[5/5] 未指定 --screenshot")

        # 报告**先落盘**：收尾保险丝一旦触发会直接结束进程，任何产物都必须
        # 在武装它之前写完，否则会丢（只含键名与状态，不含真值）。
        if args.out:
            os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
            # newline="" 关掉平台换行转换：Windows 上会写成 CRLF，与仓库
            # .gitattributes 的 eol=lf 约定冲突。统一出 LF。
            with open(args.out, "w", encoding="utf-8", newline="") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
            print("报告已写入：%s" % args.out)

        # —— 收尾。先上保险丝再关浏览器。
        # 退出码必须**在武装保险丝之前**算好：保险丝写死 rc=0 会把
        # 「提前停手（应=5）」污染成「成功」。
        final_rc = 5 if report.get("停止原因") else 0
        release = threading.Event()
        arm_close_guard(release, rc=final_rc)
        if cdp_url:
            # 接管模式：**只断开连接，绝不关闭你的浏览器** ——
            # 那是你自己开着的窗口，里面还有你的登录态。
            print("      （接管模式：只断开连接，你的浏览器保持打开）")
        else:
            try:
                browser.close()
            except Exception:
                pass  # 驱动偶发连接错，不影响已完成的填写结果
    # 退出 with 时还会停一次驱动，保险丝仍在守
    if release is not None:
        release.set()

    print("")
    reason = report.get("停止原因")
    if reason:
        print("⚠️ 本站提前停手：%s" % reason)
        print("   已扫到 %d 个字段。转成「可复制清单页」照着粘更稳：" % len(fields))
        print("       python scripts/wangshen.py sheethtml --profile <你的档案> \\")
        print("           --labels-file 页面字段.txt")
        if args.html:
            wrote = _write_manual_html(args, fields, profile)
            if wrote:
                print("   已生成：%s（用浏览器打开，点「复制」）" % wrote)
        else:
            print("   想让脚本停手时直接生成那张页：加 --html 填写清单.html")
        print("")
    print("⚠️ 已停在提交前。**附件、志愿顺序、最终提交由你本人完成。**")
    return 5 if reason else 0


def main():
    ap = argparse.ArgumentParser(description="网申表单自动填写（不提交）")
    ap.add_argument("--selftest", action="store_true",
                    help="自测：只测映射/指纹/日期这些纯函数，不开浏览器")
    ap.add_argument("--profile", help="本机档案 JSON")
    ap.add_argument("--url", help="岗位页 URL")
    ap.add_argument("--rules", help="字段规则库路径")
    ap.add_argument("--channel", default="msedge",
                    help="浏览器内核：msedge / chrome（默认 msedge）")
    ap.add_argument("--proxy", default=os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"),
                    help="浏览器代理（Chromium 不读环境变量，必须显式传）")
    ap.add_argument("--headed", action="store_true", help="显示浏览器窗口（默认无头）")
    ap.add_argument("--timeout", type=int, default=45000, help="单步超时毫秒")
    ap.add_argument("--screenshot", help="截图输出路径")
    ap.add_argument("--out", help="报告 JSON 输出路径")
    ap.add_argument("--dry-run", action="store_true", help="只映射不写入")
    ap.add_argument("--budget", type=int, default=300,
                    help="每站硬预算（秒；0 = 不限，默认 300）")
    ap.add_argument("--allow-local", action="store_true",
                    help="放行本机/内网地址（仅供开发自测；默认拒绝）")
    ap.add_argument("--connect", default="",
                    help="接管已开着的浏览器（CDP）：端口号 9222 或 http://127.0.0.1:9222；"
                         "用于复用登录态，真实网申站几乎都需要登录")
    ap.add_argument("--html", default="",
                    help="提前停手时，直接生成可复制清单页到该路径")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if not args.profile or not args.url:
        ap.error("需要 --profile 与 --url（或单独使用 --selftest）")
    return run(args)


def selftest():
    """
    不开浏览器的自测：只覆盖纯函数（映射 / 拒写 / 指纹 / 日期 / 回读判据）。
    填表本身需要真实页面，交给 `--dry-run` 在真站上验。
    """
    ok = [0]
    bad = [0]

    def check(name, cond, extra=""):
        if cond:
            ok[0] += 1
            print("  [OK] %s" % name)
        else:
            bad[0] += 1
            print("  [FAIL] %s %s" % (name, extra))

    rules = core.load_rules()
    index = core.build_index(rules)

    def mk(idx, label, typ="text", options=None, visible=True, inform=True,
           readonly=False, name="n%d" % 0):
        return {"idx": idx, "label": label, "type": typ, "name": name, "id": "",
                "options": options, "visible": visible, "inForm": inform,
                "readonly": readonly}

    # ---- 泛化短标签：拒写而不是猜 ----
    f = [mk(0, "日期")]
    plan, _g, un, _n = plan_mapping(f, ["日期"], {"出生日期": "2000-01-01"}, index, rules)
    check("① 泛化标签「日期」不被映射（拒写优于填错）", not plan and len(un) == 1)

    f = [mk(0, "状态")]
    plan, _g, un, _n = plan_mapping(f, ["状态"], {"健康状况": "良好"}, index, rules)
    check("② 泛化标签「状态」不被映射", not plan)

    # ---- 精确标签仍然工作（黑名单只拦模糊匹配）----
    f = [mk(0, "姓名")]
    plan, _g, _u, _n = plan_mapping(f, ["姓名"], {"姓名": "张三"}, index, rules)
    check("③ 精确标签正常映射且状态=待填",
          bool(plan) and plan[0]["key"] == "姓名" and plan[0]["状态"] == "待填")

    # ---- 值域别名救回 ----
    f = [mk(0, "学历", "select", ["大学本科", "硕士研究生"])]
    plan, _g, _u, notes = plan_mapping(f, ["学历"], {"最高学历": "本科"}, index, rules)
    check("④ 下拉「本科↔大学本科」靠值域别名救回并记账",
          notes["别名救回"] == ["学历"], notes)

    # ---- 人工门 ----
    f = [mk(0, "登录密码", "password"), mk(1, "上传简历", "file")]
    _p, gates, _u, _n = plan_mapping(f, ["登录密码", "上传简历"], {}, index, rules)
    check("⑤ 密码框与上传框进人工门", len(gates) == 2)

    # ---- 指纹推导 ----
    fields = [mk(0, "", inform=False), mk(1, "x", "file", visible=False),
              mk(2, "y", readonly=True)]
    fps, why = derive_fingerprints(fields)
    check("⑥ 结构指纹推导齐全",
          {"label-missing", "portal-to-body", "file-hidden",
           "readonly-cascader"} <= fps, sorted(fps))
    check("⑦ 正常页面不产生指纹",
          derive_fingerprints([mk(0, "姓名")], ) [0] == set())

    fps2, _ = derive_fingerprints(
        [mk(0, "姓名")], meta={"假成功数": 2, "别名救回数": 1})
    check("⑧ 后验指纹（假成功 / 值域别名）",
          {"value-equals-label", "dropdown-value-mismatch"} <= fps2, sorted(fps2))

    # ---- 日期归一 ----
    check("⑨ 日期补全标出", _norm_date("2026-07") == ("2026-07-01", True))
    check("⑩ 日期不可解析交人工", _norm_date("下学期") == (None, False))

    # ---- 站点标识 ----
    check("⑪ 站点取主机名且去 www",
          site_of("https://www.51job.com/a/b") == "51job.com", site_of("https://www.51job.com/a"))

    print("\n自测结果：%d 通过 / %d 失败" % (ok[0], bad[0]))
    return 0 if bad[0] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
