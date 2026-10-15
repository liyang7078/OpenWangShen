#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sheet_html.py · 可复制清单页
=============================
把「填写清单」变成一张**点一下就复制**的本机网页。

它解决什么
----------
有些网申页面会拦脚本、拦粘贴。天填不进去的时候，人只能一项项手抄 ——
而手抄最容易出错：抄错一个数字，退回重来。

本脚本把清单渲染成一个本机 HTML：
  · 每一项右边一个「复制」按钮 —— 点一下，粘到网页对应框里
  · 底部一个「复制全文」按钮 —— 一次拿全部
  · 大字号、高对比，长时间对着屏幕也不费眼

数据从哪来
----------
两种都支持：
  · `--in 填写清单.md`                转已有的 markdown 清单
  · `--profile profile.json --labels-file 页面字段.txt`   一步到位

隐私（硬约束）
--------------
产出的 HTML **含你的真实信息**，所以：
  · 默认文件名与清单同源，`.gitignore` 已屏蔽（勿手动改名成别的）
  · 页面顶部固定一条「请勿外发」提示
  · 不做 `guard()` 反证 —— 它本来就该留在本机，那道闸是拦「该出门的东西」

用法
----
  python scripts/sheet_html.py --in 填写清单.md
  python scripts/sheet_html.py --profile profile.json --labels-file 页面字段.txt
  python scripts/sheet_html.py --profile profile.json --labels 姓名,手机号 --out 页.html
  python scripts/sheet_html.py --selftest

许可：MIT
"""

import argparse
import datetime
import html
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

ROW_RE = re.compile(r"^\|(.+)\|\s*$")
HEADER_SEP = re.compile(r"^\|[\s:\-|]+\|\s*$")


# --------------------------------------------------------------------------
# 解析 markdown 清单
# --------------------------------------------------------------------------
def parse_md(text):
    """从清单 markdown 里取出「就绪」与「待补」两组。返回 (ready, miss, meta)。"""
    ready, miss, meta = [], [], {}
    section = ""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("## "):
            if "粘" in s or "就绪" in s:
                section = "ready"
            elif "补" in s:
                section = "miss"
            else:
                section = ""
            continue
        if s.startswith("| 生成时间 |") or s.startswith("| 企业 |") or \
                s.startswith("| 岗位 |") or s.startswith("| 投递链接 |"):
            cells = _cells(s)
            if len(cells) >= 2:
                meta[cells[0]] = cells[1]
            continue
        if not s.startswith("|") or HEADER_SEP.match(s):
            continue
        cells = _cells(s)
        if not cells or cells[0] in ("#",):
            continue
        if section == "ready" and len(cells) >= 3:
            ready.append({"页面字段": cells[1], "值": cells[2],
                          "必填": len(cells) > 3 and "✔" in cells[3]})
        elif section == "miss" and len(cells) >= 2:
            # 「对应档案键」在清单 markdown 里是**代码跨度**（`键`），
            # 那层反引号只对 markdown 渲染有意义。这里是 HTML 消费端，
            # 必须剥掉 —— 否则页面上会直接显示反引号，且键值也带脏字符。
            key = cells[3] if len(cells) > 3 else ""
            key = key.strip()
            if len(key) >= 2 and key.startswith("`") and key.endswith("`"):
                key = key[1:-1].strip()
            miss.append({"页面字段": cells[1],
                         "必填": "✔" in (cells[2] if len(cells) > 2 else ""),
                         "键": key})
    return ready, miss, meta


def _cells(row):
    inner = row.strip().strip("|")
    return [c.strip().replace("\\|", "|") for c in inner.split("|")]


# --------------------------------------------------------------------------
# 一步到位：从档案现算清单
# --------------------------------------------------------------------------
def md_from_profile(profile_path, labels, rules=None, meta=None):
    import make_sheet                                    # type: ignore
    fields = core.load_rules(rules)
    index = core.build_index(fields)
    profile = core.load_profile(profile_path)
    rows = make_sheet.build_rows(labels, fields, profile, index)
    ctx = {"now": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}
    ctx.update(meta or {})
    md, _ready, _miss = make_sheet.render(rows, ctx)
    return md


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------
CSS = """
:root { --ink:#111; --dim:#4a4a4a; --line:#c9c9c9; --bg:#fff; --soft:#f4f4f4;
        --missbg:#fbf7e8; }
* { box-sizing: border-box; }
body { margin:0; padding:28px 20px 80px; background:var(--bg); color:var(--ink);
       font: 20px/1.65 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; }
h1 { font-size:30px; margin:0 0 6px; letter-spacing:.5px; }
.meta { color:var(--dim); font-size:17px; margin-bottom:4px; }
.warn { background:#fff4d6; border:2px solid #e0a800; border-left:12px solid #e0a800;
        border-radius:8px; padding:12px 16px; margin:16px 0 26px; font-size:18px;
        font-weight:600; color:var(--ink); }
h2 { font-size:23px; margin:34px 0 12px; padding-bottom:8px;
     border-bottom:3px solid var(--ink); }
.row { display:flex; align-items:flex-start; gap:14px; padding:11px 12px;
       border-bottom:1px solid var(--line); }
.row:hover { background:var(--soft); }
.k { flex:0 0 190px; font-weight:700; word-break:break-all; }
.v { flex:1 1 auto; white-space:pre-wrap; word-break:break-all; }
button { flex:0 0 auto; font:inherit; font-size:17px; padding:8px 20px; cursor:pointer;
         border:2px solid var(--ink); border-radius:8px; background:#fff; color:var(--ink);
         font-weight:700; }
button:hover { background:var(--ink); color:#fff; }
button.done { background:#111; color:#fff; }
.bar { position:fixed; left:0; right:0; bottom:0; padding:14px 20px;
       background:var(--ink); display:flex; gap:16px; align-items:center; }
.bar button { background:#fff; border-color:#fff; }
.bar .tip { color:#fff; font-size:17px; }
.empty { color:var(--dim); padding:12px; }
/* 待补项：**不靠颜色变浅**表达状态（色弱 / 黑白打印也看得出来），
   改用「文字标记 + 左侧色条」双重编码。 */
.badge { display:inline-block; font-size:15px; font-weight:700; padding:2px 10px;
         border:2px solid var(--ink); border-radius:6px; margin-right:10px;
         background:#fff; }
.miss .row { background:var(--missbg); border-left:10px solid #e0a800; }
.miss .k { flex:0 0 230px; }
.miss .v { color:var(--ink); }
"""

JS = """
function fallbackCopy(t){
  var ta=document.createElement('textarea'); ta.value=t;
  ta.style.position='fixed'; ta.style.top='-1000px'; ta.style.opacity='0';
  document.body.appendChild(ta); ta.focus(); ta.select();
  var ok=false; try{ ok=document.execCommand('copy'); }catch(e){ ok=false; }
  document.body.removeChild(ta); return ok;
}
function copyText(t){
  if(navigator.clipboard && window.isSecureContext){
    return navigator.clipboard.writeText(t).then(function(){return true;},
                                                 function(){return fallbackCopy(t);});
  }
  return Promise.resolve(fallbackCopy(t));
}
function flash(btn, ok){
  var old=btn.textContent; btn.textContent = ok ? '已复制' : '请手动选中';
  btn.classList.add('done');
  setTimeout(function(){ btn.textContent=old; btn.classList.remove('done'); }, 1100);
}
function cp(btn){
  var row=btn.closest('.row');
  var v=row.querySelector('.v').textContent;
  copyText(v).then(function(ok){ flash(btn, ok); });
}
function cpAll(btn){
  var rows=document.querySelectorAll('#ready .row');
  var out=[];
  for(var i=0;i<rows.length;i++){
    var k=rows[i].querySelector('.k').textContent.replace('★','').trim();
    var v=rows[i].querySelector('.v').textContent;
    out.push(k+'：'+v);
  }
  copyText(out.join('\\n')).then(function(ok){ flash(btn, ok); });
}
"""


def render_html(ready, miss, meta, source=""):
    def esc(s):
        return html.escape(str(s or ""), quote=True)

    L = ["<!DOCTYPE html>", '<html lang="zh-CN">', "<head>", '<meta charset="utf-8">',
         '<meta name="viewport" content="width=device-width, initial-scale=1">',
         "<title>网申填写清单 · 可复制页</title>",
         "<style>%s</style>" % CSS, "</head>", "<body>",
         "<h1>网申填写清单</h1>"]
    for k, v in meta.items():
        L.append('<div class="meta">%s：%s</div>' % (esc(k), esc(v)))
    # data-notice 是给自测用的**机器可读钩子**：提示的"存在性"由钩子保证，
    # 具体措辞可以随便改而不惊动自测（改文案不该让测试变红，
    # 但删掉这条提示必须变红）。
    L.append('<div class="warn" data-notice="no-share">重要提示：本页含你的个人信息，'
             '请勿外发、勿截图分享、勿上传网盘。</div>')

    L.append('<h2>一、照着往网页里粘（%d 项就绪）</h2>' % len(ready))
    L.append('<div id="ready">')
    if ready:
        for r in ready:
            star = '<span class="req" title="必填">★</span>' if r.get("必填") else ""
            L.append('<div class="row"><div class="k">%s%s</div>'
                     '<div class="v">%s</div>'
                     '<button type="button" onclick="cp(this)">复制</button></div>'
                     % (esc(r["页面字段"]), star, esc(r["值"])))
    else:
        L.append('<div class="empty">（无）</div>')
    L.append("</div>")

    L.append('<h2>二、档案里没有、需要你补的（%d 项）</h2>' % len(miss))
    L.append('<div id="miss" class="miss">')
    if miss:
        for r in miss:
            star = ' <span class="req" title="必填">★</span>' if r.get("必填") else ""
            # 「待补」用**文字标记**，不靠颜色变浅 —— 色弱、灰度打印同样可辨。
            # data-badge / data-key：前者让自测断言"标记确实存在且带文字"，
            # 后者让自测断言"这一行的档案键就是它"，两者都不依赖文案。
            L.append('<div class="row" data-key="%s"><div class="k">%s%s</div>'
                     '<div class="v"><span class="badge" data-badge="todo">待补</span>'
                     '对应档案键：%s</div></div>'
                     % (esc(r["键"]), esc(r["页面字段"]), star, esc(r["键"])))
    else:
        L.append('<div class="empty">（无）</div>')
    L.append("</div>")

    L.append('<div class="bar"><button type="button" id="all" onclick="cpAll(this)">'
             '复制全文</button>'
             '<span class="tip" data-legend="todo">★ = 必填 ｜ 带「待补」标记的要你手填 '
             '｜ 点单项「复制」再粘到网页对应框里</span></div>')
    L.append("<script>%s</script>" % JS)
    L.append("</body></html>")
    return "\n".join(L)


# --------------------------------------------------------------------------
# 迷你 DOM —— 只给自测用
# --------------------------------------------------------------------------
# 为什么不用字符串 in 断言：`"请勿外发" in html` 这种写法盯的是**文案**。
# 改一句提示的措辞就会变红（假红），而把提示整条删掉、只要别处还残留这四个字
# 就会变绿（假绿）。所以这里把产物真的解析成树，断言**结构**与**语义**：
#   · 元素在不在、属性对不对、行数够不够
#   · 文本往返相等（转义是否真的生效）
#   · 语义由 data-* 钩子承载，措辞与断言解耦
#
# 解析器本身与单元测试共用，见 scripts/htmlprobe.py（单一来源，不复制两份）。
from htmlprobe import css_decls as _css_decls, parse as _parse_html  # noqa: E402


# 「语义不许靠表情」：⚠️✅❌❗❓⭐ 这类**状态表情**一律不许出现。
# 注意 ★(U+2605) 不在禁用之列 —— 它是排版符号，且配了 title="必填"
# 与底部文字图例，不属于"只靠图形表意"。
_BANNED_MARKS = "\u26a0\ufe0f\u2705\u274c\u2757\u2753\u2b50"


def _has_pictograph(s):
    """是否含绘文字（含 ⚠️ 这类显式表情变体选择符）。"""
    if any(ch in s for ch in _BANNED_MARKS):
        return True
    return any(0x1F000 <= ord(ch) <= 0x1FAFF for ch in s)


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

    md = "\n".join([
        "# 网申填写清单", "",
        "| 项 | 内容 |", "|---|---|",
        "| 生成时间 | 2026-10-15 22:00 |",
        "| 企业 | 示例公司 |", "",
        "## 一、照着往网页里粘（2 项就绪）", "",
        "| # | 页面字段 | 填写内容 | 必填 |", "|---|---|---|---|",
        "| 1 | 姓名 | 示例考生 | ✔ |",
        "| 2 | 手机号 | 13800000000 | ✔ |", "",
        "## 二、档案里没有、需要你补的（1 项）", "",
        "| # | 页面字段 | 必填 | 对应档案键 |", "|---|---|---|---|",
        "| 1 | 通讯地址 | ✔ | `通讯地址` |", "",
    ])
    ready, miss, meta = parse_md(md)
    check("① 解析出就绪 2 项", len(ready) == 2, len(ready))
    check("② 解析出待补 1 项", len(miss) == 1, len(miss))
    check("③ 解析出元信息", meta.get("企业") == "示例公司", meta)
    check("④ 必填标记识别", ready[0]["必填"] is True)
    check("⑤ 值原样取出", ready[1]["值"] == "13800000000")

    # ---------------- 结构化断言（解析成树，不靠文案） ----------------
    h = render_html(ready, miss, meta)
    dom = _parse_html(h)

    ready_box = dom.find_all(id="ready")
    miss_box = dom.find_all(id="miss")
    check("⑥ 就绪区/待补区都在且各 1 个",
          len(ready_box) == 1 and len(miss_box) == 1,
          (len(ready_box), len(miss_box)))

    r_rows = ready_box[0].find_all("div", **{"class": "row"}) if ready_box else []
    m_rows = miss_box[0].find_all("div", **{"class": "row"}) if miss_box else []
    check("⑦ 就绪区 2 行、待补区 1 行（行数=数据条数）",
          len(r_rows) == 2 and len(m_rows) == 1, (len(r_rows), len(m_rows)))

    # 复制按钮：数量 + **接线**（onclick 真的调到 cp(this)），与按钮文案无关
    btns = [b for b in ready_box[0].find_all("button")] if ready_box else []
    check("⑧ 每个就绪行一个复制按钮，且接线到 cp(this)",
          len(btns) == 2 and all("cp(this)" in (b.attrs.get("onclick") or "")
                                 for b in btns),
          [(b.attrs.get("onclick")) for b in btns])

    # 复制全文：按钮存在 + 接线 + 脚本里**真的定义了** cpAll
    allbtn = dom.find_all("button", id="all")
    scripts = "".join(s.text() for s in dom.find_all("script"))
    check("⑨ 复制全文按钮接线到 cpAll，且脚本里定义了 cpAll",
          len(allbtn) == 1
          and "cpAll(this)" in (allbtn[0].attrs.get("onclick") or "")
          and "function cpAll(" in scripts,
          allbtn[0].attrs.get("onclick") if allbtn else None)

    # 勿外发提示：靠 data-notice 钩子认定"存在"，且提示不能是空的
    notice = dom.find_all(**{"data-notice": "no-share"})
    check("⑩ 勿外发提示存在（data-notice 钩子）且非空",
          len(notice) == 1 and len(notice[0].text().strip()) > 0,
          len(notice))

    # 待补行必须**带着档案键**（键是数据契约，不是文案）
    keys = [r.attrs.get("data-key") for r in m_rows]
    check("⑪ 待补行带出档案键 data-key",
          keys == ["通讯地址"], keys)

    check("⑫ 文档骨架完整（DOCTYPE + html/head/body）",
          h.lstrip().startswith("<!DOCTYPE html>")
          and all(len(dom.find_all(t)) == 1 for t in ("html", "head", "body")))

    # ---------- 转义：断言**往返相等**，而不是断言某个实体串 ----------
    raw = '<b>&x</b>'
    h2 = render_html([{"页面字段": "备注", "值": raw, "必填": False}], [], {})
    dom2 = _parse_html(h2)
    v2 = [e for e in dom2.find_all(**{"class": "v"})]
    check("⑬ 值含 <>& 时往返相等（转义真的生效）",
          len(v2) == 1 and v2[0].text() == raw, v2[0].text() if v2 else None)
    check("⑭ 值里的 <b> 没有被当成标签（未破版）",
          len(dom2.find_all("b")) == 0)

    # ---------- 属性也要转义（曾经只转文本、不转属性的写法会在这里红） ----------
    evil = '" onmouseover="alert(1)'
    h3 = render_html([], [], {"企业": evil})
    dom3 = _parse_html(h3)
    metas = dom3.find_all(**{"class": "meta"})
    check("⑮ 元信息里的引号不会溢出成新属性（属性级转义）",
          len(metas) == 1
          and dom3.find_attr("onmouseover") == []
          and evil in metas[0].text(),
          [m.attrs for m in metas])

    # ---------- 空输入不报错 ----------
    h4 = render_html([], [], {})
    dom4 = _parse_html(h4)
    box4 = dom4.find_all(id="ready")
    check("⑯ 无就绪项时仍产出完整页面且无数据行",
          len(dom4.find_all("body")) == 1 and box4
          and len(box4[0].find_all("div", **{"class": "row"})) == 0)

    # ---------- 无障碍（P1-13）：状态不靠颜色/表情 ----------
    badge = dom.find_all(**{"data-badge": "todo"})
    check("⑰ 待补有**文字**标记（data-badge=todo 且带文字）",
          len(badge) == 1 and badge[0].text().strip() != "",
          len(badge))

    legend = dom.find_all(**{"data-legend": "todo"})
    check("⑱ 底部图例存在（data-legend 钩子）",
          len(legend) == 1 and len(legend[0].text().strip()) > 0)

    check("⑲ 全文不靠表情表意（无 ⚠️/✅/❌ 等，也无绘文字）",
          not _has_pictograph(h),
          [c for c in h if _has_pictograph(c)][:8])

    # 待补值的前景色：**解析 CSS 声明**，断言不是弱化灰
    v_decl = _css_decls(h, ".miss .v")
    check("⑳ 待补值前景色为正文色，不用弱化灰",
          v_decl.get("color") == "var(--ink)", v_decl)
    check("㉑ 待补项另有左侧色条做第二重编码（不只靠标记）",
          _css_decls(h, ".miss .row").get("border-left", "") != "")

    # 回归：档案键那一列的反引号必须被剥掉（曾经漏剥 → 页面直接显示 `键`，
    # 而且 data-key 属性也被污染）。两种写法都要能取对。
    _r2, _m2, _ = parse_md("\n".join([
        "## 二、档案里没有、需要你补的（2 项）", "",
        "| # | 页面字段 | 必填 | 对应档案键 |", "|---|---|---|---|",
        "| 1 | 甲字段 | ✔ | `键甲` |",
        "| 2 | 乙字段 |  | 键乙 |",
    ]))
    check("㉒ 档案键剥反引号（带与不带反引号都对）",
          [x["键"] for x in _m2] == ["键甲", "键乙"],
          [x["键"] for x in _m2])

    print("\n自测结果：%d 通过 / %d 失败" % (ok[0], bad[0]))
    return 0 if bad[0] == 0 else 1


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="可复制清单页：把填写清单渲染成点一下就复制的本机网页",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python scripts/sheet_html.py --in 填写清单.md\n"
               "  python scripts/sheet_html.py --profile profile.json --labels-file 页面字段.txt\n")
    ap.add_argument("--in", dest="infile", default="", help="输入：填写清单 .md")
    ap.add_argument("--profile", default="", help="改用档案现算清单")
    ap.add_argument("--labels", default="", help="页面字段标签（英文逗号分隔）")
    ap.add_argument("--labels-file", dest="labels_file", default="", help="页面字段标签文件")
    ap.add_argument("--rules", default=None, help="字段规则库路径")
    ap.add_argument("--out", default="", help="输出 HTML 路径")
    ap.add_argument("--overwrite", action="store_true",
                    help="允许覆盖同名 HTML（默认改存带时间戳的新名）")
    ap.add_argument("--selftest", action="store_true", help="自测")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()

    md, source = "", ""
    if args.infile:
        if not os.path.isfile(args.infile):
            print("[ERR] 找不到清单文件：%s" % args.infile)
            return 2
        with open(args.infile, "r", encoding="utf-8") as fh:
            md = fh.read()
        source = args.infile
    elif args.profile:
        labels = []
        if args.labels_file:
            if not os.path.isfile(args.labels_file):
                print("[ERR] 找不到字段标签文件：%s" % args.labels_file)
                return 2
            with open(args.labels_file, "r", encoding="utf-8") as fh:
                labels = [l.strip() for l in fh if l.strip()]
        if args.labels:
            labels += [x.strip() for x in args.labels.replace("，", ",").split(",") if x.strip()]
        if not labels:
            print("[ERR] 用 --profile 时还要给 --labels 或 --labels-file（页面上的字段名）")
            return 2
        try:
            md = md_from_profile(args.profile, labels, args.rules)
        except Exception as exc:                              # noqa: BLE001
            print("[ERR] 清单算不出来：%s" % exc)
            return 2
        source = args.profile
    else:
        ap.print_help()
        return 2

    ready, miss, meta = parse_md(md)
    if not ready and not miss:
        print("[ERR] 这份清单里没解析到表格 —— 确认它是 make_sheet.py 的产物。")
        return 2

    out = args.out
    explicit = bool(args.out)
    if not out:
        stem = os.path.splitext(os.path.basename(source))[0]
        out = os.path.join(os.path.dirname(os.path.abspath(source)) or ROOT, stem + ".html")

    # 统一 safe_write：脚本自己拼的名字默认**不覆盖**（改存带时间戳）；
    # `--out` 是用户显式点名的目标 ⇒ 加 --overwrite 才覆盖。
    wrote = core.safe_write(out, render_html(ready, miss, meta, source),
                            overwrite=bool(explicit and args.overwrite),
                            root=ROOT if not explicit else None)

    print("就绪 %d 项 ｜ 待补 %d 项" % (len(ready), len(miss)))
    print("已生成：%s" % wrote)
    if wrote != os.path.abspath(out):
        print("（同名文件已存在，本次改存新名；要覆盖请加 --overwrite）")
    print("用浏览器打开它，点单项「复制」再粘到网申页面。")
    print("⚠️ 这个文件含你的真实信息，别改名、别外发、别提交到 Git。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
