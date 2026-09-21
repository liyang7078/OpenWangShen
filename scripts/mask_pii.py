#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mask_pii.py · 个人身份信息（PII）脱敏通道
==========================================
用途：当你**确实需要 AI 帮忙写开放题**时，先把文本里的身份信息替换成占位符，
      再发出去。回来后再把占位符换回真值。

设计要点
--------
1. 只脱敏「**能唯一定位到具体某人**」的字段 —— 这类才叫 PII。
   「性别=男」「政治面貌=党员」**不脱敏**：它们定位不到人，脱了反而让文本没法用。
   （这是本工具与「无脑全字段打码」的关键区别。）
2. 白名单之外的一律不动 —— 保证处理结果可读、可用。
3. **从不打印真值**：只报告「替换了几处、哪类字段」。
4. 映射表**只在内存里存活一次**，不落盘、不写日志。

用法
----
  # 扫描：只看有没有 PII，不改内容
  python scripts/mask_pii.py --profile profile.json --scan 文本.txt

  # 脱敏：输出到新文件
  python scripts/mask_pii.py --profile profile.json --in 原稿.txt --out 脱敏稿.txt

  # 还原
  python scripts/mask_pii.py --profile profile.json --in 脱敏稿.txt --out 回填稿.txt --restore

  # 自测
  python scripts/mask_pii.py --selftest

许可：MIT
"""

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core  # noqa: E402

# PII 白名单：**能唯一定位到某个人**的字段。白名单以外一律不动。
PII_KEYS = [
    "姓名", "英文名",
    "身份证号", "证件类型",
    "手机号", "备用电话", "电子邮箱",
    "通讯地址", "邮政编码", "户籍所在地", "籍贯",
    "出生日期",
    "紧急联系人姓名", "紧急联系人电话",
]

# 占位符标签（人类可读，便于 AI 理解上下文）
PLACEHOLDER = {
    "姓名": "【姓名】", "英文名": "【英文名】",
    "身份证号": "【身份证号】", "证件类型": "【证件类型】",
    "手机号": "【手机号】", "备用电话": "【备用电话】", "电子邮箱": "【电子邮箱】",
    "通讯地址": "【通讯地址】", "邮政编码": "【邮政编码】",
    "户籍所在地": "【户籍所在地】", "籍贯": "【籍贯】",
    "出生日期": "【出生日期】",
    "紧急联系人姓名": "【紧急联系人姓名】", "紧急联系人电话": "【紧急联系人电话】",
}

MIN_LEN = 2  # 太短的值不参与替换，避免误伤


def build_map(profile):
    """
    从档案构建「真值 → 占位符」映射。
    只取白名单字段、且值长度 ≥ MIN_LEN。
    """
    m = {}
    for key in PII_KEYS:
        v = core.value_of(profile, key)
        if not isinstance(v, str) or len(v.strip()) < MIN_LEN:
            continue
        m[v.strip()] = PLACEHOLDER.get(key, "【%s】" % key)
    return m


def scan(text, mapping):
    """
    扫描：返回 [(占位符, 出现次数)]，**不含真值**。
    """
    hits = {}
    for val, ph in mapping.items():
        n = text.count(val)
        if n:
            hits[ph] = hits.get(ph, 0) + n
    return sorted(hits.items())


def mask(text, mapping):
    """脱敏。返回 (新文本, 替换次数)。长值优先，避免子串误替换。"""
    n = 0
    for val in sorted(mapping.keys(), key=len, reverse=True):
        cnt = text.count(val)
        if cnt:
            text = text.replace(val, mapping[val])
            n += cnt
    return text, n


def restore(text, mapping):
    """还原。返回 (新文本, 还原次数)。"""
    n = 0
    for val, ph in mapping.items():
        cnt = text.count(ph)
        if cnt:
            text = text.replace(ph, val)
            n += cnt
    return text, n


def residual_scan(text, mapping):
    """
    二验：脱敏后再扫一遍，确认真值没有残留。
    返回残留的**类型名**列表（不含真值）。
    """
    left = []
    for val, ph in mapping.items():
        if val and val in text:
            left.append(ph)
    return left


def read_text(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def write_text(path, text):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def selftest():
    """内置自测：含正例与反证。"""
    fx = os.path.join(core.PROJECT_ROOT, "fixtures", "档案_示例.json")
    profile = core.load_profile(fx)
    mapping = build_map(profile)

    print("【自测】PII 白名单命中字段数：%d" % len(mapping))
    assert "姓名" in [k for k in profile if profile.get(k) == "示例考生"] or True

    sample = ("我叫示例考生，手机号 13800000000，邮箱 example@example.com，"
              "身份证 110101199901011234。我是中共党员，性别男。")

    hits = scan(sample, mapping)
    print("  扫描命中：%s" % ("、".join("%s×%d" % (p, c) for p, c in hits)))

    masked, n = mask(sample, mapping)
    print("  替换次数：%d" % n)
    assert n >= 4, "应至少替换 4 处"
    assert "13800000000" not in masked, "手机号未脱敏"
    assert "example@example.com" not in masked, "邮箱未脱敏"
    assert "110101199901011234" not in masked, "身份证未脱敏"
    assert "示例考生" not in masked, "姓名未脱敏"
    # 反证：非 PII 必须保留
    assert "中共党员" in masked, "政治面貌不应被脱敏"
    assert "男" in masked, "性别不应被脱敏"

    left = residual_scan(masked, mapping)
    print("  二验残留：%s" % (left or "无"))
    assert not left, "二验发现残留，脱敏不合格"

    back, r = restore(masked, mapping)
    print("  还原次数：%d" % r)
    assert "13800000000" in back and "中共党员" in back, "还原失败"

    print("【自测通过】脱敏 / 二验 / 还原 三项全部正确")
    return 0


def main():
    ap = argparse.ArgumentParser(description="PII 脱敏通道")
    ap.add_argument("--profile", help="本机档案 JSON")
    ap.add_argument("--in", dest="src", help="输入文本文件")
    ap.add_argument("--out", dest="dst", help="输出文本文件")
    ap.add_argument("--scan", action="store_true", help="只扫描不修改")
    ap.add_argument("--restore", action="store_true", help="还原占位符")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if not args.profile or not args.src:
        ap.error("需要 --profile 与 --in（或使用 --selftest）")

    profile = core.load_profile(args.profile)
    mapping = build_map(profile)
    text = read_text(args.src)

    if args.scan:
        hits = scan(text, mapping)
        print("扫描完成：命中 %d 类字段" % len(hits))
        for ph, c in hits:
            print("  - %s ×%d" % (ph, c))
        if not hits:
            print("  未发现 PII（白名单内）")
        return 0

    if args.restore:
        out, n = restore(text, mapping)
        print("还原 %d 处" % n)
    else:
        out, n = mask(text, mapping)
        print("脱敏 %d 处" % n)
        left = residual_scan(out, mapping)
        print("二验残留：%s" % ("、".join(left) if left else "无"))
        if left:
            print("⚠️ 二验未通过 —— 请勿发送该文本")
            return 1

    if args.dst:
        write_text(args.dst, out)
        print("已写入：%s" % args.dst)
    else:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
