#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""词表漂移门：比对两份「字段规则」字典，只读，不写盘。

为什么需要这道门
----------------
OpenWangShen 与 LYSKILL 考生版共用同一份字段词表，但两边的表示形式
已经分叉：主容器键名（``字段`` / ``fields``）、类型值域（``text`` / ``文本``）、
附件类键前缀（``附件_x`` / ``_附件_x``）。这类差异**不会报错**，
只会让跨版搬过来的判断静默失效 —— 例如 ``if 类型 == "text"`` 永远不成立。

本脚本只做**检测**，不做合并：合并要人判断哪边是对的，
自动改写等于把一次人判断换成一次静默覆盖。

用法
----
    python scripts/sync_fields.py --peer <对端字典路径>
    python scripts/sync_fields.py --peer <路径> --check   # 有漂移则退出 1

退出码：0 无差异（或未加 --check）｜ 1 --check 下发现漂移 ｜ 2 参数或读取错误
"""

import argparse
import json
import os
import sys

# 类型值域归一：英文写法 → 中文写法（中文侧原样通过）
TYPE_CANON = {
    "text": "文本",
    "select": "下拉",
    "textarea": "多行文本",
    "radio": "单选",
    "file": "文件",
    "date": "日期",
}

FIELD_KEYS = ("字段", "fields")
META_KEYS = ("忽略", "值域别名", "字段优先级")


def load_doc(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def fields_of(doc):
    """取字段列表，兼容两种主容器键名。"""
    for key in FIELD_KEYS:
        value = doc.get(key)
        if isinstance(value, list):
            return value
    return []


def canon_key(key):
    """附件类键去掉前导下划线，使 ``附件_简历`` 与 ``_附件_简历`` 可比。"""
    key = str(key or "").strip()
    return key[1:] if key.startswith("_附件") else key


def canon_type(value):
    value = str(value or "").strip()
    return TYPE_CANON.get(value, value)


def index_fields(fields):
    """字段列表 → {键: {标签集合, 类型, 必填}}。键为空者跳过。"""
    out = {}
    for item in fields:
        if not isinstance(item, dict):
            continue
        key = canon_key(item.get("键"))
        if not key:
            continue
        labels = item.get("标签") or []
        out[key] = {
            "标签": set(str(x) for x in labels if str(x).strip()),
            "类型": canon_type(item.get("类型")),
            "必填": bool(item.get("必填")),
        }
    return out


def count_labels(fields):
    total = 0
    for item in fields:
        if isinstance(item, dict):
            total += len(item.get("标签") or [])
    return total


def compare(left, right):
    """返回差异字典。left / right 均为 index_fields 的产物。"""
    lk, rk = set(left), set(right)
    diff = {
        "仅左有": sorted(lk - rk),
        "仅右有": sorted(rk - lk),
        "标签不同": [],
        "类型不同": [],
        "必填不同": [],
    }
    for key in sorted(lk & rk):
        l, r = left[key], right[key]
        if l["标签"] != r["标签"]:
            diff["标签不同"].append(
                (key, sorted(l["标签"] - r["标签"]), sorted(r["标签"] - l["标签"]))
            )
        if l["类型"] != r["类型"]:
            diff["类型不同"].append((key, l["类型"], r["类型"]))
        if l["必填"] != r["必填"]:
            diff["必填不同"].append((key, l["必填"], r["必填"]))
    return diff


def compare_meta(left_doc, right_doc):
    """比对三个元数据段：忽略（列表）/ 值域别名、字段优先级（字典）。"""
    out = {}
    for key in META_KEYS:
        l, r = left_doc.get(key), right_doc.get(key)
        if isinstance(l, list) and isinstance(r, list):
            ls, rs = set(map(str, l)), set(map(str, r))
        elif isinstance(l, dict) and isinstance(r, dict):
            ls, rs = set(l), set(r)
        else:
            out[key] = ("形态不同", type(l).__name__, type(r).__name__)
            continue
        if ls != rs:
            out[key] = (sorted(ls - rs), sorted(rs - ls))
    return out


def summarize(path, doc, fields):
    return {
        "路径": path,
        "版本": str(doc.get("_版本") or "-"),
        "字段数": len(fields),
        "标签数": count_labels(fields),
        "值域别名组": len(doc.get("值域别名") or {}),
    }


def print_summary(title, s):
    print("%s：%s" % (title, s["路径"]))
    print(
        "  版本 %s ｜ 字段 %d ｜ 标签 %d ｜ 值域别名组 %d"
        % (s["版本"], s["字段数"], s["标签数"], s["值域别名组"])
    )


def print_diff(diff, meta):
    total = 0
    for name in ("仅左有", "仅右有"):
        items = diff[name]
        total += len(items)
        print("[%s] %d 个%s" % (name, len(items), ("：" + "、".join(items[:12])) if items else ""))
    if diff["标签不同"]:
        total += len(diff["标签不同"])
        print("[标签不同] %d 个" % len(diff["标签不同"]))
        for key, only_l, only_r in diff["标签不同"]:
            print("  - %s  仅左: %s  仅右: %s" % (key, only_l, only_r))
    if diff["类型不同"]:
        total += len(diff["类型不同"])
        print("[类型不同] %d 个" % len(diff["类型不同"]))
        for key, l, r in diff["类型不同"]:
            print("  - %s  左=%s 右=%s" % (key, l, r))
    if diff["必填不同"]:
        total += len(diff["必填不同"])
        print("[必填不同] %d 个" % len(diff["必填不同"]))
        for key, l, r in diff["必填不同"]:
            print("  - %s  左=%s 右=%s" % (key, l, r))
    for key, payload in meta.items():
        if payload and payload[0] == "形态不同":
            total += 1
            print("[%s 形态不同] 左=%s 右=%s" % (key, payload[1], payload[2]))
        elif payload:
            total += len(payload[0]) + len(payload[1])
            print(
                "[%s 组不同] 仅左 %d 个%s ｜ 仅右 %d 个%s"
                % (
                    key,
                    len(payload[0]),
                    ("：" + "、".join(payload[0][:10])) if payload[0] else "",
                    len(payload[1]),
                    ("：" + "、".join(payload[1][:10])) if payload[1] else "",
                )
            )
    return total


def default_own_path():
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("字段规则_通用.json", "字段映射_通用.json"):
        candidate = os.path.join(os.path.dirname(here), "assets", name)
        if os.path.isfile(candidate):
            return candidate
    return ""


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="比对两份字段规则字典，只读检测漂移（不写盘）"
    )
    parser.add_argument("--own", default="", help="本侧字典路径（默认自动探测 assets/ 下两种命名）")
    parser.add_argument("--peer", required=True, help="对端字典路径")
    parser.add_argument("--check", action="store_true", help="发现漂移时退出 1（CI / 验收门用）")
    args = parser.parse_args(argv)

    own_path = args.own or default_own_path()
    if not own_path:
        print("错误：找不到本侧字典，请用 --own 指定", file=sys.stderr)
        return 2
    for path in (own_path, args.peer):
        if not os.path.isfile(path):
            print("错误：文件不存在 %s" % path, file=sys.stderr)
            return 2

    try:
        own_doc, peer_doc = load_doc(own_path), load_doc(args.peer)
    except (OSError, ValueError) as exc:
        print("错误：读取字典失败 %s" % exc, file=sys.stderr)
        return 2

    own_fields, peer_fields = fields_of(own_doc), fields_of(peer_doc)
    if not own_fields or not peer_fields:
        print("错误：任一侧字段列表为空，可能不是字段规则字典", file=sys.stderr)
        return 2

    print_summary("本侧", summarize(own_path, own_doc, own_fields))
    print_summary("对端", summarize(args.peer, peer_doc, peer_fields))
    print()

    total = print_diff(
        compare(index_fields(own_fields), index_fields(peer_fields)),
        compare_meta(own_doc, peer_doc),
    )

    print()
    if total == 0:
        print("结论：两侧归一后一致（0 处差异）")
        return 0
    print("结论：发现 %d 处差异 —— 跨版搬代码前必须先对齐，否则类型判据会静默失效" % total)
    return 1 if args.check else 0


if __name__ == "__main__":
    sys.exit(main())
