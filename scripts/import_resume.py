#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
import_resume.py · 简历导入（本机抽取）
========================================
把手上那份简历，变成一份**预填好的档案**。

它解决什么
----------
`make_checklist.py` 给你一张空白问卷 —— 但简历里本来就有大半。
一项项重打一遍没有意义。

本脚本在**本机**把简历读成文本，用规则抽出字段，写成 `profile.json`：
  · 抽到的 → 直接填好
  · 抽不到的 → 留在问卷里标「待补」

**全程不联网、不调模型。** 简历内容不进 AI 上下文。

支持格式
--------
  .txt / .md        零依赖
  .docx             需 python-docx
  .pdf              需 pdfplumber 或 pypdf
  缺依赖时**明确报错并给出替代做法**，不静默失败。

隐私（硬约束）
--------------
档案是**真值文件**，所以本脚本：
  · 从不打印任何取值（只打印「抽到了哪几项」的**键名**）
  · 默认写到 `.gitignore` 已屏蔽的 `profile.json`
注意：正因如此，产物**不做** `guard()` 反证 —— 那道闸是用来拦「不该出门的东西」，
档案本来就该留在本机。

用法
----
  python scripts/import_resume.py --in 我的简历.docx
  python scripts/import_resume.py --in 简历.txt --out profile.json
  python scripts/import_resume.py --in 简历.txt --merge profile.json
  python scripts/import_resume.py --selftest

许可：MIT
"""

import argparse
import datetime
import json
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_OUT = os.path.join(ROOT, "profile.json")

# 标签与值之间的分隔符。**只认冒号与制表符** ——
# 空格分隔（`姓名 张三`）歧义太大，宁可留到「待补」让人自己填，也不猜。
SEP = re.compile(r"[:：\t]")

# 形态正则：不依赖标签，直接按值的样子认
# ⚠️ 量词必须**有界**。`[\w.+-]+@[\w-]+\.[A-Za-z]{2,}` 在长文本上是 O(N²)：
#    每个起始位置都会先吞掉一整段词字符再回溯。实测 4 万字 → 4.7 秒，
#    一份几十万字的简历能把本机拖住数十分钟。改成定界量词后是线性。
PATTERNS = (
    ("手机号", re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")),
    ("电子邮箱", re.compile(r"([\w.+-]{1,64}@[\w-]{1,63}\.[A-Za-z]{2,24})")),
    ("身份证号", re.compile(r"(?<!\d)(\d{17}[\dXx])(?!\d)")),
)

MAX_VAL = 120          # 超过这个长度多半不是字段值，是一段话
MAX_LAB = 20
MAX_TEXT = 300_000     # 简历文本上限：超出即截断，防止正则/内存拖死本机


# --------------------------------------------------------------------------
# 读文本
# --------------------------------------------------------------------------
def read_text(path):
    """把简历读成纯文本。缺依赖 → 抛 RuntimeError（带可执行的替代做法）。"""
    ext = os.path.splitext(path)[1].lower()

    if ext in ("", ".txt", ".md", ".markdown"):
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()

    if ext == ".docx":
        try:
            import docx                                  # type: ignore
        except ImportError:
            raise RuntimeError(
                "读 .docx 需要 python-docx。装它：pip install python-docx\n"
                "       不想装也可以：用 Word 把简历另存为「纯文本 (.txt)」再来。")
        doc = docx.Document(path)
        parts = [p.text for p in doc.paragraphs]
        for t in doc.tables:                             # 简历常把信息放表格里
            for row in t.rows:
                parts.append("\t".join(c.text.strip() for c in row.cells))
        return "\n".join(parts)

    if ext == ".pdf":
        try:
            import pdfplumber                            # type: ignore
            with pdfplumber.open(path) as pdf:
                return "\n".join((p.extract_text() or "") for p in pdf.pages)
        except ImportError:
            pass
        try:
            from pypdf import PdfReader                  # type: ignore
            return "\n".join((p.extract_text() or "") for p in PdfReader(path).pages)
        except ImportError:
            raise RuntimeError(
                "读 .pdf 需要 pdfplumber 或 pypdf。装它：pip install pdfplumber\n"
                "       不想装也可以：把简历另存为 .txt 再来。")

    raise RuntimeError("不支持的格式：%s（支持 .txt / .md / .docx / .pdf）" % (ext or "无扩展名"))


# --------------------------------------------------------------------------
# 抽取
# --------------------------------------------------------------------------
def extract(text, fields):
    """抽字段。返回 (值字典, 来源字典)。

    **只抽高置信度的**：标签行 与 值形态。抽不到的一律不填 —— 宁可让人补，
    也不填一个似是而非的值进去（错值比缺值更难发现）。
    键名一律取自词表，保证 `value_of(profile, 键)` 一定取得回来。
    """
    index = core.build_index(fields)
    known = {f["键"] for f in fields}
    got, how = {}, {}

    # ① 标签行：`姓名：张三` / `姓　名\t张三` / `Mobile: 138...`
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = SEP.split(line, 1)
        if len(parts) != 2:
            continue
        lab, val = parts[0].strip(), parts[1].strip()
        if not val or len(val) > MAX_VAL or len(lab) > MAX_LAB:
            continue
        key = index.get(core.norm(lab))
        if key and key not in got:
            got[key], how[key] = val, "标签行"

    # ② 值形态（标签没写也能认出来）
    for key, rx in PATTERNS:
        if key in got or key not in known:
            continue
        m = rx.search(text)
        if m:
            got[key], how[key] = m.group(1), "值形态"

    # ③ 由身份证号推算（纯本机，不猜别的）
    sid = got.get("身份证号", "")
    if len(sid) == 18:
        if "出生日期" not in got and "出生日期" in known:
            got["出生日期"] = "%s-%s-%s" % (sid[6:10], sid[10:12], sid[12:14])
            how["出生日期"] = "身份证推算"
        if "性别" not in got and "性别" in known:
            try:
                got["性别"] = "男" if int(sid[16]) % 2 else "女"
                how["性别"] = "身份证推算"
            except ValueError:
                pass
    return got, how


def required_keys(fields):
    """必填的值字段（附件类不算 —— 那是文件，不是字）。"""
    return [f["键"] for f in fields
            if f.get("必填") and not str(f["键"]).startswith("附件_")]


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

    fields = core.load_rules()
    # 虚构样本。取值一律用 fixtures/档案_示例.json 里的官方假值 ——
    # 它们是 dist_check.py 白名单内的值，不会把自己的自测举报成真值。
    sample = "\n".join([
        "姓名：示例考生",
        "性别：男",
        "政治面貌：中共党员",
        "最高学历：硕士研究生",
        "毕业院校：示例大学",
        "专业：会计学",
        "手机号：13800000000",
        "电子邮箱：example@example.com",
        "身份证号：110101199901011234",
        "自我评价：这段话是故意写长的，用来验证超过一百二十个字的取值不会被当成字段值。"
        "如果长度判断失效，这一整段就会被错误地抽取出来，自测随即失败。"
        "这里继续堆一些没有实际含义的字，确保它稳稳超过上限。"
        "简历里确实会有这种长段落，比如自我评价和项目描述，所以这条防线是必要的。",
    ])
    got, how = extract(sample, fields)

    check("① 标签行抽到姓名", got.get("姓名") == "示例考生", got.get("姓名"))
    check("② 标签行抽到政治面貌", got.get("政治面貌") == "中共党员")
    check("③ 标签行抽到毕业院校", got.get("毕业院校") == "示例大学")
    check("④ 值形态抽到手机号", got.get("手机号") == "13800000000")
    check("⑤ 值形态抽到邮箱", got.get("电子邮箱") == "example@example.com")
    check("⑥ 值形态抽到身份证", got.get("身份证号") == "110101199901011234")
    check("⑦ 身份证推算出生日期", got.get("出生日期") == "1999-01-01", got.get("出生日期"))
    check("⑧ 身份证推算性别", got.get("性别") == "男")
    check("⑨ 超长取值不被当字段值", got.get("自我评价") is None,
          (got.get("自我评价") or "")[:30])
    check("⑩ 抽不到的确实没填（不猜）", "通讯地址" not in got and "邮政编码" not in got)

    # 键名必须全部落在词表内，否则档案会「看着填了、实际取不到」
    unknown = [k for k in got if k not in {f["键"] for f in fields}]
    check("⑪ 抽出的键全部是词表键", not unknown, unknown)

    # 空文本不抛错
    g2, _ = extract("", fields)
    check("⑫ 空文本不抛错且零抽取", g2 == {})

    # 单字标签不误命中（`名` 不该撞上 `姓名`）
    g3, _ = extract("名：随便", fields)
    check("⑬ 单字标签不参与误匹配", "姓名" not in g3, g3)

    req = required_keys(fields)
    check("⑭ 必填键算得出来且无附件", bool(req) and not any(k.startswith("附件_") for k in req),
          len(req))

    print("\n自测结果：%d 通过 / %d 失败" % (ok[0], bad[0]))
    return 0 if bad[0] == 0 else 1


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="简历导入：在本机把简历抽成预填档案（不联网、不进模型）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python scripts/import_resume.py --in 我的简历.docx\n"
               "  python scripts/import_resume.py --in 简历.txt --merge profile.json\n")
    ap.add_argument("--in", dest="infile", required=False, help="简历文件（.txt/.md/.docx/.pdf）")
    ap.add_argument("--out", default="", help="输出档案路径（默认 profile.json）")
    ap.add_argument("--merge", default="", help="合并进已有档案（写回该文件，先备份）")
    ap.add_argument("--rules", default=None, help="字段规则库路径")
    ap.add_argument("--overwrite", action="store_true", help="允许覆盖同名档案")
    ap.add_argument("--selftest", action="store_true", help="自测")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()
    if not args.infile:
        ap.print_help()
        return 2

    fields = core.load_rules(args.rules)
    try:
        text = read_text(args.infile)
    except RuntimeError as exc:
        print("[ERR] %s" % exc)
        return 2
    except OSError as exc:
        print("[ERR] 读不了这个文件：%s" % exc)
        return 2

    # 超大文本截断：本工具只认「标签: 值」这类短行，
    # 第 30 万字之后不可能还是简历字段，继续扫只会白烧 CPU。
    if len(text) > MAX_TEXT:
        print("[提示] 简历 %d 字，超过上限 %d 字 —— 只扫前 %d 字。"
              % (len(text), MAX_TEXT, MAX_TEXT))
        print("       多半是排版残留（如 PDF 转文本的重复空行）。")
        text = text[:MAX_TEXT]

    got, how = extract(text, fields)

    # ---- 合并进已有档案（已有值**不覆盖**，只补空）----
    dest = args.merge or args.out or DEFAULT_OUT
    base = {}
    if args.merge and os.path.isfile(args.merge):
        try:
            base = core.load_profile(args.merge)
        except Exception as exc:                          # noqa: BLE001
            print("[ERR] 已有档案读不了：%s" % exc)
            return 2

    out = dict(base)
    added = []
    for k, v in got.items():
        if not str(out.get(k) or "").strip():
            out[k] = v
            added.append(k)

    if args.merge and os.path.isfile(args.merge):
        # 改既有档案 ⇒ 先备份（铁律：改任何既有文件，第一步先备份）
        shutil.copy2(args.merge,
                     args.merge + ".bak" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))

    # 统一 safe_write（2026-10-16）：原子替换 + 默认不覆盖同名（换名另存）。
    # 合并（--merge）是**回写同一份档案**，属授权覆盖；新建时只有 --overwrite 才覆盖。
    payload = json.dumps(out, ensure_ascii=False, indent=2)
    wrote = core.safe_write(dest, payload,
                            overwrite=bool(args.merge or args.overwrite),
                            root=ROOT)
    if os.path.abspath(wrote) != os.path.abspath(dest):
        print("[提示] 同名档案已存在，本次改存：%s" % os.path.basename(wrote))
        print("       要直接覆盖请加 --overwrite，要合并请用 --merge。")

    # ---- 报告：**只列键名，不列取值** ----
    missing = [k for k in required_keys(fields) if not str(out.get(k) or "").strip()]
    print("")
    print("读入：%s ｜ 抽出 %d 项" % (os.path.basename(args.infile), len(got)))
    print("抽出：%d 项 —— %s" % (len(got), "、".join(got) or "（无）"))
    if added:
        print("本次填入：%d 项 —— %s" % (len(added), "、".join(added)))
    kept = [k for k in got if k not in added]
    if kept:
        print("原有值不动：%d 项 —— %s" % (len(kept), "、".join(kept)))
    print("待补必填：%d 项 —— %s" % (len(missing), "、".join(missing) or "（无）"))
    print("")
    print("已写入：%s" % wrote)
    print("⚠️ 这份文件是你的真实信息，只放本机。别提交到 Git（.gitignore 已屏蔽）。")
    print("   接着体检一下：python scripts/wangshen.py check --profile %s"
          % os.path.basename(wrote))
    return 0


if __name__ == "__main__":
    sys.exit(main())
