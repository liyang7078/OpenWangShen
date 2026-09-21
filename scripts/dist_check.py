#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dist_check.py · 分发前「真实值」扫描器
======================================
用途：在把仓库打包、发布、或交给任何人之前，扫一遍**入库文件**，
      确认没有任何真实个人值混在里面。

为什么要单独做这一步
--------------------
`.gitignore` 只挡得住「文件名符合规则的东西」。
它挡不住「把真值粘贴进示例文件」「把真值写进文档」这类事故。
本脚本就是补这一刀。

四条判据
--------
1. **路径规则**：入库路径命中「本就该被忽略」的模式（profile.json 等）
2. **值等同**  ：入库文本中出现与 `--profile` 某一字段**完全相等**的值
3. **结构模式**：出现校验位合法的 18 位身份证 / 11 位手机号 / 邮箱
4. **人属性**  ：规则类文件（`assets/` · `sites/`）里出现第一人称或数值型个人属性

**判据 2 是最硬的一条** —— 只有它能区分「文档里的假样例」与「真人数据」。
3 / 4 两条给 WARN：它们无法区分示例与真值，只能提示人工看一眼。
判据 3 会跳过 `fixtures/档案_示例.json` 里的**官方假值**，
否则示例文档的 WARN 会把真信号淹没。

设计约束
--------
- 只用标准库（与 core.py / mask_pii.py 一致）
- **从不打印真值**，只打印「哪个文件、哪一行、哪类字段」
- 判据 2 是 mask_pii.residual_scan 的推广：
  后者只扫 PII 白名单，前者扫档案里的**全部**字段

用法
----
  # 公开发布前的标准动作
  python scripts/dist_check.py --profile profile.json

  # 自测
  python scripts/dist_check.py --selftest

退出码：0 = 无 FAIL 可分发；1 = 存在 FAIL 禁止分发

许可：MIT
"""

import argparse
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core  # noqa: E402

# ── 判据 1：路径规则 ───────────────────────────────────────────
PATH_FORBIDDEN = [re.compile(p) for p in (
    r"(^|/)profile\.json$",
    r"(^|/)[^/]*profile[^/]*\.json$",
    r"(^|/)填写清单.*\.md$",
    r"(^|/)填表报告.*\.json$",
)]
# 白名单：这些名字看起来像、但确实是可分发的内容
PATH_EXEMPT = [re.compile(p) for p in (
    r"^fixtures/档案_示例\.json$",
    r"^assets/.*\.json$",
    r"^sites/.*\.json$",
    r"^examples/填写清单_示例\.md$",
    r"^examples/填表报告_示例\.json$",
)]

# ── 判据 3：结构模式 ───────────────────────────────────────────
ID_RE = re.compile(r"(?<!\d)(\d{17}[\dXx])(?!\d)")
MOBILE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[A-Za-z]{2,}")
ID_W = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
ID_C = "10X98765432"

# ── 判据 4：人属性 ────────────────────────────────────────────
RULE_FILE_RE = re.compile(r"^(assets|sites)/")
PERSON_MARKERS = ("本人", "我的", "我叫", "我于", "我毕", "我毕业")
# 注意 \"? —— 规则库是 JSON，键名带引号（"年龄": 24），
# 不允许引号会让这条判据在真实文件上完全失效。
NUMERIC_ATTR_RE = re.compile(r"(年龄|分数|成绩|GPA|排名)\"?\s*[:：=]\s*-?\d")

SKIP_SUFFIX = (".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip",
               ".pyc", ".woff", ".woff2", ".ttf")


def id_checksum_ok(v):
    """18 位身份证的校验位是否合法。"""
    if len(v) != 18:
        return False
    try:
        s = sum(int(v[i]) * ID_W[i] for i in range(17))
    except ValueError:
        return False
    return ID_C[s % 11] == v[17].upper()


def tracked_files():
    """
    列出**入库**文件（相对路径，正斜杠）。
    优先用 git ls-files —— 因为分发出去的正是它。
    git 不可用时退回遍历目录。
    """
    try:
        r = subprocess.run(["git", "ls-files"], cwd=core.PROJECT_ROOT,
                           capture_output=True, text=True, encoding="utf-8")
        if r.returncode == 0 and r.stdout.strip():
            return [p.replace("\\", "/") for p in r.stdout.splitlines() if p.strip()]
    except OSError:
        pass

    out = []
    for root, dirs, files in os.walk(core.PROJECT_ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        for f in files:
            full = os.path.join(root, f)
            out.append(os.path.relpath(full, core.PROJECT_ROOT).replace("\\", "/"))
    return sorted(out)


def read_lines(path):
    """读文本行；二进制/不可解码返回 None。"""
    if path.lower().endswith(SKIP_SUFFIX):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().splitlines()
    except (UnicodeDecodeError, OSError):
        return None


def profile_values(profile):
    """
    摊平档案里**所有**字符串值 → {值: 字段路径}。
    只看长度 ≥2 的值，避免「男」「1」这类短值满仓库误报。
    """
    found = {}

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                if str(k).startswith("_"):
                    continue
                walk(v, str(k))
        elif isinstance(node, list):
            for v in node:
                walk(v, path)
        elif isinstance(node, str):
            s = node.strip()
            if len(s) >= 2:
                found.setdefault(s, path)

    walk(profile, "")
    return found


def documented_fake_values():
    """
    「官方假值」清单 —— 取自 fixtures/档案_示例.json。

    设计理由：判据 3 无法区分「文档里的假样例」和「真人数据」。
    与其让示例值满屏报 WARN（真信号会被噪音淹没），
    不如把**唯一权威的假数据源**作为白名单：
    出现在别处的值只要不在这个白名单里，就仍然会被报出来。
    """
    path = os.path.join(core.PROJECT_ROOT, "fixtures", "档案_示例.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return set()

    vals = set()

    def walk(node):
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, str) and node.strip():
            vals.add(node.strip())

    walk(data)
    return vals


def check(profile=None, verbose=True):
    """
    跑全部判据。返回 (fails, warns)，均为 [(判据, 文件, 行号, 说明)]。
    说明里**绝不含真值**。
    """
    fails, warns = [], []
    files = tracked_files()

    # 判据 1
    for rel in files:
        if any(p.search(rel) for p in PATH_EXEMPT):
            continue
        for p in PATH_FORBIDDEN:
            if p.search(rel):
                fails.append(("路径规则", rel, 0, "该路径本应被 .gitignore 忽略，却出现在入库清单里"))
                break

    # 判据 2（需要真档案做对照）
    values = profile_values(profile) if profile else {}
    fake = documented_fake_values()

    for rel in files:
        lines = read_lines(os.path.join(core.PROJECT_ROOT, rel))
        if lines is None:
            continue

        is_rule_file = bool(RULE_FILE_RE.match(rel))

        for i, line in enumerate(lines, 1):
            # 判据 2
            for val, field in values.items():
                if val in line:
                    fails.append(("值等同", rel, i, "与档案字段「%s」的值完全一致" % field))

            # 判据 3（跳过官方假值，否则示例文档会淹没真信号）
            for m in ID_RE.finditer(line):
                if m.group(1) not in fake and id_checksum_ok(m.group(1)):
                    warns.append(("结构模式", rel, i, "校验位合法的 18 位身份证"))
                    break
            for m in MOBILE_RE.finditer(line):
                if m.group(1) not in fake:
                    warns.append(("结构模式", rel, i, "11 位手机号"))
                    break
            for m in EMAIL_RE.finditer(line):
                if m.group(0) not in fake:
                    warns.append(("结构模式", rel, i, "邮箱"))
                    break

            # 判据 4（只对「必须与人无关」的规则文件生效）
            if is_rule_file:
                for mk in PERSON_MARKERS:
                    if mk in line:
                        warns.append(("人属性", rel, i, "规则文件里出现第一人称「%s」" % mk))
                        break
                if NUMERIC_ATTR_RE.search(line):
                    warns.append(("人属性", rel, i, "规则文件里出现数值型个人属性"))

    if verbose:
        print("扫描入库文件：%d 个" % len(files))
        print("官方假值白名单：%d 个（取自 fixtures/档案_示例.json）" % len(fake))
        if profile:
            print("对照档案字段值：%d 个（只用于比对，不打印）" % len(values))
        print()

        for tag, rel, ln, why in fails:
            loc = "%s:%d" % (rel, ln) if ln else rel
            print("[FAIL] %-8s %s" % (tag, loc))
            print("        %s" % why)
        for tag, rel, ln, why in warns:
            loc = "%s:%d" % (rel, ln) if ln else rel
            print("[WARN] %-8s %s" % (tag, loc))
            print("        %s" % why)

        if not fails and not warns:
            print("未发现任何可疑内容。")

        print()
        print("结论：FAIL %d 项 / WARN %d 项 —— %s"
              % (len(fails), len(warns), "禁止分发" if fails else "可以分发"))

    return fails, warns


def selftest():
    """自测：正例（假样例放行）+ 反例（真值拦截）。"""
    import tempfile

    tmp = tempfile.mkdtemp(prefix="distcheck_")
    sample = os.path.join(tmp, "x.txt")

    # 假数据一律取仓库自带的示例档案 —— 自测绝不自己编号码。
    # （教训：自测里编一个手机号，扫描器会掉过头来举报自己）
    fake_profile = core.load_profile(
        os.path.join(core.PROJECT_ROOT, "fixtures", "档案_示例.json"))
    values = profile_values(fake_profile)
    assert values, "未能从示例档案摊平出任何值"

    probe = "13800000000"          # 示例档案里的手机号，在官方假值白名单内
    assert probe in values, "示例档案里找不到用于验证的手机号"
    print("【自测 1】示例档案值摊平：%d 个 ✓" % len(values))

    # 判据 2：真值出现在文本里 → 必须被检出
    with open(sample, "w", encoding="utf-8") as f:
        f.write("这是文档\n联系人：%s\n" % probe)
    hit = [v for v in values if v in open(sample, encoding="utf-8").read()]
    assert probe in hit, "判据 2 未能检出真值"
    print("【自测 2】值等同检出 ✓")

    # 判据 3：校验位 —— 合法号由算法**现场构造**，绝不手写。
    # （教训：手写身份证号几乎必然校验位算错，会让自测本身失效）
    base = "11010119900307751"
    s = sum(int(base[i]) * ID_W[i] for i in range(17))
    good_id = base + ID_C[s % 11]
    bad_id = base + ID_C[(s + 1) % 11]
    assert id_checksum_ok(good_id), "算法构造的合法身份证未通过"
    assert not id_checksum_ok(bad_id), "非法校验位未被识破"
    assert len(good_id) == 18, "构造出的号码长度不对"
    print("【自测 3】身份证校验位判定（合法号由算法构造，不手写）✓")

    # 官方假值白名单：必须能读到示例档案
    fake = documented_fake_values()
    assert fake, "未能读到官方假值清单（fixtures/档案_示例.json）"
    assert any("@" in v for v in fake), "假值清单里应含示例邮箱"
    print("【自测 4】官方假值白名单：%d 个 ✓" % len(fake))

    # 判据 4：规则文件里的人属性标记
    assert PERSON_MARKERS[0] in "本人姓名", "第一人称标记失效"
    assert NUMERIC_ATTR_RE.search('"年龄": 24'), "数值型个人属性未识别"
    assert not NUMERIC_ATTR_RE.search('"年龄": "填写年龄"'), "文案误报"
    print("【自测 5】人属性判据 ✓")

    # 判据 1：路径白名单必须放行示例文件
    assert any(p.search("fixtures/档案_示例.json") for p in PATH_EXEMPT), "示例文件被误拦"
    assert not any(p.search("profile.json") for p in PATH_EXEMPT), "profile.json 被误放行"
    print("【自测 6】路径规则与白名单 ✓")

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    print("【自测通过】四条判据全部生效")
    return 0


def main():
    ap = argparse.ArgumentParser(description="分发前真实值扫描")
    ap.add_argument("--profile", help="本机档案 JSON（提供后启用最硬的判据 2）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    profile = core.load_profile(args.profile) if args.profile else None
    fails, _ = check(profile)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
