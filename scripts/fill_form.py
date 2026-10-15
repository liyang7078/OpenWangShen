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

# 视为「人工门」的字段类型：程序永不代填
HUMAN_GATE_TYPES = {"password", "file"}
CAPTCHA_HINTS = ("验证码", "captcha", "verify code", "短信验证", "动态码")


# --------------------------------------------------------------------------
# 页面扫描：提取表单字段
# --------------------------------------------------------------------------
SCAN_JS = r"""
() => {
  const SKIP = new Set(['hidden', 'submit', 'button', 'image', 'reset']);

  function labelOf(el) {
    // 1) 原生 labels
    if (el.labels && el.labels.length) {
      const t = (el.labels[0].innerText || '').trim();
      if (t) return t;
    }
    // 2) label[for=id]
    if (el.id) {
      const l = document.querySelector('label[for="' + el.id + '"]');
      if (l && l.innerText) return l.innerText.trim();
    }
    // 3) aria-label / placeholder
    const aria = el.getAttribute('aria-label');
    if (aria) return aria.trim();
    const ph = el.getAttribute('placeholder');
    if (ph) return ph.trim();
    // 4) 向上找容器里第一段短文本
    let node = el.parentElement, depth = 0;
    while (node && depth < 3) {
      const txt = (node.innerText || '').trim();
      if (txt && txt.length <= 40) return txt;
      node = node.parentElement; depth++;
    }
    return '';
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
        if (t && t.length <= 40) return t;
      }
      // 再退一步：同层里有没有 aria-label 的分组容器
      const al = node.getAttribute && node.getAttribute('aria-label');
      if (al) return al.trim();
      node = node.parentElement; depth++;
    }
    return '';
  }

  const out = [];
  let i = 0;
  document.querySelectorAll('input,select,textarea').forEach(el => {
    const t = (el.type || el.tagName).toLowerCase();
    if (SKIP.has(t)) return;
    const idx = i++;
    el.setAttribute('data-ows-idx', String(idx));

    let label = '';
    if (t === 'radio' || t === 'checkbox') {
      label = groupLabelOf(el);          // 先用「组名」
    }
    if (!label) label = labelOf(el);     // 退化到通用提取

    let options = null;
    if (t === 'select' || el.tagName.toLowerCase() === 'select') {
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
      visible: !!(el.offsetParent !== null)
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
    返回 (plan, human_gates, unmatched)
      plan = [{idx, label, key, type, value, how}]
    """
    plan, gates, unmatched = [], [], []

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

        key, how = core.map_one(lab, index, rules)
        if not key:
            # 再试 placeholder / name
            for alt in (f.get("placeholder"), f.get("name"), f.get("id")):
                if not alt:
                    continue
                key, how = core.map_one(alt, index, rules)
                if key:
                    how = "partial"
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

        plan.append({"idx": f["idx"], "label": lab, "key": key,
                     "type": f["type"], "value": val, "how": how,
                     "状态": "待填"})
    return plan, gates, unmatched


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
                page.fill(sel, v)
                ok = True
            if ok:
                filled += 1
            else:
                failed.append({"label": item["label"], "原因": "选项未匹配"})
        except Exception as e:
            failed.append({"label": item["label"], "原因": str(e).split("\n")[0][:80]})
    return {"已填": filled, "失败": failed, "跳过": skipped,
            "日期补全": adjusted, "超时中止": timed_out}


def _fill_choice(page, sel, item, field):
    """单选/复选：按选项文本匹配。"""
    val = str(item["value"]).strip()
    # 该字段同名的所有选项
    name = field.get("name") or ""
    if name:
        opts = page.query_selector_all('input[name="%s"]' % name)
    else:
        opts = [page.query_selector(sel)]
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
            lab = o.get_attribute("value") or ""
        if lab and (val in lab or lab in val):
            o.check()
            return True
    return False


def _fill_select(page, item, field):
    """下拉框：按选项文本匹配，退化按 value 匹配。"""
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
    # 模糊：包含关系
    for o in (field.get("options") or []):
        if val and (val in o or o in val):
            try:
                page.select_option(sel, label=o)
                return True
            except Exception:
                continue
    return False


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
        browser = pw.chromium.launch(**launch_kw)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        page.set_default_timeout(args.timeout)

        print("[1/5] 打开页面：%s" % args.url)
        page.goto(args.url, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)

        print("[2/5] 扫描表单字段")
        fields = scan_page(page)
        report["页面标题"] = page.title()
        report["字段总数"] = len(fields)
        print("      发现 %d 个输入控件" % len(fields))

        fields_by_idx = {f["idx"]: f for f in fields}
        labels = [f["label"] for f in fields if f.get("label")]
        plan, gates, unmatched = plan_mapping(fields, labels, profile, index, rules)

        report["可自动填"] = len([p for p in plan if p["状态"] == "待填"])
        report["档案缺项"] = [p["label"] for p in plan if p["状态"] == "档案缺项"]
        report["人工门"] = gates
        report["规则库未命中"] = [f.get("label") or f.get("name") for f in unmatched]

        print("[3/5] 映射结果：可填 %d ／ 档案缺项 %d ／ 人工门 %d ／ 未命中 %d"
              % (report["可自动填"], len(report["档案缺项"]),
                 len(gates), len(report["规则库未命中"])))

        # ---- 提前止损：超预算 或 识别率过低 → 停手，转可复制清单页 ----
        rate = (report["可自动填"] / len(fields)) if fields else 0.0
        report["识别率"] = round(rate, 3)
        stop_reason = ""
        if deadline and time.time() > deadline:
            stop_reason = "超预算（%d 秒）" % args.budget
        elif len(fields) >= 5 and rate < 0.2:
            stop_reason = "识别率过低（%.0f%%，低于 20%%）" % (rate * 100)
        if stop_reason:
            report["停止原因"] = stop_reason
            print("      ⚠️ %s —— 停手，不再往下填" % stop_reason)

        if args.dry_run:
            print("[4/5] --dry-run：不写入任何内容")
            res = {"已填": 0, "失败": [], "跳过": [], "日期补全": [],
                   "超时中止": False}
        elif stop_reason:
            print("[4/5] 已停手，不写入")
            res = {"已填": 0, "失败": [], "跳过": [], "日期补全": [],
                   "超时中止": True, "原因": stop_reason}
        else:
            print("[4/5] 写入表单（不提交）")
            res = apply_plan(page, plan, fields_by_idx, deadline=deadline)
            print("      已填 %d ／ 失败 %d ／ 跳过 %d"
                  % (res["已填"], len(res["失败"]), len(res["跳过"])))
            for a in res.get("日期补全", []):
                print("      ⚠️ %s：%s" % (a["label"], a["说明"]))
            if res.get("超时中止"):
                report["停止原因"] = "填写途中到点（%d 秒）" % args.budget
                print("      ⚠️ %s —— 已停手" % report["停止原因"])

        report["填写结果"] = res

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

        # —— 收尾。先上保险丝再关浏览器：退出码只能由填写结果决定。
        release = threading.Event()
        arm_close_guard(release, rc=0)
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
    ap.add_argument("--profile", required=True, help="本机档案 JSON")
    ap.add_argument("--url", required=True, help="岗位页 URL")
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
    ap.add_argument("--html", default="",
                    help="提前停手时，直接生成可复制清单页到该路径")
    args = ap.parse_args()
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
