#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenWangShen 核心库
====================
职责：规则加载 / 标签索引 / 字段映射 / 档案读取。
依赖：仅 Python 3 标准库。零第三方依赖。

隐私原则（本模块的硬约束）
--------------------------
本模块只搬运「键名」与「标签」这类元信息，**任何情况下都不打印真实值**。
真实值只在调用方（本地脚本）内部从档案取出、直接交给目标（浏览器或本地文件），
不经过标准输出，因而不进入任何模型上下文。

作者：liyang（微信：liyang7078）
许可：MIT
"""

import json
import os
import re
import sys

# Windows 控制台默认编码可能不是 UTF-8，这里兜底，避免中文 print 直接崩
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(_HERE)
DEFAULT_RULES_PATH = os.path.join(PROJECT_ROOT, "assets", "字段规则_通用.json")

# 附件类字段的键名前缀约定（与规则库同源）
ATTACH_PREFIX = "附件_"


# --------------------------------------------------------------------------
# 归一化
# --------------------------------------------------------------------------
_BRACKET_RE = re.compile(r"[（(][^）)]*[)）]")
_SPACE_RE = re.compile(r"[\s\u3000]+")
_NOISE = ("*", "：", ":", "、", "，", ",", "?", "？", "!", "！", "-")


def norm(text):
    """
    把标签归一化成可比较的形式。

    处理：去首尾空白 → 去必填星号/标点 → 去括号及其内容 → 去所有空白 → 转小写。
    例："姓名（中文）*" → "姓名"；"Full Name" → "fullname"
    """
    if text is None:
        return ""
    s = str(text).strip()
    for ch in _NOISE:
        s = s.replace(ch, "")
    s = _BRACKET_RE.sub("", s)
    s = _SPACE_RE.sub("", s)
    return s.lower()


# --------------------------------------------------------------------------
# 规则库
# --------------------------------------------------------------------------
def load_rules(path=None):
    """
    读取字段规则库。返回字段列表（已剔除 _ 开头的元数据条目）。
    """
    p = path or DEFAULT_RULES_PATH
    with open(p, "r", encoding="utf-8") as f:
        raw = json.load(f)
    fields = []
    for item in raw.get("字段", []):
        key = item.get("键")
        if not key or str(key).startswith("_"):
            continue
        fields.append(item)
    return fields


def build_index(fields):
    """
    构建「归一化标签 → 键」索引。
    同一标签若被多个字段声明，后声明的**不覆盖**先声明的（先到先得，保证稳定）。
    """
    index = {}
    for item in fields:
        key = item["键"]
        for label in item.get("标签", []):
            n = norm(label)
            if not n:
                continue
            index.setdefault(n, key)
    return index


# --------------------------------------------------------------------------
# 映射
# --------------------------------------------------------------------------
def map_one(page_label, index, fields):
    """
    单个页面标签 → 档案键。
    返回 (键 or None, 置信度)  置信度 ∈ {"exact", "partial", None}
    策略：先精确；再「包含」匹配，多个候选时取标签最长的（最具体）。
    """
    n = norm(page_label)
    if not n:
        return None, None

    if n in index:
        return index[n], "exact"

    # 页面标签过短（如只有一个字）不参与模糊匹配，否则「名」会撞上「紧急联系人姓名」
    if len(n) < 2:
        return None, None

    # 包含匹配：页面标签更长时，规则标签被包含；反之也成立
    best_key, best_len = None, 0
    for item in fields:
        for label in item.get("标签", []):
            ln = norm(label)
            if not ln:
                continue
            if (ln in n or n in ln) and len(ln) > best_len:
                best_key, best_len = item["键"], len(ln)

    if best_key:
        # 长度 ≥2 才算有效，避免 "名" 这类单字乱命中
        if best_len >= 2:
            return best_key, "partial"
    return None, None


def map_fields(page_labels, fields, index=None):
    """
    批量映射。返回 (matched, unmatched)
      matched   = {页面标签: (键, 置信度)}
      unmatched = [未能映射的页面标签]
    """
    idx = index if index is not None else build_index(fields)
    matched, unmatched = {}, []
    for label in page_labels:
        key, how = map_one(label, idx, fields)
        if key:
            matched[label] = (key, how)
        else:
            unmatched.append(label)
    return matched, unmatched


def field_meta(fields, key):
    """按键取字段元数据（必填/类型/分组）。"""
    for item in fields:
        if item["键"] == key:
            return item
    return {"键": key, "标签": [key], "必填": False, "类型": "text", "分组": "未分类"}


def is_attachment(key):
    """附件类字段判定。"""
    return str(key).startswith(ATTACH_PREFIX)


def attachment_label(key):
    """附件键 → 人话标签：附件_简历 → 简历"""
    return str(key)[len(ATTACH_PREFIX):] if is_attachment(key) else str(key)


# --------------------------------------------------------------------------
# 档案
# --------------------------------------------------------------------------
def load_profile(path):
    """
    读取本机档案 JSON。返回 dict。
    校验：文件存在、JSON 合法、顶层是对象。
    """
    if not os.path.isfile(path):
        raise FileNotFoundError("档案文件不存在：%s" % path)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("档案顶层必须是 JSON 对象（{...}）")
    return data


def profile_report(profile, fields):
    """
    档案体检：**只输出字段名与有无，绝不输出值**。
    返回 {"总数", "已填", "缺失", "未登记": [...], "分组": {...}}
    """
    known = {item["键"] for item in fields}
    filled, missing = [], []
    for item in fields:
        key = item["键"]
        v = profile.get(key)
        ok = v is not None and str(v).strip() != ""
        (filled if ok else missing).append(key)

    extra = [k for k in profile.keys()
             if not str(k).startswith("_") and k not in known]

    # 分组统计（仅已填）
    groups = {}
    for item in fields:
        g = item.get("分组", "未分类")
        groups.setdefault(g, {"已填": 0, "缺失": 0})
        key = item["键"]
        v = profile.get(key)
        if v is not None and str(v).strip() != "":
            groups[g]["已填"] += 1
        else:
            groups[g]["缺失"] += 1

    return {
        "总数": len(fields),
        "已填": filled,
        "缺失": missing,
        "档案未登记字段": extra,
        "分组": groups,
    }


def value_of(profile, key):
    """
    按键取值。支持两种档案写法：
      1) 直接用规则库键名     {"姓名": "张三"}
      2) 键名不可用时，调用方自行处理别名
    返回值恒为字符串；空值返回 ""。
    """
    v = profile.get(key)
    if v is None:
        return ""
    if isinstance(v, (list, dict)):
        # 复杂结构不做字符串化猜测，交给调用方
        return v
    return str(v).strip()


if __name__ == "__main__":
    # 便于手工快速验证：只打印规则库统计，不涉及任何档案
    fs = load_rules()
    idx = build_index(fs)
    print("规则库加载 OK")
    print("字段数：%d" % len(fs))
    print("标签索引条目：%d" % len(idx))
    groups = {}
    for i in fs:
        groups[i.get("分组", "未分类")] = groups.get(i.get("分组", "未分类"), 0) + 1
    for g, c in groups.items():
        print("  - %s：%d 个字段" % (g, c))
