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
import datetime
import os
import sys

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


def main():
    ap = argparse.ArgumentParser(description="生成网申填写清单（保底模式）")
    ap.add_argument("--profile", help="本机档案 JSON 路径")
    ap.add_argument("--rules", help="字段规则库路径（默认 assets/字段规则_通用.json）")
    ap.add_argument("--labels", help="页面字段标签，英文逗号分隔")
    ap.add_argument("--labels-file", dest="labels_file", help="页面字段标签文件（每行一个）")
    ap.add_argument("--company", default="", help="企业名")
    ap.add_argument("--job", default="", help="岗位名")
    ap.add_argument("--url", default="", help="投递链接")
    ap.add_argument("--resume", default="", help="本次使用的简历文件名")
    ap.add_argument("--out", help="输出路径（.md）")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不落盘")
    ap.add_argument("--overwrite", action="store_true",
                    help="允许覆盖同名文件（默认不覆盖，自动加时间戳）")
    ap.add_argument("--selftest", action="store_true", help="用内置虚构数据自测")
    args = ap.parse_args()

    if args.selftest:
        fx = os.path.join(core.PROJECT_ROOT, "fixtures")
        args.profile = os.path.join(fx, "档案_示例.json")
        args.labels_file = os.path.join(fx, "页面字段_示例.txt")
        args.company = "示例集团"
        args.job = "财务会计岗"
        args.out = args.out or os.path.join(core.PROJECT_ROOT, "examples",
                                            "填写清单_示例.md")
        # 自测产物落在固定路径，允许刷新，避免堆积一堆时间戳文件
        args.overwrite = True
        print("【自测模式】使用 fixtures/ 下的虚构数据")

    if not args.profile:
        ap.error("必须提供 --profile（或使用 --selftest）")

    fields = core.load_rules(args.rules)
    index = core.build_index(fields)
    profile = core.load_profile(args.profile)

    labels = None
    if args.labels:
        labels = [s.strip() for s in args.labels.split(",") if s.strip()]
    elif args.labels_file and os.path.isfile(args.labels_file):
        with open(args.labels_file, "r", encoding="utf-8") as f:
            labels = [ln.strip() for ln in f if ln.strip()]

    rows, unmatched, _ = build_rows(labels, fields, profile, index)
    attach = collect_attachments(labels, fields, profile, index)

    ctx = {
        "now": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "company": args.company, "job": args.job,
        "url": args.url, "resume": args.resume,
    }
    head, ready, miss = render(rows, ctx)
    body = render_attach(attach, ctx)

    # 未命中字段（页面有、规则库没有）如实告知，不猜
    tail = ""
    if unmatched:
        tail = ("\n## 五、规则库未命中（需你手动填写）\n\n"
                + "\n".join("- %s" % u for u in unmatched) + "\n")

    content = head + "\n" + body + tail

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

    if os.path.exists(out) and not args.overwrite:
        stem, ext = os.path.splitext(out)
        out = "%s_%s%s" % (stem,
                           datetime.datetime.now().strftime("%Y%m%d%H%M%S"), ext)
        print("  同名文件已存在，改存为：%s" % out)

    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(content)
    print("  已写入：%s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
