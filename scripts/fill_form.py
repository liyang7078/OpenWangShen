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
import re
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
# 自绘面板（2026-10-16 补全 P0-10）
# --------------------------------------------------------------------------
# 只读框/自绘日历/级联选择器：`fill` 与「JS 直写 value」都会**假成功** ——
# 组件把值渲染在自己的面板里，DOM 的 value 根本没动。唯一可靠的办法是
# 「点开面板 → 按文本点中那一项 → 以控件展示值回读判定」。
PANEL_SETTLE_MS = 450        # 面板动画的等待上限（真实站实测 300~400ms 足够）
PANEL_MAX_LEVELS = 3         # 级联最多点几级（省 / 市 / 区）
PANEL_MAX_ITEMS = 400        # 单次采集的候选项上限（防超大虚拟列表拖死本机）

# 日期类控件：值形如 2026-06-30 / 2026-07
DATE_HINTS = ("日期", "时间", "年月", "生日", "起止", "起始", "截止")
# 地区级联类控件：值是由「省/市/区」拼起来的串
REGION_HINTS = ("籍贯", "户籍", "生源", "出生地", "地址", "省市", "地区", "所在")
REGION_SEG_RE = re.compile(r"[^省市区县]+?(?:省|市|区|县|自治区|自治州|特别行政区)")


def split_region(value):
    """
    把地区串切成级联需要的逐级片段。
      "山东省济南市历下区" → ["山东省", "济南市", "历下区"]
      "北京市海淀区"       → ["北京市", "海淀区"]
      "内蒙古自治区呼和浩特市" → ["内蒙古自治区", "呼和浩特市"]
    切不动 → 整体作一段（宁可按一段试，也不猜）。
    """
    s = str(value or "").strip()
    if not s:
        return []
    segs = REGION_SEG_RE.findall(s)
    if not segs:
        return [s]
    joined = "".join(segs)
    rest = s[len(joined):].strip()
    if rest:
        segs.append(rest)
    return segs


def panel_kind(item, field):
    """
    判定该控件要不要走面板化写入。返回 "date" / "cascade" / ""。
    判据只取「控件形态 + 字段语义」，不认具体站点（一次登记换 N 站复用）。
    """
    if not field.get("panel") and not field.get("readonly"):
        return ""
    label = "%s%s" % (item.get("label") or "", item.get("key") or "")
    val = str(item.get("value") or "")
    if any(h in label for h in REGION_HINTS) or len(REGION_SEG_RE.findall(val)) >= 2:
        return "cascade"
    if item.get("type") == "date" or any(h in label for h in DATE_HINTS):
        return "date"
    # 有面板迹象但语义不明：按级联试（可多级点选，退化为单级就是普通下拉）
    return "cascade"


def _panel_items(frame):
    """采集面板里可见的候选项：[{"k": int, "text": str}]。

    另把闸门诊断记进 `LAST_PANEL_GATE`（旁路，见 PANEL_ITEMS_JS 末段注释）。
    """
    global LAST_PANEL_GATE
    try:
        got = frame.evaluate(PANEL_ITEMS_JS) or []
    except Exception:                                        # noqa: BLE001
        return []
    if isinstance(got, dict):
        items = got.get("items") or []
        LAST_PANEL_GATE = {"闸门生效": bool(got.get("gate")),
                           "剔除数": int(got.get("excluded") or 0),
                           "候选数": len(items)}
    else:
        # 兼容旧契约（纯数组）。**不猜就不炸** —— 万一回滚了 JS 没回滚这里。
        items = got or []
        LAST_PANEL_GATE = {"闸门生效": False, "剔除数": 0, "候选数": len(items)}
    return items[:PANEL_MAX_ITEMS]


def _snap_overlays(frame):
    """点击**前**标记此刻已可见的浮层容器（供浮层归属闸比对）。返回标记数，失败 -1。

    必须在**每一次会弹面板的点击之前**调用 —— 漏一次，那一次点击打开的面板就
    带着"上一轮遗留状态"参与归属判断，闸门要么失效要么误判。
    """
    try:
        return int(frame.evaluate(SNAP_OVERLAYS_JS) or 0)
    except Exception:                                        # noqa: BLE001
        return -1


def _panel_click(frame, k):
    """按采集时打的标记点中某一项（用 JS click，避免弹层被 portal 到 body 后坐标错位）。"""
    try:
        el = frame.query_selector('[data-ows-opt="%d"]' % k)
        if el:
            el.click()
            return True
    except Exception:                                        # noqa: BLE001
        pass
    return False



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
(prefix) => {
  const SKIP = new Set(['hidden', 'submit', 'button', 'image', 'reset']);
  const PRE = prefix || '';

  // ---- shadow DOM 穿透（2026-10-16 新增）----
  // 新一代组件库（Web Components）把表单放在 shadow root 里：
  // document.querySelectorAll 从外部**看不见**它们 ⇒ 扫到 0 个字段。
  // 这里改成「从 document 出发，遇到 shadowRoot 就钻进去」。
  const deepAll = (sel, root) => {
    const out = [];
    const seen = new Set();
    const walk = (node) => {
      let hits = [];
      try { hits = node.querySelectorAll(sel); } catch (e) { hits = []; }
      for (const e of hits) { if (!seen.has(e)) { seen.add(e); out.push(e); } }
      let kids = [];
      try { kids = node.querySelectorAll('*'); } catch (e) { kids = []; }
      for (const el of kids) {
        const sr = el.shadowRoot;
        if (sr && !seen.has(sr)) { seen.add(sr); walk(sr); }
      }
    };
    walk(root || document);
    return out;
  };

  const deepById = (id) => {
    if (!id) return null;
    for (const el of deepAll('[id]')) { if (el.id === id) return el; }
    return null;
  };

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
    return (t || '').replace(/^(请输入|请填写|请选择|请下拉|请点击|请上传|请注明|请录入|请提供|输入|填写|选择)\s*/, '').trim();
  }
  // ---- 标签污染判据（2026-10-16 由 LYSKILL · 5A 判据移植）----
  // 为什么要有：清单里出现过两类**根本不是字段名**的「伪字段」，考生照着填只会白费劲：
  //   甲 纯提示语 —— 剥掉提示前缀后只剩「内容/文本/信息/输入/搜索/查询」这类词尾。
  //      只判「剥完是否为空」是不够的：placeholder=「请输入内容」剥完剩「内容」，
  //      照样被当字段名一路流进清单（LYSKILL 实测：清单第 30 项）。
  //   乙 值回显 —— 级联 / 省市区控件把**已选值**渲染成文本，形如
  //      「江苏省 / 江苏省-盐城市」，被候选标签节点命中（LYSKILL 实测：清单第 29 项）。
  //      判据是「斜杠两侧自我包含」—— 真字段名不会含着自己，
  //      「专业/方向」「刊物/机构」都不满足。
  // 两条判据都只看**形状**，不看站点：提示语是固定词面，值回显是自包含结构。
  // 命中即**不当字段名**，继续往下走几何法 / 父行标签 —— 而不是硬把污染文本留下来：
  // 留下它等于给考生一条无法执行的指令。
  const HINT_TAIL = /^(内容|文本|信息|输入|搜索|查询)$/;
  const isHintOnly = (s) => {
    const b = stripHint(s);
    return !b || HINT_TAIL.test(b);
  };
  function isBadLabelName(t) {
    const s = (t || '').replace(/[\u00a0\u3000]/g, ' ').replace(/\s+/g, ' ').trim();
    // 长度不在这里管：调用方本来就只接受 2~40 字；这里专判「长度合格但不是字段名」。
    if (!s || s.length < 2 || s.length > 40) return false;
    if (isHintOnly(s)) return true;
    const m = s.split(/\s*[\/／|｜]\s*/).map((x) => x.trim()).filter(Boolean);
    if (m.length === 2 && m[0] !== m[1] &&
        (m[1].indexOf(m[0]) >= 0 || m[0].indexOf(m[1]) >= 0)) return true;
    return false;
  }
  // 候选节点本身长在**控件自己的壳层里** ⇒ 那是「已选值回显」，不是字段名。
  // ⚠ 只认 picker / cascader / dropdown / calendar 这类**控件壳**，
  //   **刻意不含泛的 select** —— 页面行容器类名里带 "select" 的很多
  //   （如 form-select-item），一放进来会把整行标签剔掉。
  function isCtlValueEcho(n, ctl) {
    try {
      const w = n.closest('[class*="picker"],[class*="cascader"],[class*="dropdown"],[class*="calendar"]');
      return !!(w && ctl && w.contains(ctl));
    } catch (e) { return false; }
  }
  // 不静默：剔掉了什么，逐条带出去报告（去重、封顶 20 条）。
  // 为什么必须带出去：「污染判据误杀真字段名」的唯一发现途径就是有人看到这份名单。
  const labelPolluted = [];
  const notePolluted = (t) => {
    const s = (t || '').replace(/[\u00a0\u3000]/g, ' ').replace(/\s+/g, ' ').trim();
    if (s && labelPolluted.length < 20 && labelPolluted.indexOf(s) < 0) labelPolluted.push(s);
  };
  // 取标签的**统一出口**：污染文本不当字段名（记一笔后返回空，调用方继续往下走）。
  function accept(t) {
    const s = (t || '').trim();
    if (!s) return '';
    if (isBadLabelName(s)) { notePolluted(s); return ''; }
    return s;
  }

  function visible(el) {
    return !!(el.offsetParent !== null ||
              (el.getClientRects && el.getClientRects().length));
  }

  // 候选标签节点先算一次，避免每个控件都全页扫一遍
  const CAND = deepAll('label,span,div,p,td,th,dt,dd');

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
      // 污染文本 / 控件值回显（2026-10-16 5A 移植）：几何最近≠是字段名，
      //   级联控件旁边紧贴着它自己的「江苏省 / 江苏省-盐城市」回显文本，
      //   几何法会把回显当成字段名收走。命中即跳过，继续找下一个候选。
      if (isBadLabelName(t) || isCtlValueEcho(n, el)) { notePolluted(t); continue; }
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
      if (t && !isSectionTitle(el.labels[0], t) && !isBadLabelName(t)) return t;
      if (t) notePolluted(t);
    }
    // 2) label[for=id]（穿透 shadow DOM）
    if (el.id) {
      for (const l of deepAll('label[for="' + el.id + '"]')) {
        const t = (l.innerText || '').trim();
        if (t && !isSectionTitle(l, t) && !isBadLabelName(t)) return t;
      }
    }
    // 3) aria-label / aria-labelledby
    const aria = el.getAttribute('aria-label');
    if (aria && aria.trim()) return aria.trim();
    const alb = el.getAttribute('aria-labelledby');
    if (alb) {
      const parts = alb.split(/\s+/).map(id => {
        const n = deepById(id);
        return n ? (n.innerText || '').trim() : '';
      }).filter(Boolean);
      if (parts.length) return parts.join('');
    }
    // 4) placeholder（剥掉「请输入/请选择」提示前缀）
    //    剥完是「内容/文本/信息…」这类词尾的 ⇒ 纯提示语，不是字段名（5A 移植），记一笔后跳过。
    const ph = stripHint(el.getAttribute('placeholder'));
    if (ph && !isBadLabelName(ph)) return ph;
    if (ph) notePolluted(ph);
    // 5) 向上找容器里第一段短文本（跳过区段标题）
    let node = el.parentElement, depth = 0;
    while (node && depth < 3) {
      const t = (node.innerText || '').trim();
      if (t && t.length <= 40 && !isSectionTitle(node, t) && !isBadLabelName(t)) return t;
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
        if (t && t.length <= 40 && !isSectionTitle(l, t) && !isBadLabelName(t)) return t;
      }
      const al = node.getAttribute && node.getAttribute('aria-label');
      if (al) return al.trim();
      node = node.parentElement; depth++;
    }
    return geometricLabel(el);
  }

  // 是否存在自绘面板的迹象（日历 / 级联 / 下拉弹层）——
  // 这类控件写 value 会假成功，必须走「点开 → 点选」。
  function panelHint(el) {
    if (el.getAttribute('aria-haspopup')) return true;
    if (el.getAttribute('aria-expanded') !== null) return true;
    if (el.readOnly) return true;
    if (el.getAttribute('role') === 'combobox') return true;
    return false;
  }

  // ---- 清掉上一轮遗留的身份属性（2026-10-16 由 LYSKILL 判据移植）----
  //   同一 idx 若同时挂在新旧两批元素上，选择器只会取**第一个** —— 取到旧节点就是
  //   「串位」的物证：值写进上一个字段，而回读用同一个选择器读回来照样"正常"，
  //   串位天然不可见（LYSKILL 实测某银行站：生源地的值进了微信号框，回读只报"该字段为空"）。
  //   必须**扫描期清、扫描期重打**：留到写入期才补打，等于用重建后的序号去标元素，
  //   一旦两次登记结果漂移，属性就把漂移固化成「身份」。
  //   用 deepAll 而非 document.querySelectorAll —— 后者钻不进 shadow root，
  //   留在 shadow 里的旧属性清不掉，正是漏网的那一半。
  try {
    deepAll('[data-ows-idx]').forEach(e => e.removeAttribute('data-ows-idx'));
  } catch (e) {}

  const out = [];
  let i = 0;
  deepAll('input,select,textarea').forEach(el => {
    const t = (el.type || el.tagName).toLowerCase();
    if (SKIP.has(t)) return;
    const idx = PRE + String(i++);
    el.setAttribute('data-ows-idx', idx);

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
      viaGroup: viaGroup,
      panel: panelHint(el)
    });
  });
  // 返回契约（2026-10-16 改）：从「纯数组」变成 `{fields, polluted}` ——
  //   因为污染判据必须把「剔掉了什么」带出去。Playwright 只序列化普通对象属性，
  //   挂到数组上的自定义属性会被丢掉 ⇒ 只能显式包一层。
  //   调用方只认 `.fields`；`.polluted` 缺省为空数组（老调用点不会因为缺字段炸掉）。
  return { fields: out, polluted: labelPolluted };
}
"""


# --------------------------------------------------------------------------
# 自绘面板（日历 / 级联 / 下拉弹层）里的候选项采集
# --------------------------------------------------------------------------
# 为什么要单独一套：这类控件**不受我们控制**（不是原生 select），
# 唯一可靠的办法是「点开它 → 看它渲染出了什么 → 点中那一项」。
# 采集用「结构特征 + 可见性」双判据，不依赖具体组件库的类名。
#
# ⚠ 2026-10-16 补「浮层归属闸」（由 LYSKILL · 5A 判据移植）。为什么非补不可：
#   点开控件后会**全页**采集候选项，而网申页常有「隔壁下拉的面板还开着」的情形
#   （考生填到一半、或前一个控件没点掉）。两个面板里有同文本选项是**极常见**的
#   ——省/市/区重名在网申站上遍地都是。后果不是"填不上"而是**安静点错**：
#   引擎点了兄弟面板的选项，目标控件一点没变，而回读只判「非空且≠标签」
#   ⇒ 照样记「已填」。写到真实申请表里的错误值是**不可逆**的，
#   而失败（填写清单）是可恢复的 —— 所以这里宁可收窄，绝不放宽。
OVERLAY_SEL_JS = ('[class*="popup"],[class*="Popup"],[class*="dropdown"],[class*="Dropdown"],'
                  '[class*="menu"],[class*="panel"],[class*="popper"],[class*="overlay"],'
                  '[role="listbox"],[role="menu"]')

# 点击**前**快照：把此刻已可见的浮层容器打上 `data-ows-prev` 标记。
# 为什么用「打标记」而不是把节点传回 Python：DOM 引用过不了 JS↔Python 的边界；
#   而整个「点开 → 采集 → 点选」在 LYSKILL 里是**一个** evaluate 内部完成的，
#   本引擎是 Python 编排的分步调用 ⇒ 状态只能外挂到 DOM 上。
# ⚠ **先清后打**（与扫描期 data-ows-idx 同一判据，同一理由）：
#   不清掉上一轮的标记，就会把「上一轮就已可见」误判成「这一轮翻转」——
#   这是同一类错误的镜像：把噪音固化成身份。
SNAP_OVERLAYS_JS = r"""
() => {
  const SEL = '%s';
  const vis = (el) => !!(el.offsetParent !== null ||
                        (el.getClientRects && el.getClientRects().length));
  try {
    document.querySelectorAll('[data-ows-prev]').forEach(e => e.removeAttribute('data-ows-prev'));
  } catch (e) {}
  let n = 0;
  try {
    document.querySelectorAll(SEL).forEach(e => {
      if (vis(e)) { e.setAttribute('data-ows-prev', '1'); n++; }
    });
  } catch (e) {}
  return n;
}
""" % OVERLAY_SEL_JS

PANEL_ITEMS_JS = r"""
() => {
  const SEL = [
    '[role="option"]', '[role="listbox"] li', '[role="menu"] li',
    '[class*="picker"] li', '[class*="picker"] td', '[class*="picker"] dd',
    '[class*="calendar"] td', '[class*="calendar"] li',
    '[class*="dropdown"] li', '[class*="dropdown"] dd',
    '[class*="cascader"] li', '[class*="option"]', '[class*="cell"]',
    '[class*="panel"] li', '[class*="panel"] td', '[class*="panel"] span',
    '[class*="select"] li', '[class*="popup"] li', 'td[class*="day"]'
  ];
  const out = [], seen = new Set();
  const vis = (el) => !!(el.offsetParent !== null ||
                        (el.getClientRects && el.getClientRects().length));
  const panelish = (el) => {
    let n = el, d = 0;
    while (n && d < 6) {
      const a = (n.getAttribute && (n.getAttribute('class') || '')) || '';
      const r = n.getAttribute && (n.getAttribute('role') || '');
      if (/picker|calendar|dropdown|cascader|popup|select|panel|menu|option/i.test(a)
          || r === 'listbox' || r === 'menu' || r === 'dialog') return true;
      n = n.parentElement; d++;
    }
    return false;
  };
  // ---- 浮层归属闸（5A 移植）------------------------------------------------
  //   判据：**浮层容器的可见性翻转**。点击前记下"已可见的容器"（由
  //   SNAP_OVERLAYS_JS 打上 data-ows-prev），点击后**没带标记的可见容器**
  //   就是本次点击打开的面板 ⇒ 只有它（及其祖先/后代）里的候选项算数。
  //   隔壁那个本来就开着的面板带着标记 ⇒ 被排除。
  //   为什么不用这两种（LYSKILL 已实测证伪，不重复踩）：
  //     · 像素距离窗 —— 收到 -200/+300 实测 12/18 → 7/18，翻车；
  //       且**上弹面板整块落在控件上方**，会被窗口整体剔掉。
  //     · 点击前后 DOM 差集 —— 真站大量面板是**加载时预挂载**、只靠 display
  //       切换，点击不新增节点 ⇒ 差集恒空，闸门永不生效。
  //   安全性（**只许收窄**）：页面上一个"未带标记的可见浮层"都没有时
  //   （判据不可用：预挂载面板刚被 display 打开时正属此列），
  //   ⇒ `gateOn=false` ⇒ **原样放行**。判据失灵时绝不误杀。
  const OV_SEL = '%s';
  const overlayOf = (el) => {
    if (!el || !el.closest) return null;
    try {
      const o = el.closest(OV_SEL);
      if (o && o !== document.body) return o;
    } catch (e) {}
    // 退一步：最近的 fixed/absolute 定位祖先（各站浮层写法不一，类名常不含 popup）
    let cur = el.parentElement, d = 0;
    while (cur && cur !== document.body && d < 12) {
      let st = null; try { st = getComputedStyle(cur); } catch (e) {}
      if (st && (st.position === 'fixed' || st.position === 'absolute')) return cur;
      cur = cur.parentElement; d++;
    }
    return null;
  };
  let fresh = [];
  try {
    fresh = Array.from(document.querySelectorAll(OV_SEL))
      .filter(e => vis(e) && !e.hasAttribute('data-ows-prev'));
  } catch (e) { fresh = []; }
  const gateOn = fresh.length > 0;
  const ownsPanel = (el) => {
    if (!gateOn) return true;                 // 判据不可用 ⇒ 放行（只许收窄）
    const ov = overlayOf(el);
    if (!ov) return true;                     // 不在任何浮层容器里 ⇒ 不判归属，放行
    for (const f of fresh) {
      if (f === ov || f.contains(ov) || ov.contains(f) || f.contains(el)) return true;
    }
    return false;                             // 明确属于别的面板 ⇒ 剔除
  };
  let excluded = 0;
  for (const sel of SEL) {
    for (const el of document.querySelectorAll(sel)) {
      if (seen.has(el) || !vis(el)) continue;
      if (el.querySelector('input,select,textarea')) continue;
      const txt = (el.innerText || el.textContent || '').trim();
      if (!txt || txt.length > 24) continue;
      if (!panelish(el)) continue;
      seen.add(el);
      if (!ownsPanel(el)) { excluded++; continue; }
      out.push({ k: out.length, text: txt });
      el.setAttribute('data-ows-opt', String(out.length - 1));
    }
    if (out.length > 400) break;
  }
  // 返回契约（2026-10-16 改）：从「纯数组」变成 `{items, gate, excluded}` ——
  //   `gate` / `excluded` 是**旁路诊断**：闸门有没有生效、剔掉了几个，
  //   直接决定"某项没填上"到底是"面板里真没有"还是"被闸门误杀"。
  //   这种区分不给出来，排障就只能靠猜。
  return { items: out, gate: gateOn, excluded: excluded };
}
""" % OVERLAY_SEL_JS

# 面板搜索框兜底（2026-10-16 由 LYSKILL · 3A/4A 判据移植）。
# 要解决的问题：**虚拟列表型下拉**（学校名称 / 专业名称 / 所在城市…）点开面板后，
#   里面可能**一个候选项都没有** —— 选项要等关键字注入后才渲染出来。
#   LYSKILL 实测「学校名称」：`showSearch` 型 select 的搜索框
#   `input.ant-select-search__field` 长在**控件内部**、不在浮层里，
#   而"浮层里恰好一个可编辑 input"的判据永远取不到它 ⇒ 兜底从不启用
#   ⇒ 点开后候选恒为空 ⇒ 关键字没注入、匹配永远失败。
#
# ⛔ **绝不能"全页找离控件最近的可见文本框"**（LYSKILL 已实测证伪，不重复踩）：
#   网申页字段是**竖排**的，「最近的下方文本框」几乎必然是**下一个字段的输入框**
#   ⇒ 值被写进隔壁字段，而写入侧毫无察觉（它以为自己填的是搜索框）。
#   这就是"静默串位"的又一条来路。
#   ⇒ 只认两种来源：① 本控件**自身容器内**的搜索框；
#                   ② **唯一一个**可见浮层里的**恰好一个**可编辑 input。
#   多一个少一个都不写 —— **不唯一不猜**。
# 打标记而非返回节点：DOM 引用过不了 JS↔Python 边界；标记同理用「先清后打」，
#   不清就会取到**上一个控件**留下的搜索框（那一刻它才是最接近控件的那一个）。
FIND_SEARCHBOX_JS = r"""
(ctlSel) => {
  const vis = (el) => !!(el.offsetParent !== null ||
                        (el.getClientRects && el.getClientRects().length));
  const editable = (el) => {
    const t = (el.getAttribute('type') || '').toLowerCase();
    return t !== 'hidden' && t !== 'submit' && t !== 'button' && t !== 'file'
           && !el.readOnly && !el.disabled;
  };
  try {
    document.querySelectorAll('[data-ows-search]').forEach(e => e.removeAttribute('data-ows-search'));
  } catch (e) {}
  const mark = (el, where) => {
    try { el.setAttribute('data-ows-search', where); } catch (e) {}
    return where;
  };
  let ctl = null;
  try { ctl = document.querySelector(ctlSel); } catch (e) { ctl = null; }
  if (!ctl) return 'none';

  // ① 控件**自身容器内**的搜索框。安全性：取的就是本控件自己壳层里的 input，
  //    天然属于本控件，不存在"最近的下方文本框 = 下一个字段"的串位风险；
  //    仍守「不唯一不猜」—— 容器内多个可编辑 input 时不启用。
  try {
    const host = ctl.closest('.ant-select, .el-select, .phoenix-select, '
                           + '[class*="select"][class*="picker"], [class*="picker"]');
    if (host) {
      const own = Array.from(host.querySelectorAll('input')).filter(editable);
      if (own.indexOf(ctl) >= 0) return mark(ctl, 'ctrl');   // 控件本身就是搜索框
      if (own.length === 1) return mark(own[0], 'host');
    }
  } catch (e) {}

  // ② 唯一一个可见浮层里的恰好一个可编辑 input。
  //    只收「与控件水平交叠、且垂直上贴着控件」的浮层 ⇒ 滤掉页头导航 / 左侧菜单栏。
  try {
    const r = ctl.getBoundingClientRect();
    const boxes = Array.from(document.querySelectorAll('%s')).filter((b) => {
      if (!vis(b)) return false;
      const br = b.getBoundingClientRect();
      if (br.width < 40 || br.height < 20) return false;
      if (br.bottom < r.top + 4) return false;               // 完全在控件上方
      if (br.top > r.bottom + 520) return false;             // 离控件太远
      return (Math.min(br.right, r.right) - Math.max(br.left, r.left)) >= 40;  // 水平交叠
    });
    if (boxes.length !== 1) return 'none';                   // 不唯一 ⇒ 不猜
    const inputs = Array.from(boxes[0].querySelectorAll('input')).filter(editable);
    if (inputs.length !== 1) return 'none';                  // 不唯一 ⇒ 不猜
    return mark(inputs[0], 'panel');
  } catch (e) { return 'none'; }
}
""" % OVERLAY_SEL_JS


# --------------------------------------------------------------------------
# 展开型重复模块：先点「＋ 添加」把子表单变出来，再交给**同一条** scan/fill 链路
# --------------------------------------------------------------------------
# 为什么要有这段：教育 / 实习 / 校园 / 项目 / 荣誉 / 论文 / 证书 这类模块，控件
# **默认不渲染**，要先点「＋ 添加」才出现。只扫首屏的引擎对它们**一个字段都看不见**——
# 这不是「填不上」，是「根本没扫到」；**扫描漏掉的东西，后面所有环节都救不回来**。
#
# 三条边界（本能力的红线，写死在这里，不许放宽）：
#   · **只点增行入口**：文案须含 添加/新增/增加，且**一个禁用词都不许含**。
#   · **每个模块只点一次**（点两次就多出一条空记录，等于替你造数据）。
#   · **不填值、不删行**：这里只负责「把控件变出来」。
EXPAND_WORDS = ("添加", "新增", "增加")
EXPAND_DENY = ("保存", "暂存", "提交", "确认", "取消", "关闭", "删除", "返回",
               "上一步", "下一步", "导出", "下载", "上传", "重置")
EXPAND_ATTR = "data-ows-exp"

# 归一化：只去空白与常见装饰字符（全角加号、星号、括号……）——
#   为什么要去「＋」：「＋ 添加」「+添加」「添加」是**同一个入口**的三种写法。
_EXP_STRIP = re.compile(r"[\s\u00a0*＊＋+（）()【】\[\]·、,，.。:：;；\-—_/\\|]+")


def _exp_norm(s):
    return _EXP_STRIP.sub("", str(s or ""))


def is_add_entry(text):
    """这串文案是不是「增行入口」？（**纯函数**，自测直接喂字符串）

    判据是**双向**的：必须含增行词，且**一个禁用词都不能含**。
    宁可漏点一个入口（少填一段，你看得见），也绝不点错一个保存 / 提交（你看不见）。
    """
    t = _exp_norm(text)
    if not t or len(t) > 12:
        return False
    if any(w in t for w in EXPAND_DENY):
        return False
    return any(w in t for w in EXPAND_WORDS)


EXPAND_JS = r"""
() => {
  const txt = el => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const CTL = 'input,select,textarea';
  const cntIn = root => {
    if (!root) return -1;
    let n = 0;
    try {
      root.querySelectorAll(CTL).forEach(c => {
        if (c.tagName === 'INPUT' && (c.type === 'hidden' || c.type === 'file')) return;
        if (vis(c)) n++;
      });
    } catch (e) { return -1; }
    return n;
  };

  // 每轮**重新标记**：点开一个模块后页面会重排，上一轮的标记可能已经失效。
  try {
    document.querySelectorAll('[data-ows-exp]').forEach(e => e.removeAttribute('data-ows-exp'));
  } catch (e) {}

  // 模块容器：只认**结构明确**的那种。定位不出返回 null ⇒ 调用方**不据此跳过**，
  //   退回旧行为 —— 不拿一个猜出来的容器去判「这模块是不是已经展开过」。
  const boxOf = el => {
    try {
      return el.closest('[class*="gridContainer"],[class*="Card"],[class*="card"],' +
                        '[class*="Module"],[class*="module"],[class*="block"],[class*="Block"]');
    } catch (e) { return null; }
  };
  const isBtnTxt = s => s.length <= 14 &&
    /(添加|新增|增加|删除|移除|编辑|修改|保存|暂存|提交|取消|确认|关闭|重置|返回|上一步|下一步|上移|下移|收起|展开)/.test(s);
  const modOf = el => {
    const b = boxOf(el);
    if (b) {
      const t = b.querySelector('[class*="title"],[class*="Title"]');
      if (t) { const s = txt(t).replace(/^[*＊+\s]+/, ''); if (s && s.length <= 20) return s; }
      const ls = (b.innerText || '').split('\n').map(s => s.trim()).filter(s => s && !isBtnTxt(s));
      return ls.length ? ls[0].slice(0, 20) : '';
    }
    let n = el;
    for (let i = 0; i < 8 && n; i++) {
      const ls = (n.innerText || '').split('\n').map(s => s.trim()).filter(s => s && !isBtnTxt(s));
      if (ls.length) return ls[0].slice(0, 20);
      n = n.parentElement;
    }
    return '';
  };

  const entries = [];
  let k = 0;
  // 只认 a / button / [role=button] / [role=link]：这类入口在真实站点上都是可点控件。
  //   不把任意 div / span 的文案当入口 —— 少一点召回，换掉误触风险。
  document.querySelectorAll('a,button,[role="button"],[role="link"]').forEach(el => {
    if (!vis(el)) return;
    const t = txt(el);
    if (!t || t.length > 20) return;
    el.setAttribute('data-ows-exp', String(k));
    entries.push({ k: k, t: t, mod: modOf(el), ctrls: cntIn(boxOf(el)) });
    k++;
  });
  return { count: cntIn(document), entries: entries };
}
"""

LAST_EXPAND_STATS = {"入口数": 0, "点击次数": 0}

# 最近一次 scan_page 里**被判为污染而剔掉的标签**（5A 判据，2026-10-16 移植）。
# 为什么单列而不是塞进 returns：它是**旁路报告**，不影响字段表本身；
# 但"剔掉了什么"必须让用户看得见 —— 否则污染判据一旦误杀真字段名，
# 表现是"某个字段凭空消失"，用户无从判断是不是判据的锅。
LAST_SCAN_POLLUTED = []

# 最近一次面板采集的**闸门诊断**：{"闸门生效": bool, "剔除数": int, "候选数": int}。
# 为什么单列：它把"某项没填上"一分为二 —— 面板里**真没有**这个选项（站点问题，
# 该进清单让考生手填）vs **被归属闸剔掉了**（判据问题，该改代码）。不给这个区分，
# 排障只能靠猜；给了，一行日志就能定性。
LAST_PANEL_GATE = {"闸门生效": False, "剔除数": 0, "候选数": 0}

# 最近一次**搜索兜底**的落点：{"来源": "ctrl|host|panel", "关键字": str}。
# 为什么单列：「这次为什么填上了」和「这次为什么没填上」都要能回答。
# 来源=host 说明搜索框长在控件容器里（`showSearch` 型）；
# 来源=panel 说明走的是浮层内唯一 input。两种形态的失败原因完全不同。
LAST_SEARCH_PICK = {"来源": "", "关键字": ""}


def _count_controls(page):
    """当前页面上**可见**的输入控件数（只读，用于展开前 / 后做差）。失败返回 None。"""
    try:
        return page.evaluate("""() => {
            const vis = el => { const r = el.getBoundingClientRect();
                                return r.width > 0 && r.height > 0; };
            let n = 0;
            document.querySelectorAll('input,select,textarea').forEach(c => {
              if (c.tagName === 'INPUT' && (c.type === 'hidden' || c.type === 'file')) return;
              if (vis(c)) n++;
            });
            return n;
        }""")
    except Exception:                                        # noqa: BLE001
        return None


def _labels_now(page):
    """当前页面上带标签的可见控件标签清单（**只读**，用于展开前 / 后做差）。"""
    try:
        return [(f.get("label") or "") for f in scan_page(page)[0]]
    except Exception:                                        # noqa: BLE001
        return []


def _new_labels(before, after):
    """多重集差：`after` 里比 `before` 多出来的标签（按出现次序，保留重复）。"""
    from collections import Counter
    b, out = Counter(before), []
    for l in after:
        if b.get(l, 0) > 0:
            b[l] -= 1
        elif l:
            out.append(l)
    return out


def _exp_key(e):
    """增行入口的**去重键**：模块名优先；解析不出模块名时退回该入口的页面序号。

    退回序号而不是返回同一个常量 —— 否则所有「认不出模块名」的入口会塌成同一个键，
    第一个被点过之后，**其余模块全部再也点不到**。
    """
    m = (e.get("mod") or "").strip()
    return ("mod:" + m) if m else ("pos:%s" % e.get("k"))


def expand_blocks(page, max_entries=12, wait_ms=1600, log=None):
    """把页面上「＋ 添加」型模块的子表单**点开**，返回逐条日志。

    做法：每轮**重新标记**页面上所有增行入口 → 取「还没点过的模块」里的第一个 →
    滚动到可见 → 真点击 → 等渲染 → 记「可见控件数」的增减 + **新增出来的字段标签**。

    返回 `[{"模块","入口","点开前","点开后","新增","新增字段","新增字段数","结果"}]`，
    「结果」∈ `已展开` / `没反应` / `异常`。
    **本函数不填任何值、不删任何行、绝不点提交类按钮。**

    ⚠ 去重键 = **模块名**（不是按钮）。这是本能力的红线：同一个模块点两次就多出一条
      空记录，等于替你造数据。

    ⚠⚠ **这条红线在实测中【未根治】，如实交底**（判据移植自 LYSKILL，2026-10-02）：
      · 现象：某个模块被点了**两次**。根因不是同名重复，而是**同一模块点开后又会冒出
        新的「＋ 添加」** —— 于是靠名称去重失效，靠元素标记同样失效（新渲染出的元素）。
      · 目前**唯一有效的判据**是「**容器里已有可见控件 ⇒ 该模块已展开过 ⇒ 跳过**」：
        它是**结构事实**，不依赖命名（名字会塌成「+ 添加」/「删除」），也不依赖 DOM 路径
        （实测展开前后 0/8 存活，页面重排路径全变）。容器定位不出时（`ctrls == -1`）
        **不据此跳过**，退回旧行为 —— 宁可多点，不拿一个猜出来的容器当判据。
      · 危害等级：中低（多出的空行通常**不会落库**，可目视、可手删），**但未消除**。
      · 加固方式：报告固定输出「入口数 / 点击次数」两个数，多点一眼就能看见。
    """
    global LAST_EXPAND_STATS
    out, done = [], set()
    LAST_EXPAND_STATS = {"入口数": 0, "点击次数": 0}
    for _ in range(max(0, int(max_entries))):
        try:
            info = page.evaluate(EXPAND_JS)
        except Exception as e:                               # noqa: BLE001
            out.append({"模块": "", "入口": "", "点开前": None, "点开后": None,
                        "新增": None, "新增字段": [], "新增字段数": 0,
                        "结果": "异常", "原因": str(e)[:120]})
            break
        entries = info.get("entries") or []
        n_entry = len([x for x in entries if is_add_entry(x.get("t") or "")])
        LAST_EXPAND_STATS["入口数"] = max(LAST_EXPAND_STATS["入口数"], n_entry)

        pick = None
        for e in entries:
            if not is_add_entry(e.get("t") or ""):
                continue
            key = _exp_key(e)
            if key in done:
                continue
            c = e.get("ctrls")
            if int(c if c is not None else -1) > 0:          # ← 治本判据（见上）
                done.add(key)
                continue
            pick = e
            break
        if pick is None:
            break

        before = info.get("count")
        rec = {"模块": pick.get("mod") or "(未识别模块名)", "入口": pick.get("t"),
               "点开前": before, "点开后": None, "新增": None,
               "新增字段": [], "新增字段数": 0, "结果": ""}
        try:
            loc = page.locator('[data-ows-exp="%d"]' % int(pick["k"])).first
            loc.scroll_into_view_if_needed(timeout=8000)
            page.wait_for_timeout(300)
            lab_before = _labels_now(page)
            loc.click(timeout=6000)
            LAST_EXPAND_STATS["点击次数"] += 1
            page.wait_for_timeout(int(wait_ms))
            lab_after = _labels_now(page)
            new = _new_labels(lab_before, lab_after)
            after = _count_controls(page)
            grew = (after is not None and before is not None and after > before)
            rec.update({"点开后": after,
                        "新增": (after - before)
                        if (after is not None and before is not None) else None,
                        "新增字段": new, "新增字段数": len(new),
                        "结果": "已展开" if (new or grew) else "没反应"})
            done.add(_exp_key(pick))
        except Exception as e:                               # noqa: BLE001
            rec.update({"结果": "异常", "原因": str(e)[:120]})
            out.append(rec)
            break
        out.append(rec)
        if log:
            log(rec)
    return out


def scan_page(page):
    """
    扫描页面表单字段。**穿透 iframe 与 shadow DOM**（2026-10-16）。

    返回 (fields, frames_by_idx)：
      · fields       每个控件一个 dict，`idx` 是**全局唯一字符串**
      · frames_by_idx  idx → 该控件所在的 frame（供写入时用同一个 frame 定位）
    主文档前缀为空；第 i 个子 frame 前缀 `f<i>_`，保证 idx 不撞车。

    另把「被判为污染而剔掉的标签」记进模块级 `LAST_SCAN_POLLUTED`（跨 frame 合并、
    顺序即扫描顺序）。为什么不当第三个返回值：调用点有 6 处，改返回值等于把
    「要不要看污染」这件事强加给每一处；而它本来就只是个**旁路报告**，
    与 `LAST_EXPAND_STATS` 同一手法。
    """
    global LAST_SCAN_POLLUTED, LAST_PANEL_GATE, LAST_SEARCH_PICK
    fields, frames_by_idx = [], {}
    polluted = []
    # 每次扫描 = 一次"新页面"边界 ⇒ 三份诊断一起归零。
    #   不归零的后果是**上一页的数字挂到这一页的结论上**，比没有诊断更坏。
    LAST_PANEL_GATE = {"闸门生效": False, "剔除数": 0, "候选数": 0}
    LAST_SEARCH_PICK = {"来源": "", "关键字": ""}
    scopes = [(page.main_frame, "")]
    for i, fr in enumerate(page.frames):
        if fr is page.main_frame:
            continue
        scopes.append((fr, "f%d_" % i))

    for fr, pre in scopes:
        try:
            got = fr.evaluate(SCAN_JS, pre)
        except Exception:                                    # noqa: BLE001
            # 跨域 iframe / 已销毁的 frame：跳过，不因此整体失败
            continue
        # 兼容两种形态：新契约是 dict，旧契约定是纯数组。**不猜就不炸** ——
        # 万一有人回滚了 SCAN_JS 而忘了回滚这里，也不该整轮报错。
        if isinstance(got, dict):
            rows, bad = got.get("fields") or [], got.get("polluted") or []
        else:
            rows, bad = got or [], []
        for t in bad:
            if t not in polluted:
                polluted.append(t)
        for f in rows:
            frames_by_idx[f["idx"]] = fr
            f["frame"] = pre or "main"
            fields.append(f)
    LAST_SCAN_POLLUTED = polluted
    return fields, frames_by_idx


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


def apply_plan(page, plan, fields_by_idx, frames_by_idx=None, deadline=None):
    """
    执行填写。返回 {已填, 失败, 跳过, 日期补全, 超时中止, 假成功, 面板写入}。
    **本函数不触碰任何提交类按钮。**

    `deadline` = 绝对时间戳（time.time() 口径）。到点即停手 —— 已完成的部分
    照常保留在页面上，未完成的不再往下试，交给「可复制清单页」。
    `frames_by_idx`：idx → 该控件所在 frame（iframe / shadow DOM 场景必需）。
    """
    filled, failed, skipped, adjusted = 0, [], [], []
    timed_out = False
    fake = 0                     # 假成功计数（写入返回成功、回读为空/值=标签）
    paneled = 0                  # 靠面板点选才写进去的（自绘日历 / 级联）
    frames_by_idx = frames_by_idx or {}

    for item in plan:
        if deadline is not None and time.time() > deadline:
            timed_out = True
            break
        if item["状态"] == "档案缺项":
            skipped.append({"label": item["label"], "原因": "档案缺项"})
            continue

        f = fields_by_idx.get(item["idx"], {})
        frame = frames_by_idx.get(item["idx"]) or page.main_frame
        # 身份解析（串位防线）：不唯一就拒写并报明原因 —— 绝不换一个元素顶上。
        sel, why = resolve_sel(frame, item["idx"])
        if sel is None:
            failed.append({"label": item["label"], "原因": why})
            continue
        try:
            if item["type"] in ("radio", "checkbox"):
                ok = _fill_choice(frame, sel, item, f)
            elif item["type"] == "select" or f.get("tag") == "select":
                ok = _fill_select(frame, item, f, sel)
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
                ok = _force_fill(frame, sel, v)

            # ---- 面板化兜底：只读框 / 自绘日历 / 级联选择器 ----
            # `fill` 与「JS 直写 value」对这类控件都会假成功（组件把值渲染在
            # 自己的面板里，DOM 的 value 根本没动）。必须点开面板、点中那一项。
            if (not ok or not _read_back(frame, item["idx"], item["type"])) \
                    and item["type"] not in ("radio", "checkbox", "select"):
                kind = panel_kind(item, f)
                if kind and _panel_fill(frame, sel, item, kind, deadline):
                    filled += 1
                    paneled += 1
                    continue

            if not ok:
                failed.append({"label": item["label"], "原因": "选项未匹配 / 控件不可写"})
                continue

            # ---- 回读验证：写入返回 ≠ 真的写进去了 ----
            got = _read_back(frame, item["idx"], item["type"])
            if got is None:
                fake += 1
                failed.append({"label": item["label"],
                               "原因": "回读时元素身份失效（写入过程中页面重渲染），无法确认是否填上"})
                continue
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
            failed.append({"label": item["label"], "原因": friendly_error(e)})
    return {"已填": filled, "失败": failed, "跳过": skipped,
            "假成功": fake, "超时中止": timed_out, "日期补全": adjusted,
            "面板写入": paneled}


def _fill_choice(frame, sel, item, field):
    """单选/复选：按选项文本匹配（含值域别名）。"""
    val = str(item["value"]).strip()
    name = field.get("name") or ""
    if name:
        opts = frame.query_selector_all('input[name="%s"]' % name)
    else:
        opts = [frame.query_selector(sel)]

    pairs = []                     # [(选项文字, 元素)]
    for o in opts:
        if not o:
            continue
        oid = o.get_attribute("id") or ""
        lab = ""
        if oid:
            l = frame.query_selector('label[for="%s"]' % oid)
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


def _fill_select(frame, item, field, sel=None):
    """下拉框：精确 → 值 → 值域别名 → 模糊。`sel` 由调用方校验唯一性后传入。"""
    val = str(item["value"]).strip()
    if sel is None:
        sel = '[data-ows-idx="%s"]' % item["idx"]
    try:
        frame.select_option(sel, label=val)
        return True
    except Exception:                                        # noqa: BLE001
        pass
    try:
        frame.select_option(sel, value=val)
        return True
    except Exception:                                        # noqa: BLE001
        pass

    opts = field.get("options") or []
    hit = core.match_option(val, opts)
    if hit:
        try:
            frame.select_option(sel, label=hit)
            return True
        except Exception:                                    # noqa: BLE001
            pass
    for o in opts:
        if val and (val in o or o in val):
            try:
                frame.select_option(sel, label=o)
                return True
            except Exception:                                # noqa: BLE001
                continue
    return False


def resolve_sel(frame, idx):
    """
    把 idx 解析成**唯一命中**的选择器；不唯一就返回 (None, 原因)。

    为什么不能直接用 querySelector
    ------------------------------
    `querySelector` 遇到多个匹配会**静默取第一个**。页面重渲染后，同一个 idx 可能同时
    挂在新旧两批元素上 —— 取到旧节点，值就写进了上一个字段（串位）；而回读用的是同一个
    选择器，读回来照样"正常"，**串位因此天然不可见**。

    口径按三档收紧（移植 LYSKILL `form_engine.py` 的 `byHandle` 判据）：
      · 唯一命中  → 用它。这是扫描时认下的同一个元素
      · 命中 0 个 → 「元素身份失效」（页面已重建 / 控件被换掉）—— **绝不换一个顶上**
      · 命中多个  → 「身份歧义」—— 同样拒写，不挑一个顶上

    **宁可填不上，绝不填错：填不上用户看得见，填错用户看不见。**
    """
    sel = '[data-ows-idx="%s"]' % idx
    try:
        n = frame.eval_on_selector_all(sel, "els => els.length")
    except Exception:                                        # noqa: BLE001
        return None, "元素查询失败（页面已重建？）"
    if n == 1:
        return sel, ""
    if n == 0:
        return None, "元素身份失效（扫描后页面已重建）"
    return None, "身份歧义（%d 个元素带同一标识）" % n


def _force_fill(frame, sel, value):
    """
    写入：先常规 fill；失败（只读框、自绘控件）再走 JS 直写并派发 input/change。
    站点策略库 P-003/P-006：只读日历/级联用 page.fill 会直接失败或超时。
    返回值：True = 已写入（**读回验证在调用方做**）。
    """
    try:
        frame.fill(sel, value)
        return True
    except Exception:                                        # noqa: BLE001
        pass
    try:
        frame.eval_on_selector(sel, """(el, v) => {
            el.removeAttribute('readonly');
            el.focus();
            el.value = v;
            el.dispatchEvent(new Event('input',  {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
        }""", value)
        return True
    except Exception:                                        # noqa: BLE001
        return False


# --------------------------------------------------------------------------
# 面板化写入：点开 → 点选（P0-10 补全）
# --------------------------------------------------------------------------
def _panel_pick(frame, wanted_list):
    """
    在**当前打开的面板**里找并点中一项。
    `wanted_list` 是候选写法，**按顺序**从严到宽试（如 ['2026-06-30','2026/06/30','30']）。
    """
    items = _panel_items(frame)
    if not items:
        return False
    # ① 归一后精确相等（最严，先试）
    for want in wanted_list:
        nv = core.norm(want)
        if not nv:
            continue
        for it in items:
            if core.norm(it["text"]) == nv:
                return _panel_click(frame, it["k"])
    # ② 值域别名 / 互为包含
    texts = [it["text"] for it in items]
    for want in wanted_list:
        hit = core.match_option(want, texts)
        if hit:
            for it in items:
                if it["text"] == hit:
                    return _panel_click(frame, it["k"])
    return False


def _search_pick(frame, ctl_sel, wanted_list):
    """往该控件**自己的**搜索框注入关键字，重采面板后点选。返回是否点中。

    为什么要单独一步而不是"点开时顺便打字"：大多数面板**不需要**搜索
    （普通下拉、日历），无条件打字会给它们塞进过滤词 ⇒ 把本来能选中的项过滤掉。
    只有在「直接点选失败」之后才动用它 —— 这是兜底，不是主路径。
    """
    global LAST_SEARCH_PICK
    want = ""
    for w in wanted_list:
        if str(w or "").strip():
            want = str(w).strip()
            break
    if not want:
        return False
    try:
        where = frame.evaluate(FIND_SEARCHBOX_JS, ctl_sel)
    except Exception:                                        # noqa: BLE001
        return False
    if not where or where == "none":
        return False
    try:
        # 先清空再打字：不少组件把已有的过滤词留着，直接 fill 会叠成「旧词+新词」
        # ⇒ 过滤结果恒为空，而失败表现完全一样（"面板里没有这一项"），无从分辨。
        frame.fill('[data-ows-search]', "")
        frame.fill('[data-ows-search]', want)
        frame.wait_for_timeout(PANEL_SETTLE_MS + 300)
    except Exception:                                        # noqa: BLE001
        return False
    LAST_SEARCH_PICK = {"来源": where, "关键字": want}
    return _panel_pick(frame, wanted_list)


def _date_candidates(val):
    """日期面板的候选写法：整串 → 斜杠写法 → 只留「日」。"""
    s = str(val or "").strip()
    out = [s]
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        out.append(s.replace("-", "/"))
        out.append(s.replace("-", ""))
        out.append(s.split("-")[-1])            # 日历格子里写的是「30」
    elif re.match(r"^\d{4}-\d{2}$", s):
        out.append(s.replace("-", "/"))
    return [x for x in out if x]


def _panel_fill(frame, sel, item, kind, deadline=None):
    """
    面板化写入。返回 True 表示**面板里点选成功**（回读仍由调用方复核）。

    为什么不做「只在某个站点上写死选择器」
    ------------------------------------
    面板结构是**组件库**决定的，不是站点决定的。这里只按「可见 + 落在弹层里 +
    文本够短」三条通用判据采集候选项，再靠文本匹配点中 —— 换站点照样能用。
    """
    def alive():
        return deadline is None or time.time() <= deadline

    if not alive():
        return False
    val = str(item.get("value") or "")
    el = None
    try:
        el = frame.query_selector(sel)
    except Exception:                                        # noqa: BLE001
        return False
    if not el:
        return False
    try:
        el.scroll_into_view_if_needed(timeout=3000)
    except Exception:                                        # noqa: BLE001
        pass

    # 日期：先试「聚焦后键盘直输」—— 不少组件库认键盘，比点日历稳
    if kind == "date":
        try:
            el.click()
            frame.wait_for_timeout(PANEL_SETTLE_MS)
            frame.fill(sel, val)
            # 回车确认（2026-10-16 由 LYSKILL 移植）：一批日历组件「打完字不回车
            # 就不认」（antd v3 的 `.ant-calendar-input` 正是此形态）。
            # ⚠ **只在确已弹出面板时按** —— 面板没弹出来时空按回车，可能触发页面
            #   自己的「回车即提交」默认行为，而「绝不点提交」是本技能的硬红线。
            #   宁可少按一次回车（多一步点日历格兜底），也绝不冒一次误提交的险。
            if _panel_items(frame):
                try:
                    frame.press(sel, "Enter")
                    frame.wait_for_timeout(PANEL_SETTLE_MS)
                except Exception:                            # noqa: BLE001
                    pass
        except Exception:                                    # noqa: BLE001
            pass
        if _read_back(frame, item["idx"], item["type"]):
            return True

    # 点开面板
    # ⚠ 必须先快照已可见浮层（归属闸用），再点击 —— 顺序反了，本次打开的面板
    #   会被自己的标记认成"早就开着"，闸门随即整体失效（**静默失效**，最坏的一种）。
    _snap_overlays(frame)
    try:
        el.click()
    except Exception:                                        # noqa: BLE001
        return False
    frame.wait_for_timeout(PANEL_SETTLE_MS)

    if kind == "date":
        targets = [_date_candidates(val)]
    else:
        segs = split_region(val)
        targets = [[seg] for seg in segs[:PANEL_MAX_LEVELS]]
        if not targets:
            return False

    hit_any = False
    for wanted in targets:
        if not alive():
            break
        if _panel_pick(frame, wanted):
            hit_any = True
            frame.wait_for_timeout(PANEL_SETTLE_MS)
            continue
        # ---- 搜索兜底（2026-10-16 由 LYSKILL · 3A/4A 判据移植）----
        #   虚拟列表型下拉点开后可能**一个候选项都没有**（要等关键字才渲染）。
        #   再给一次机会：往**它自己的**搜索框注关键字 → 重采 → 再点。
        #   见 FIND_SEARCHBOX_JS 上方注释：只认「本控件容器内」与「唯一浮层内恰好一个」，
        #   多一个少一个都不写（"最近的下方文本框"那条路会静默串位，已实测证伪）。
        if kind != "date" and _search_pick(frame, sel, wanted):
            hit_any = True
            frame.wait_for_timeout(PANEL_SETTLE_MS + 300)
            continue
        # 该级没命中：日期再退一步试「只按日」；级联则到此为止（不猜下一级）
        if kind == "date":
            tail = val.split("-")[-1] if "-" in val else val
            if tail and tail not in wanted and _panel_pick(frame, [tail]):
                hit_any = True
                frame.wait_for_timeout(PANEL_SETTLE_MS)
        else:
            break
    return hit_any


READ_VALUE_JS = """
(el) => {
  const v = (el.value == null ? '' : String(el.value));
  if (v) return v;
  // 自绘级联：展示值在 .ant-cascader-picker-label 里，input.value **恒为空** ——
  //   回读侧不补这一读，"填写成功"就会被二次确认判成"假成功"
  //   （与写入侧是同一个坑，必须同源修）。
  if (/cascader/i.test(String(el.className || ''))) {
    let e2 = el, lb = null;
    for (let k = 0; k < 5 && e2 && !lb; k++) {
      lb = (e2.querySelector && e2.querySelector('[class*="cascader-picker-label"]')) || null;
      e2 = e2.parentElement;
    }
    if (lb) return (lb.textContent || '').trim();
  }
  return '';
}
"""


def _read_back(frame, idx, kind):
    """
    回读控件当前值。**这是「已填」的唯一判据** ——
    以前 page.fill 返回就算成功，自绘控件把值丢掉也照样记「已填」（假成功）。

    返回值三态（2026-10-16 加前两态，判据取自 LYSKILL `VERIFY_JS`）：
      · None  —— **元素身份失效**：回读时该控件已不在页面上。它**不等于"值为空"**，
                 更不许冒充"空字段"—— 那会把「页面重建」误报成「没填上」，两件事
                 的处置完全不同。由调用方单列。
      · ""    —— 元素在，值确实为空。
      · 有值  —— 真写进去了。

    取不到值时**不静默返回空串**：静默的"空"正是假成功的温床。
    """
    sel, why = resolve_sel(frame, idx)
    if sel is None:
        return None
    try:
        if kind in ("radio", "checkbox"):
            return "1" if frame.eval_on_selector(sel, "el => !!el.checked") else ""
        v = frame.eval_on_selector(sel, READ_VALUE_JS)
        return (v or "").strip()
    except Exception:                                        # noqa: BLE001
        return None


# --------------------------------------------------------------------------
# 页面就绪等待（Backlog-7）
# --------------------------------------------------------------------------
# 固定 `wait_for_timeout(3000)` 两头不讨好：慢站没加载完就开始扫（扫到 0 个字段），
# 快站白等 3 秒 ×N 站，把 300 秒预算浪费在空等上。
# ⇒ 改成「等控件出现（有上限）＋ 短促沉降」，预算紧张时自动缩短。
SETTLE_MS = 600


def wait_ready(page, timeout_ms, settle_ms=SETTLE_MS):
    """
    等页面出现可填控件。返回 (是否等到, 说明)。
    等不到不报错 —— 交给后面的「扫到 0 个字段」停手逻辑去说明原因。
    """
    try:
        page.wait_for_selector("input,select,textarea",
                               state="attached", timeout=max(1000, timeout_ms))
        waited = "已等到输入控件"
    except Exception:                                        # noqa: BLE001
        waited = "等待超时（页面可能还没加载完 / 内容在 iframe 里）"
    try:
        page.wait_for_timeout(settle_ms)
    except Exception:                                        # noqa: BLE001
        pass
    return ("超时" not in waited), waited


# --------------------------------------------------------------------------
# 错误信息中文友好（Backlog-8 · P1-15）
# --------------------------------------------------------------------------
ERROR_HINTS = (
    ("Timeout", "等待超时（页面太慢或元素不存在）"),
    ("timeout", "等待超时（页面太慢或元素不存在）"),
    ("not visible", "控件不可见（可能被折叠/隐藏）"),
    ("intercepts pointer events", "控件被浮层挡住（先关掉弹窗/遮罩）"),
    ("strict mode violation", "页面上有多个同名控件，无法确定填哪一个"),
    ("Element is not attached", "控件已被页面刷新掉（表单重绘）"),
    ("Target closed", "浏览器页面被关闭"),
    ("net::ERR", "页面打不开（网络或地址问题）"),
    ("Execution context was destroyed", "页面在填写途中跳转/重载"),
)


def friendly_error(exc):
    """把 playwright 的英文异常翻译成「人话 + 下一步」。

    译文在**前**（让人看懂），原始信息在**后**（截断保留）：不能把诊断线索吃掉 ——
    报问题时「30000ms exceeded」和「15000ms exceeded」是两条不同的线索，
    只回一句「等待超时」等于把线索丢了。
    """
    raw = str(exc).split("\n")[0].strip()
    if not raw:
        return "未知错误"
    for key, cn in ERROR_HINTS:
        if key.lower() in raw.lower():
            return "%s（原始信息：%s）" % (cn, raw[:80])
    return raw[:80]


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
        # 统一 safe_write：显式路径 ⇒ 允许覆盖（用户自己点的名）
        return core.safe_write(args.html,
                               sheet_html.render_html(ready, miss, meta),
                               overwrite=True)
    except Exception as exc:                              # noqa: BLE001
        print("   （可复制页没能生成：%s）" % friendly_error(exc))
        return ""


def exit_code(report):
    """收尾退出码。**提前停手 ⇒ 5**（fail-visible），正常走完 ⇒ 0。

    抽成纯函数是为了能被自测直接断言 —— 原先只有一行内联表达式，
    测试只能去匹配源码字符串，改个格式就假红。
    """
    return 5 if report.get("停止原因") else 0


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
            wait_ready(page, min(step_to, 3000))
        else:
            print("[1/5] 打开页面：%s" % args.url)
            page.goto(args.url, wait_until="domcontentloaded")
            # 等「真的出现输入控件」，不再固定空等 3 秒（Backlog-7）
            hit, why = wait_ready(page, args.wait)
            print("      %s" % why)
        report["接管模式"] = bool(cdp_url)

        if args.expand:
            print("[2/5] 展开「＋ 添加」型模块（--expand）")
            exp = expand_blocks(page, log=lambda r: print(
                "      %-18s %s" % (r["模块"][:18], r["结果"])))
            report["展开"] = exp
            report["展开汇总"] = dict(LAST_EXPAND_STATS)
            print("      入口 %d ／ 点击 %d ／ 成功展开 %d"
                  % (LAST_EXPAND_STATS["入口数"], LAST_EXPAND_STATS["点击次数"],
                     len([r for r in exp if r["结果"] == "已展开"])))
            # 展开改变了 DOM ⇒ 等一拍再扫，否则扫到的是「展开到一半」的状态
            page.wait_for_timeout(600)

        print("[2/5] 扫描表单字段")
        fields, frames_by_idx = scan_page(page)
        report["页面标题"] = page.title()
        report["字段总数"] = len(fields)
        iframes = len({f["frame"] for f in fields if f["frame"] != "main"})
        print("      发现 %d 个输入控件%s"
              % (len(fields), "（含 %d 个 iframe）" % iframes if iframes else ""))
        # 污染剔除**必须说出来**：判据一旦误杀真字段名，表现是"某字段凭空消失"，
        #   不给名单，用户无从判断是判据的锅还是页面本来就没有。
        if LAST_SCAN_POLLUTED:
            report["标签污染剔除"] = list(LAST_SCAN_POLLUTED)
            print("      剔掉 %d 条「像字段名但不是」的文本（提示语 / 已选值回显）：%s"
                  % (len(LAST_SCAN_POLLUTED), "、".join(LAST_SCAN_POLLUTED[:5])))

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
            res = apply_plan(page, plan, fields_by_idx,
                             frames_by_idx=frames_by_idx, deadline=deadline)
            print("      已填 %d ／ 失败 %d ／ 跳过 %d"
                  % (res["已填"], len(res["失败"]), len(res["跳过"])))
            if res.get("面板写入"):
                print("      其中 %d 个是「自绘控件」——靠点开面板逐级点选才填进去的"
                      % res["面板写入"])
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
            # ---- 面板诊断（2026-10-16 · 三向同步新增）----
            # 这一段存在的唯一目的：把「某项没填上」一分为二 ——
            #   面板里**真没有**这个选项（站点问题，该进清单让考生手填）
            #   vs **被归属闸剔掉了**（判据问题，该改代码）。
            # 不给这个区分，排障只能靠猜，而"猜"在这里的代价就是改错地方。
            gate = dict(LAST_PANEL_GATE)
            if gate["剔除数"]:
                report["面板归属闸"] = gate
                print("      归属闸剔除 %d 个「别人家面板」的候选项（防点到隔壁下拉；"
                      "闸门%s）" % (gate["剔除数"], "生效" if gate["闸门生效"] else "未生效"))
            if LAST_SEARCH_PICK.get("来源"):
                report["搜索兜底"] = dict(LAST_SEARCH_PICK)
                print("      搜索兜底生效：搜索框来源=%s，关键字=%s"
                      % (LAST_SEARCH_PICK["来源"], LAST_SEARCH_PICK["关键字"]))

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
            # 截图由 playwright 直接写（二进制），这里只保证目录存在。
            # 显式路径＝用户自己的选择，不做基准限制（见 core.safe_write 的说明）。
            core.ensure_dir(args.screenshot)
            page.screenshot(path=args.screenshot, full_page=True)
            print("[5/5] 已截图：%s" % args.screenshot)
        else:
            print("[5/5] 未指定 --screenshot")

        # 报告**先落盘**：收尾保险丝一旦触发会直接结束进程，任何产物都必须
        # 在武装它之前写完，否则会丢（只含键名与状态，不含真值）。
        if args.out:
            # 走统一 safe_write：同名不覆盖（改存带时间戳）+ 原子替换。
            # `--out` 是用户显式点名的目标 ⇒ 视为授权覆盖。
            wrote = core.safe_write(
                args.out, json.dumps(report, ensure_ascii=False, indent=2),
                overwrite=True, root=os.path.dirname(os.path.abspath(args.out)))
            print("报告已写入：%s" % wrote)

        # —— 收尾。先上保险丝再关浏览器。
        # 退出码必须**在武装保险丝之前**算好：保险丝写死 rc=0 会把
        # 「提前停手（应=5）」污染成「成功」。
        final_rc = exit_code(report)
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
    return exit_code(report)


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
    ap.add_argument("--wait", type=int, default=8000,
                    help="等「出现输入控件」的上限毫秒（默认 8000；不再固定空等 3 秒）")
    ap.add_argument("--screenshot", help="截图输出路径")
    ap.add_argument("--out", help="报告 JSON 输出路径")
    ap.add_argument("--dry-run", action="store_true", help="只映射不写入")
    ap.add_argument("--expand", action="store_true",
                    help="先点开「＋ 添加」型重复模块（记录组类站点必备）再扫描")
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
           readonly=False, name="n%d" % 0, panel=False):
        return {"idx": idx, "label": label, "type": typ, "name": name, "id": "",
                "options": options, "visible": visible, "inForm": inform,
                "readonly": readonly, "panel": panel, "frame": "main"}

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

    # ---- 面板化写入的纯函数（P0-10）----
    check("⑫ 地区串逐级切分",
          split_region("山东省济南市历下区") == ["山东省", "济南市", "历下区"],
          split_region("山东省济南市历下区"))
    check("⑬ 直辖市两级切分",
          split_region("北京市海淀区") == ["北京市", "海淀区"],
          split_region("北京市海淀区"))
    check("⑭ 自治区不被切碎",
          split_region("内蒙古自治区呼和浩特市")[0] == "内蒙古自治区",
          split_region("内蒙古自治区呼和浩特市"))
    check("⑮ 切不动时整体作一段",
          split_region("东城区") == ["东城区"], split_region("东城区"))
    check("⑯ 只读的籍贯框判为级联", panel_kind(
        {"label": "籍贯", "key": "籍贯", "type": "text", "value": "山东省济南市历下区"},
        mk(0, "籍贯", readonly=True, panel=True)) == "cascade")
    check("⑰ 日期框判为日期", panel_kind(
        {"label": "出生日期", "key": "出生日期", "type": "date", "value": "2000-01-01"},
        mk(0, "出生日期", "date", readonly=True, panel=True)) == "date")
    check("⑱ 普通文本框不走面板", panel_kind(
        {"label": "姓名", "key": "姓名", "type": "text", "value": "张某"},
        mk(0, "姓名")) == "")
    check("⑲ 日期候选取整串与「日」",
          _date_candidates("2026-06-30")[:1] == ["2026-06-30"]
          and "30" in _date_candidates("2026-06-30"))

    # ---- 错误信息中文化（P1-15）----
    check("⑳ 超时异常被译成人话",
          "超时" in friendly_error(Exception("Timeout 45000ms exceeded")),
          friendly_error(Exception("Timeout 45000ms exceeded")))
    check("㉑ 未知异常不吞掉原文",
          friendly_error(Exception("SomeWeirdError: abc")) .startswith("SomeWeirdError"))

    # ---- 身份解析三档 ＋ 回读三态（2026-10-16 · 串位防线）----
    # 这一段盯的是**京东实测缺陷**（微信号←毕业院校）。防线写在代码里不算数，
    # 得有一条会红的断言看着它 —— 判据强度 = 执行点数量。
    class _F(object):
        """只实现 resolve_sel 用到的那一个方法，别的一概不管。"""
        def __init__(self, n, boom=False):
            self.n, self.boom = n, boom

        def eval_on_selector_all(self, sel, js):
            if self.boom:
                raise RuntimeError("frame detached")
            return self.n

    _s, _w = resolve_sel(_F(1), "0")
    check("㉒ 身份唯一命中 → 用它", _s == '[data-ows-idx="0"]' and _w == "", (_s, _w))
    _s, _w = resolve_sel(_F(0), "0")
    check("㉓ 命中 0 个 → 判「身份失效」并拒写（不换元素顶上）",
          _s is None and "失效" in _w, (_s, _w))
    _s, _w = resolve_sel(_F(3), "0")
    check("㉔ 命中多个 → 判「身份歧义」并拒写（不挑一个顶上）",
          _s is None and "歧义" in _w, (_s, _w))
    _s, _w = resolve_sel(_F(0, boom=True), "0")
    check("㉕ 查询抛错 → 也不放行（宁拒勿错）", _s is None, (_s, _w))

    # 反证：身份不可用时回读必须返回 None，**不能退化成一个空串** ——
    #   空串会让调用方把「页面重建」直接说成「该字段为空」，两件事处置完全不同。
    check("㉖ 身份失效时回读返回 None（不是空串），不冒充空字段",
          _read_back(_F(0), "0", "text") is None)
    check("㉗ 命中多个时回读同样返回 None",
          _read_back(_F(2), "0", "text") is None)

    # 执行点（**顺序**判据，不只看"有没有"）：清理必须发生在打属性之前。
    #   只断言"含 removeAttribute"会骗人 —— 顺序反了照样能过。LYSKILL 的教训正是
    #   「留到写入期才补打属性，等于把漂移固化成身份」，这里盯的就是这个先后。
    _clean = SCAN_JS.index("removeAttribute('data-ows-idx')")
    _stamp = SCAN_JS.index("setAttribute('data-ows-idx'")
    check("㉘ 扫描期「先清后打」（顺序判据）", _clean < _stamp, (_clean, _stamp))

    # ---- 展开型模块：增行入口判据（红线 = 只点增行，绝不点保存/提交）----
    check("㉙ 「＋ 添加」判为入口（全角加号、空格都属同一种写法）",
          is_add_entry("＋ 添加") is True and is_add_entry("+添加") is True)
    check("㉚ 纯「添加」判为入口", is_add_entry("添加") is True)
    check("㉛ 「新增」「增加」都算", is_add_entry("新增") and is_add_entry("增加"))
    check("㉜ **红线**：含「保存」即拒（保存并添加）", is_add_entry("保存并添加") is False)
    check("㉝ **红线**：含「提交」即拒（添加并提交）", is_add_entry("添加并提交") is False)
    check("㉞ **红线**：含「删除」即拒（添加/删除）", is_add_entry("添加/删除") is False)
    check("㉟ 与增行无关的按钮不误判（下一步 / 全部展开）",
          is_add_entry("下一步") is False and is_add_entry("全部展开") is False)
    check("㊱ 超长文案不当入口（长度上限 12 字）",
          is_add_entry("添加一条新的教育经历记录信息") is False)
    check("㊲ 空 / 纯装饰字符 → 拒", is_add_entry("") is False and is_add_entry("＋") is False)

    # ---- 去重键：退回序号，不许全塌成一个键 ----
    check("㊳ 有模块名 → 用模块名做去重键",
          _exp_key({"mod": "教育实习", "k": 3}) == "mod:教育实习")
    check("㊴ 无模块名 → 退回入口序号（否则其余模块再也点不到）",
          _exp_key({"mod": "", "k": 3}) == "pos:3"
          and _exp_key({"mod": "", "k": 4}) == "pos:4")

    # ---- 多重集差：新增字段算得对 ----
    check("㊵ 多出来的标签算得对", _new_labels(["a", "b"], ["a", "b", "c"]) == ["c"])
    check("㊶ 重复标签不被吞掉（a 只多一条、b 是新的）",
          _new_labels(["a"], ["a", "a", "b"]) == ["a", "b"])
    check("㊷ 展开前后没变化 → 差为空（不能瞎报「已展开」）",
          _new_labels(["a"], ["a"]) == [])

    # ================= 三向同步新增（2026-10-16 · OWSH ← LYSKILL）=================
    # 判据分两类：JS 源码判据（只能读文本）+ Python 行为判据（用假 frame 喂）。
    # 每条都配反证 —— 只断言"含某个字符串"会被自指与注释骗到（LYSKILL 的教训）。

    # ---- 归属闸：闸门失灵时必须放行（只许收窄），且必须能剔除域外候选 ----
    check("㊸ 归属闸：页面上没有「未标记的可见浮层」时 gateOn=false ⇒ 原样放行",
          "const gateOn = fresh.length > 0;" in PANEL_ITEMS_JS
          and "if (!gateOn) return true;" in PANEL_ITEMS_JS)
    check("㊹ 归属闸：明确属于别的面板 ⇒ 剔除（有且只有这一条窄化出口）",
          "明确属于别的面板" in PANEL_ITEMS_JS
          and "excluded++" in PANEL_ITEMS_JS)

    # ---- 顺序判据（不是"含没含"）：先清后打 ----
    #   顺序反了照样能过"含 removeAttribute"这种弱断言，而漏掉的正是整条判据的价值。
    check("㊺ 浮层快照「先清后打」（清理必须发生在打标记之前）",
          SNAP_OVERLAYS_JS.index("removeAttribute('data-ows-prev')")
          < SNAP_OVERLAYS_JS.index("setAttribute('data-ows-prev'"))
    check("㊻ 搜索框标记同样「先清后打」",
          FIND_SEARCHBOX_JS.index("removeAttribute('data-ows-search')")
          < FIND_SEARCHBOX_JS.index("setAttribute('data-ows-search'"))

    # ---- 反证：搜索兜底**绝不允许**退回"全页找最近的可编辑文本框"----
    #   那条路在网申页竖排布局下几乎必然抓到下一个字段的输入框 ⇒ 静默串位。
    #   断言"不含全页 input 采集"+"必须按控件自身几何取浮层"两条同时成立。
    check("㊼ 反证：搜索兜底不走「全页找文本框」（只认控件容器内 / 唯一浮层内）",
          "document.querySelectorAll('input'" not in FIND_SEARCHBOX_JS
          and "const r = ctl.getBoundingClientRect();" in FIND_SEARCHBOX_JS
          and "if (boxes.length !== 1) return 'none';" in FIND_SEARCHBOX_JS)

    # ---- 污染判据存在，且 SCAN_JS 已改新契约（否则 polluted 出不来）----
    check("㊽ 标签污染判据存在：词尾（内容/文本/信息）与「斜杠两侧自包含」两条",
          "HINT_TAIL" in SCAN_JS and "内容|文本|信息|输入|搜索|查询" in SCAN_JS
          and "isBadLabelName" in SCAN_JS and "isCtlValueEcho" in SCAN_JS
          and "return { fields: out, polluted: labelPolluted };" in SCAN_JS)

    class _PF(object):
        """面板用假 frame：只实现 evaluate。"""
        def __init__(self, ret=None, boom=False):
            self.ret, self.boom, self.calls = ret, boom, []

        def evaluate(self, js, arg=None):
            self.calls.append((js, arg))
            if self.boom:
                raise RuntimeError("frame detached")
            return self.ret

    # ---- _panel_items：新旧契约都要能吃，且诊断必须记下来 ----
    _its = _panel_items(_PF({"items": [{"k": 0, "text": "江苏省"}],
                             "gate": True, "excluded": 7}))
    check("㊾ 新契约（dict）解析出候选项 + 闸门诊断被记下",
          _its == [{"k": 0, "text": "江苏省"}]
          and LAST_PANEL_GATE == {"闸门生效": True, "剔除数": 7, "候选数": 1},
          (_its, LAST_PANEL_GATE))

    # ---- 反证：旧契约（纯数组）不能炸；采集抛错必须返回空表而非整轮崩 ----
    _its = _panel_items(_PF([{"k": 0, "text": "a"}]))
    check("㊿ 反证：旧契约不炸且闸门记为未生效；采集抛错 → 空表",
          _its == [{"k": 0, "text": "a"}] and LAST_PANEL_GATE["闸门生效"] is False
          and _panel_items(_PF(boom=True)) == [],
          (_its, LAST_PANEL_GATE))

    print("\n自测结果：%d 通过 / %d 失败" % (ok[0], bad[0]))
    return 0 if bad[0] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
