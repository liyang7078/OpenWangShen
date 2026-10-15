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
            miss.append({"页面字段": cells[1],
                         "必填": "✔" in (cells[2] if len(cells) > 2 else ""),
                         "键": cells[3] if len(cells) > 3 else ""})
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
:root { --ink:#111; --dim:#555; --line:#c9c9c9; --bg:#fff; --soft:#f4f4f4; }
* { box-sizing: border-box; }
body { margin:0; padding:28px 20px 80px; background:var(--bg); color:var(--ink);
       font: 20px/1.65 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; }
h1 { font-size:30px; margin:0 0 6px; letter-spacing:.5px; }
.meta { color:var(--dim); font-size:17px; margin-bottom:4px; }
.warn { background:#fff4d6; border:2px solid #e0a800; border-radius:8px;
        padding:12px 16px; margin:16px 0 26px; font-size:18px; font-weight:600; }
h2 { font-size:23px; margin:34px 0 12px; padding-bottom:8px;
     border-bottom:3px solid var(--ink); }
.row { display:flex; align-items:flex-start; gap:14px; padding:11px 12px;
       border-bottom:1px solid var(--line); }
.row:hover { background:var(--soft); }
.k { flex:0 0 190px; font-weight:700; word-break:break-all; }
.k .req { color:var(--ink); }
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
.miss .k { flex:0 0 230px; }
.miss .v { color:var(--dim); }
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
    L.append('<div class="warn">⚠️ 本页含你的个人信息，请勿外发、勿截图分享、勿上传网盘。</div>')

    L.append('<h2>一、照着往网页里粘（%d 项就绪）</h2>' % len(ready))
    L.append('<div id="ready">')
    if ready:
        for r in ready:
            star = '<span class="req">★</span>' if r.get("必填") else ""
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
            star = "★" if r.get("必填") else ""
            L.append('<div class="row"><div class="k">%s%s</div>'
                     '<div class="v">（待补）对应档案键：%s</div></div>'
                     % (esc(r["页面字段"]), star, esc(r["键"])))
    else:
        L.append('<div class="empty">（无）</div>')
    L.append("</div>")

    L.append('<div class="bar"><button type="button" id="all" onclick="cpAll(this)">'
             '复制全文</button>'
             '<span class="tip">★ = 必填 ｜ 点单项「复制」再粘到网页对应框里</span></div>')
    L.append("<script>%s</script>" % JS)
    L.append("</body></html>")
    return "\n".join(L)


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

    h = render_html(ready, miss, meta)
    check("⑥ HTML 有复制按钮", h.count("onclick=\"cp(this)\"") == 2)
    check("⑦ HTML 有复制全文", "cpAll(this)" in h and "复制全文" in h)
    check("⑧ HTML 含勿外发提示", "请勿外发" in h)
    check("⑨ 待补项带档案键", "对应档案键" in h and "通讯地址" in h)
    check("⑩ 结构完整", h.startswith("<!DOCTYPE html>") and h.rstrip().endswith("</html>"))

    # 转义：值里带 < > & 不得破版
    h2 = render_html([{"页面字段": "备注", "值": '<b>&x</b>', "必填": False}], [], {})
    check("⑪ HTML 转义（<> 不破版）", "&lt;b&gt;&amp;x&lt;/b&gt;" in h2)
    check("⑫ 无就绪项也不报错", "（无）" in render_html([], [], {}))

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
    if not out:
        stem = os.path.splitext(os.path.basename(source))[0]
        out = os.path.join(os.path.dirname(os.path.abspath(source)) or ROOT, stem + ".html")

    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="") as fh:
        fh.write(render_html(ready, miss, meta, source))

    print("就绪 %d 项 ｜ 待补 %d 项" % (len(ready), len(miss)))
    print("已生成：%s" % out)
    print("用浏览器打开它，点单项「复制」再粘到网申页面。")
    print("⚠️ 这个文件含你的真实信息，别改名、别外发、别提交到 Git。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
