#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_sheet.py · 网申填写清单生成器
===================================
把「本机档案 + 页面字段」映射成一份**人类照着抄的清单**。

这是「自动填表」不可用时的保底路径：
    档案.json + 页面字段  ->  (本地脚本)  ->  填写清单.md  ->  你自己往网页里粘

隐私红线
--------
真实值**只写进清单文件**，终端一个值都不打印。
（打印出来就会进入 AI 助手的上下文，那就破线了。）

用法
----
  # 已知页面字段（推荐：从真实页面抓到标签后传入）
  python scripts/make_sheet.py --profile fixtures/档案_示例.json \
      --labels-file fixtures/页面字段_示例.txt \
      --company "某集团" --job "财务会计岗" --out 填写清单.md

  # 未知页面字段（列出全量字段，必填优先，供你对着空表单找）
  python scripts/make_sheet.py --profile profile.json --out 填写清单.md

  # 自测（用内置虚构数据）
  python scripts/make_sheet.py --selftest

许可：MIT
"""

import argparse
import contextlib
import datetime
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core  # noqa: E402


def build_rows(labels, fields, profile, index=None):
    """
    生成待填行。返回 (rows, unmatched)
      rows = [{"页面字段", "键", "值", "必填", "类型", "置信度", "状态"}]
    · 给了 labels  -> 按页面顺序，只列页面上真实存在的字段
    · 没给 labels  -> 按规则库全量列出，必填优先
    附件类字段不进入本表（单独归入附件节）。
    """
    if labels:
        matched, unmatched = core.map_fields(labels, fields, index)
        rows = []
        for lb in labels:
            if lb not in matched:
                continue
            key, how = matched[lb]
            if core.is_attachment(key):
                continue
            rows.append(_mk(lb, key, fields, profile, how))
        return rows, unmatched, None

    # 无页面标签：全量列出（必填优先，再按分组）
    order = {"基本信息": 0, "联系方式": 1, "教育背景": 2,
             "能力证书": 3, "求职意向": 4, "经历与自述": 5}
    items = [f for f in fields if not core.is_attachment(f["键"])]
    items.sort(key=lambda i: (0 if i.get("必填") else 1,
                              order.get(i.get("分组", ""), 9)))
    rows = [_mk((i.get("标签") or [i["键"]])[0], i["键"], fields, profile, "by-rule")
            for i in items]
    return rows, [], None


def _mk(label, key, fields, profile, how):
    meta = core.field_meta(fields, key)
    val = core.value_of(profile, key)
    ok = val not in ("", None) and not isinstance(val, (list, dict))
    return {
        "页面字段": label,
        "键": key,
        "值": val if ok else "",
        "必填": bool(meta.get("必填")),
        "类型": meta.get("类型", "text"),
        "置信度": how,
        "状态": "就绪" if ok else "档案缺项",
    }


def render(rows, ctx):
    """渲染 Markdown 清单。"""
    L = []
    L.append("# 网申填写清单")
    L.append("")
    L.append("> ⚠️ **本文件含个人信息，请勿外发、勿上传网盘、勿提交到 Git。**")
    L.append("> 投递完成后建议自行删除。")
    L.append("")
    L.append("| 项 | 内容 |")
    L.append("|---|---|")
    L.append("| 生成时间 | %s |" % ctx["now"])
    if ctx.get("company"):
        L.append("| 企业 | %s |" % ctx["company"])
    if ctx.get("job"):
        L.append("| 岗位 | %s |" % ctx["job"])
    if ctx.get("url"):
        L.append("| 投递链接 | %s |" % ctx["url"])
    L.append("")

    ready = [r for r in rows if r["状态"] == "就绪"]
    miss = [r for r in rows if r["状态"] == "档案缺项"]

    L.append("## 一、照着往网页里粘（%d 项就绪）" % len(ready))
    L.append("")
    if ready:
        L.append("| # | 页面字段 | 填写内容 | 必填 |")
        L.append("|---|---|---|---|")
        for i, r in enumerate(ready, 1):
            v = str(r["值"]).replace("|", "\\|").replace("\n", "<br>")
            L.append("| %d | %s | %s | %s |" % (i, r["页面字段"], v,
                                                "✔" if r["必填"] else ""))
    else:
        L.append("（无）")
    L.append("")

    L.append("## 二、档案里没有、需要你补的（%d 项）" % len(miss))
    L.append("")
    if miss:
        L.append("> 这些不是「系统填不了」，而是**你的档案里确实没有**。补进档案即可长期复用。")
        L.append("")
        L.append("| # | 页面字段 | 必填 | 对应档案键 |")
        L.append("|---|---|---|---|")
        for i, r in enumerate(miss, 1):
            L.append("| %d | %s | %s | `%s` |" % (i, r["页面字段"],
                                                  "✔" if r["必填"] else "", r["键"]))
    else:
        L.append("（无）")
    L.append("")
    return "\n".join(L), ready, miss


def render_attach(attach_rows, ctx):
    L = []
    L.append("## 三、需要上传的附件")
    L.append("")
    if attach_rows:
        for a in attach_rows:
            L.append("- **%s**（页面字段：%s）%s"
                     % (core.attachment_label(a["键"]), a["页面字段"],
                        "｜✔ 必传" if a["必填"] else ""))
    else:
        L.append("- 按该企业简章要求准备。（简历 / 成绩单 / 证书 / 证件照等）")
    if ctx.get("resume"):
        L.append("- 本次建议使用简历：`%s`" % ctx["resume"])
    L.append("")
    L.append("---")
    L.append("")
    L.append("## 四、填完之后")
    L.append("")
    L.append("1. 逐项核对你粘进去的内容（尤其是**身份证号、手机号**这类关键字段）。")
    L.append("2. **附件、志愿顺序、最终提交**这三件事由你本人完成，任何自动化都不代劳。")
    L.append("3. 投完回来说一句，即完成入账。")
    L.append("")
    return "\n".join(L)


def _load_records(path):
    """读记录组拆分产物（`split_records.py` 生成）。读不到返回 None —— **清单照出**，
    不为一个可选的附录让整张清单失败。"""
    if not path:
        return None
    import json
    try:
        with io.open(path, encoding="utf-8") as fh:
            return json.loads(fh.read())
    except Exception:                                        # noqa: BLE001
        return None


def render_records(doc):
    """把记录组拆分结果渲染成清单的一节。**只渲染键名与段数，不渲染文本值。**

    为什么值得单独一节：经历类内容在页面上是**可多条的表格**（一组一行），不是单值框。
    清单如果只列「实习经历 = 一整段文字」，你在页面上根本不知道该往哪一行放。
    这一节把「切成几段、每段起止是什么」摊开，剩下的正文分配仍由你决定 ——
    **本工具不做「这段文字里哪句是企业名」这种语义推断**，猜错就是替你造假。
    """
    mods = (doc or {}).get("模块") or []
    if not mods:
        return ""
    L = []
    L.append("## 附：记录组 —— 多段该怎么填")
    L.append("")
    L.append("> 下列内容在页面上是**「表格 + 添加一行」**的多条结构，一组一行；")
    L.append("> 这里按**日期区间**把它切成了多段，**每段对应一行**。")
    L.append("> 起止时间以原文为准（**未擅自补日**）；每行内各个字段的正文由你自己分配。")
    L.append("")
    L.append("| 档案键 | 页面上的候选模块 | 段数 | 各段起止 |")
    L.append("|---|---|---|---|")
    for m in mods:
        spans = []
        for seg in m.get("记录") or []:
            if seg.get("_扁平时间"):
                spans.append("（扁平键直接取值）")
            elif seg.get("起"):
                spans.append("%s~%s" % (seg["起"], seg["止"] or "?"))
            else:
                spans.append("（无起止，需自己补）")
        cands = "、".join(m.get("候选模块名") or []) or "（通用表未覆盖，请照页面实际模块名填）"
        L.append("| `%s` | %s | %d | %s |"
                 % (m["档案键"], cands, m.get("段数") or 0, "；".join(spans)))
    L.append("")
    st = (doc or {}).get("统计") or {}
    if st.get("无区间键"):
        L.append("- 有日期但不成对（起止只够填一半）：%s" % "、".join(st["无区间键"]))
    if st.get("无日期键"):
        L.append("- 完全没有日期信息（起止需自己补）：%s" % "、".join(st["无日期键"]))
    L.append("")
    return "\n".join(L)


def collect_attachments(labels, fields, profile, index):
    """收集附件类字段（仅当给了页面标签时）。"""
    if not labels:
        return []
    matched, _ = core.map_fields(labels, fields, index)
    out = []
    for lb, (key, how) in matched.items():
        if core.is_attachment(key):
            meta = core.field_meta(fields, key)
            out.append({"页面字段": lb, "键": key, "必填": bool(meta.get("必填"))})
    return out


def build(labels, fields, profile, ctx, records_doc=None):
    """算出清单正文（不打印、不落盘）。

    把「算」与「打印/写盘」分开，自测才能断言**算出来的东西**，
    而不必去抓屏匹配文案。返回 (content, rows, ready, miss, attach, unmatched)。

    `records_doc` = 记录组拆分产物（`split_records.py` 生成）。给了就多出一节，
    说明**经历类内容在页面上是多条表格、该分成几段**；不给则清单与从前**一模一样**
    （不传参数行为不变，是这条升级的底线）。
    """
    index = core.build_index(fields)
    rows, unmatched, _ = build_rows(labels, fields, profile, index)
    attach = collect_attachments(labels, fields, profile, index)
    head, ready, miss = render(rows, ctx)
    body = render_attach(attach, ctx)

    # 未命中字段（页面有、规则库没有）如实告知，不猜
    tail = ""
    if unmatched:
        tail = ("\n## 五、规则库未命中（需你手动填写）\n\n"
                + "\n".join("- %s" % u for u in unmatched) + "\n")
    return head + "\n" + body + render_records(records_doc) + tail, \
        rows, ready, miss, attach, unmatched


def selftest():
    ok, bad = [0], [0]

    def check(name, cond, extra=""):
        if cond:
            ok[0] += 1
            print("  [OK] %s" % name)
        else:
            bad[0] += 1
            print("  [FAIL] %s %s" % (name, extra))

    fx = os.path.join(core.PROJECT_ROOT, "fixtures")
    prof_path = os.path.join(fx, "档案_示例.json")
    labels_path = os.path.join(fx, "页面字段_示例.txt")
    fields = core.load_rules(None)
    profile = core.load_profile(prof_path)
    with open(labels_path, encoding="utf-8") as fh:
        labels = [ln.strip() for ln in fh if ln.strip()]
    ctx = {"now": "2026-10-16 09:00", "company": "示例集团", "job": "财务会计岗"}

    content, rows, ready, miss, attach, unmatched = build(labels, fields, profile, ctx)

    check("① 清单非空且以标题开头",
          content.strip() != "" and content.startswith("# "), content[:20])
    check("② 每个页面标签都有归宿（就绪／缺项／附件／未命中）",
          len(rows) + len(attach) + len(unmatched) == len(labels),
          (len(rows), len(attach), len(unmatched), len(labels)))
    check("③ 就绪项的值逐条等于档案里的值",
          bool(ready) and all(str(r["值"]) == str(profile.get(r["键"], r["值"]))
                              for r in ready),
          len(ready))
    check("④ 就绪与缺项互斥，且并集 == rows",
          not (set(id(r) for r in ready) & set(id(r) for r in miss))
          and len(ready) + len(miss) == len(rows),
          (len(ready), len(miss), len(rows)))
    check("⑤ 缺项行在正文里带出档案键（`键` 代码跨度）",
          all("`%s`" % r["键"] in content for r in miss),
          [r["键"] for r in miss])
    check("⑥ 附件不进就绪／缺项表（各归各的节）",
          not (set(r["键"] for r in attach) & set(r["键"] for r in rows)))

    # ---- 隐私红线：真值只写进清单文件，终端一个都不打印 ----
    tmpd = tempfile.mkdtemp(prefix="ows-sheet-")
    dry = os.path.join(tmpd, "绝不落盘.md")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main(["--profile", prof_path, "--labels-file", labels_path,
                   "--dry-run", "--out", dry])
    printed = buf.getvalue()
    values = [str(v) for k, v in profile.items()
              if not str(k).startswith("_") and len(str(v)) >= 4]
    check("⑦ --dry-run 真的不落盘", rc == 0 and not os.path.exists(dry))
    check("⑧ 终端输出里不含档案里的任何真值", not [v for v in values if v in printed],
          [v for v in values if v in printed][:3])
    check("⑨ 而清单文件本身含值（证明「不打印」不是因为没算出来）",
          any(str(r["值"]) in content for r in ready))

    # ---- 未命中的标签：如实告知，绝不猜 ----
    bogus = "这个标签规则库里没有"
    c2, rows2, _r2, _m2, _a2, unm2 = build(labels + [bogus], fields, profile, ctx)
    check("⑩ 规则库不认识的标签进「未命中」，不猜值",
          unm2 == [bogus] and len(rows2) == len(rows), unm2)
    check("⑪ 未命中项在正文里点名（不静默丢弃）",
          bogus in c2 and "未命中" in c2)

    # ---- 不给页面标签：全量列举，必填优先 ----
    c3, rows3, _r3, _m3, a3, unm3 = build(None, fields, profile, ctx)
    check("⑫ 无标签时按规则库全量列举，且无「未命中」",
          len(rows3) > 0 and unm3 == [], (len(rows3), unm3))
    flags = [r["必填"] for r in rows3]
    check("⑬ 全量列举里必填排在选填之前",
          flags == sorted(flags, key=lambda b: not b), flags[:6])
    check("⑭ 全量列举不含附件字段，附件另计",
          not any(core.is_attachment(r["键"]) for r in rows3) and a3 == [])

    # ---- 覆盖语义：脚本自己拼的名字不覆盖；--overwrite 才覆盖 ----
    tgt = os.path.join(tmpd, "填写清单.md")
    with open(tgt, "w", encoding="utf-8") as fh:
        fh.write("旧内容")
    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        rc2 = main(["--profile", prof_path, "--labels-file", labels_path,
                    "--out", tgt])
    with open(tgt, encoding="utf-8") as fh:
        kept = fh.read()
    extra = [f for f in os.listdir(tmpd) if f != "填写清单.md"]
    check("⑮ 给了 --out 但没给 --overwrite → 不覆盖，原文件不动",
          rc2 == 0 and kept == "旧内容", kept[:10])
    check("⑯ 且确实另存出了新文件（不是默默丢弃）", len(extra) == 1, extra)

    buf3 = io.StringIO()
    with contextlib.redirect_stdout(buf3):
        rc3 = main(["--profile", prof_path, "--labels-file", labels_path,
                    "--out", tgt, "--overwrite"])
    with open(tgt, encoding="utf-8") as fh:
        overwritten = fh.read()
    check("⑰ 显式 --overwrite 才覆盖",
          rc3 == 0 and "旧内容" not in overwritten and "网申填写清单" in overwritten)

    # ---- 记录组附录（2026-10-16 新增）----
    # 底线是「不传就不变」，所以先钉住这一条。
    check("⑱ 不传记录组数据 → 不渲染该节（清单与从前一致）",
          render_records(None) == "" and render_records({}) == "")
    check("⑲ 反证：不传时清单正文里确实没有记录组节",
          "记录组 —— 多段该怎么填" not in content, content[-160:])

    rd = {"模块": [{"档案键": "实习经历", "候选模块名": ["实习经历"], "段数": 2,
                    "记录": [{"序": 1, "起": "2023-07", "止": "2023-09"},
                             {"序": 2, "起": "2024-01", "止": "2024-03"}]},
                   {"档案键": "论文专利", "候选模块名": [], "段数": 1,
                    "记录": [{"序": 1, "起": "", "止": ""}]}],
          "统计": {"无区间键": ["工作经历"], "无日期键": ["获奖情况"]}}
    txt = render_records(rd)
    check("⑳ 列出候选模块与段数", "实习经历" in txt and "| 2 |" in txt, txt[:140])
    check("㉑ 列出各段起止", "2023-07~2023-09" in txt and "2024-01~2024-03" in txt, txt[:200])
    check("㉒ 无候选模块时如实标注（不硬塞一个假模块名）",
          "通用表未覆盖" in txt, txt[:240])
    check("㉓ 两类缺口分开报（不成对 ≠ 没日期）",
          "有日期但不成对" in txt and "完全没有日期信息" in txt, txt[-160:])

    # 反证：该节**只**渲染结构与统计，不渲染任何正文值
    check("㉔ **该节只出结构与统计，不出现经历正文**（值不进清单旁注）",
          "在某公司" not in txt and "实习内容" not in txt)

    print("\n自测结果：%d 通过 / %d 失败" % (ok[0], bad[0]))
    return 0 if bad[0] == 0 else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="生成网申填写清单（保底模式）")
    ap.add_argument("--profile", help="本机档案 JSON 路径")
    ap.add_argument("--rules", help="字段规则库路径（默认 assets/字段规则_通用.json）")
    ap.add_argument("--labels", help="页面字段标签，英文逗号分隔")
    ap.add_argument("--labels-file", dest="labels_file", help="页面字段标签文件（每行一个）")
    ap.add_argument("--company", default="", help="企业名")
    ap.add_argument("--job", default="", help="岗位名")
    ap.add_argument("--url", default="", help="投递链接")
    ap.add_argument("--resume", default="", help="本次使用的简历文件名")
    ap.add_argument("--records", default="",
                    help="记录组拆分产物（split_records.py 生成），附进清单；不给则不变")
    ap.add_argument("--out", help="输出路径（.md）")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不落盘")
    ap.add_argument("--overwrite", action="store_true",
                    help="允许覆盖同名文件（默认不覆盖，自动加时间戳）")
    ap.add_argument("--selftest", action="store_true", help="用内置虚构数据自测")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    if not args.profile:
        ap.error("必须提供 --profile（或使用 --selftest）")

    fields = core.load_rules(args.rules)
    profile = core.load_profile(args.profile)

    labels = None
    if args.labels:
        labels = [s.strip() for s in args.labels.split(",") if s.strip()]
    elif args.labels_file and os.path.isfile(args.labels_file):
        with open(args.labels_file, "r", encoding="utf-8") as f:
            labels = [ln.strip() for ln in f if ln.strip()]

    ctx = {
        "now": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "company": args.company, "job": args.job,
        "url": args.url, "resume": args.resume,
    }
    records_doc = _load_records(getattr(args, "records", ""))
    content, rows, ready, miss, attach, unmatched = build(
        labels, fields, profile, ctx, records_doc=records_doc)
    if records_doc:
        print("  记录组：已附上拆分指引（%d 个键）" % len(records_doc.get("模块") or []))
    elif getattr(args, "records", ""):
        print("  [WARN] 记录组产物读不了，已跳过该节（清单其余部分不受影响）")

    print("清单生成完成")
    print("  页面字段：%s" % (len(labels) if labels else "（未提供，按规则库全量列举）"))
    print("  待填项：%d（就绪 %d ／ 档案缺项 %d）" % (len(rows), len(ready), len(miss)))
    print("  附件项：%d" % len(attach))
    print("  规则库未命中：%d" % len(unmatched))

    if args.dry_run:
        print("（--dry-run：未落盘）")
        return 0

    out = args.out
    if not out:
        out = os.path.join(os.getcwd(), "填写清单.md")

    # 统一 safe_write（2026-10-16）：脚本自己拼的默认名 **不覆盖**（换名另存），
    # 用户显式给了 --out 才由 --overwrite 决定。原子替换，不留半个文件。
    wrote = core.safe_write(out, content, overwrite=bool(args.out and args.overwrite))
    if os.path.abspath(wrote) != os.path.abspath(out):
        print("  同名文件已存在，改存为：%s" % wrote)
    print("  已写入：%s" % wrote)
    return 0


if __name__ == "__main__":
    sys.exit(main())
