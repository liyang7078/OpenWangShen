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

作者：liyang7078
许可：MIT
"""

import datetime
import json
import os
import re
import sys
import tempfile

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
# placeholder 里的提示前缀：「请选择日期」真正想说的是「日期」。
# 不剥掉的话，「请输入/请选择」这类提示会参与匹配 —— 实测「请选择」会撞上「性别」。
_PLACEHOLDER_PREFIX_RE = re.compile(
    r"^(请输入|请选择|请填写|请上传|请注明|请录入|请提供|请选择您的|请输入您的)")


def norm(text):
    """
    把标签归一化成可比较的形式。

    处理：去首尾空白 → 去必填星号/标点 → 去括号及其内容 → 去所有空白 → 转小写。
    例："姓名（中文）*" → "姓名"；"Full Name" → "fullname"
    """
    if text is None:
        return ""
    s = str(text).strip()
    s = _PLACEHOLDER_PREFIX_RE.sub("", s)
    for ch in _NOISE:
        s = s.replace(ch, "")
    s = _BRACKET_RE.sub("", s)
    s = _SPACE_RE.sub("", s)
    return s.lower()


# --------------------------------------------------------------------------
# 泛化短标签黑名单（2026-10-15 新增）
# --------------------------------------------------------------------------
# 「日期」这两个字**不指向任何具体字段**。但没有这层保护时，「包含匹配」会把它
# 撞到标签最长的那个字段上 —— 实测（本机 165 字段词表）：
#
#     日期 → 加入党派时间      状态 → 健康状况        性质 → 户籍
#     时间 → 获得惩罚/处分时间   方式 → 辅导员联系方式   请选择 → 性别
#
# 后果比「填不上」严重得多：**填错是看不见的**（页面不报错、程序报"已填"）。
# 所以这里的判据是**拒写优于填错** —— 宁可交回给考生手填。
#
# 只拦「partial（包含匹配）」；「exact（精确相等）」一律放行：
# 页面字段就叫「备注」「成绩」「姓名」时，精确命中就是对的。
GENERIC_LABELS = frozenset((
    "日期", "时间", "名称", "类型", "状态", "性质", "方式", "情况", "原因",
    "内容", "简介", "描述", "说明", "等级", "分数", "类别", "号码", "其他",
    "开始", "结束", "年月", "请选择", "请输入", "请填写", "添加", "新增",
))


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
            # 泛化短标签不参与包含匹配（见 GENERIC_LABELS 的实测依据）：
            # 「日期」「状态」这类词撞上谁都是错的，交回人工。
            if n in GENERIC_LABELS:
                return None, None
            return best_key, "partial"
    return None, None


def map_one_strict(page_label, index, fields):
    """
    同 `map_one`，但**匹配不唯一时拒写**（返回 (None, "ambiguous")）。

    为什么另开一个函数
    ------------------
    `map_one` 在多个候选里取「标签最长」的那个，看着聪明，其实是**掷骰子**：
    若两个不同字段的标签并列最长，谁先声明谁赢 —— 结果取决于规则库的书写顺序，
    同一页面换个版本就可能填错。这里把「并列」显式判为歧义，交回人工。

    实测收益：把「拒写优于填错」落成代码，而不是只写在文档里。
    """
    n = norm(page_label)
    if not n or len(n) < 2:
        return None, None
    # 精确命中优先：页面字段就叫「备注」「成绩」时，精确相等就是对的
    if n in index:
        return index[n], "exact"
    # 只有「模糊匹配」才受泛化黑名单约束
    if n in GENERIC_LABELS:
        return None, None

    best = []
    best_len = 0
    for item in fields:
        for label in item.get("标签", []):
            ln = norm(label)
            if not ln:
                continue
            if (ln in n or n in ln):
                if len(ln) > best_len:
                    best_len, best = len(ln), [item["键"]]
                elif len(ln) == best_len:
                    best.append(item["键"])

    if best_len < 2 or not best:
        return None, None
    if len(set(best)) > 1:
        return None, "ambiguous"          # 并列最长 ⇒ 拒写
    return best[0], "partial"


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


# --------------------------------------------------------------------------
# 值域别名（2026-10-15 接线）
# --------------------------------------------------------------------------
# 规则库里一直存着 14 组「值域别名」（本科 ↔ 大学本科 / 硕士 ↔ 硕士研究生 …），
# 但引擎从来没读过 —— 页面下拉里写着「大学本科」，档案里写着「本科」，
# 匹配不上 ⇒ **静默失败**（看着在填，实际没选中）。这一节把它接上。
_VA_CACHE = {}


def value_aliases(path=None):
    """
    读规则库顶层的「值域别名」。
    返回 {类别: {标准值: [别名, …]}}
    """
    p = path or DEFAULT_RULES_PATH
    if p in _VA_CACHE:
        return _VA_CACHE[p]
    try:
        with open(p, "r", encoding="utf-8") as f:
            raw = json.load(f)
        va = raw.get("值域别名") or {}
    except (OSError, ValueError):
        va = {}
    _VA_CACHE[p] = va
    return va


_ALIAS_INDEX = {}


def _alias_index(path=None):
    """归一化别名 → 标准值。例：'大学本科' → '本科'。"""
    key = path or DEFAULT_RULES_PATH
    if key in _ALIAS_INDEX:
        return _ALIAS_INDEX[key]
    idx = {}
    for _cat, mapping in value_aliases(path).items():
        if not isinstance(mapping, dict):
            continue
        for std, alts in mapping.items():
            idx.setdefault(norm(std), std)
            for a in (alts or []):
                idx.setdefault(norm(a), std)
    _ALIAS_INDEX[key] = idx
    return idx


def canonical_value(value, path=None):
    """
    把档案里的值归一到一个**标准值**。
    '大学本科' → '本科'；'CET6' 之类不在表里的 → 原样返回。
    """
    v = str(value or "").strip()
    if not v:
        return ""
    return _alias_index(path).get(norm(v), v)


def option_aliases(value, path=None):
    """
    给定标准值，返回它在页面里**所有可能的写法**（含自身）。
    例：'本科' → {'本科','大学本科','全日制本科','学士','本科生'}
    """
    v = str(value or "").strip()
    if not v:
        return set()
    std = canonical_value(v, path)
    out = {v, std}
    for _cat, mapping in value_aliases(path).items():
        if not isinstance(mapping, dict):
            continue
        for s, alts in mapping.items():
            if norm(s) == norm(std) or norm(s) == norm(v):
                out.add(s)
                out.update(a for a in (alts or []) if a)
    return {x for x in out if x}


def match_option(value, options, path=None):
    """
    在页面**实际选项**里找出与档案值等价的那一个，返回选项原文；找不到返回 None。

    三级（由严到宽）：
      ① 归一后完全相等
      ② 命中值域别名（本科 ↔ 大学本科）
      ③ 互为包含（长度 ≥2，防「男」撞「男装」）
    """
    opts = [str(o).strip() for o in (options or []) if str(o or "").strip()]
    if not opts or not str(value or "").strip():
        return None

    v = str(value).strip()
    nv = norm(v)
    for o in opts:
        if norm(o) == nv:
            return o

    alts = option_aliases(v, path)
    na = {norm(a) for a in alts if norm(a)}
    for o in opts:
        if norm(o) in na:
            return o

    std = norm(canonical_value(v, path))
    if len(std) >= 2:
        for o in opts:
            no = norm(o)
            if len(no) >= 2 and (no in std or std in no):
                return o
    return None


# --------------------------------------------------------------------------
# 字段优先级（2026-10-15 接线）
# --------------------------------------------------------------------------
# 规则库顶层带着一张「字段优先级」表（按网申出现频次分层：1 最高，默认 9）。
# 以前引擎不读它 ⇒ 每站 300 秒预算被低价值字段先吃光，高频字段反而没填上。
_PRIO_CACHE = {}


def field_priority(path=None):
    """{键: 层号}。层号越小越先填。"""
    p = path or DEFAULT_RULES_PATH
    if p not in _PRIO_CACHE:
        try:
            with open(p, "r", encoding="utf-8") as f:
                raw = json.load(f)
            prio = raw.get("字段优先级") or {}
        except (OSError, ValueError):
            prio = {}
        _PRIO_CACHE[p] = {k: v for k, v in prio.items()
                          if not str(k).startswith("_")}
    return _PRIO_CACHE[p]


def priority_rank(key, path=None):
    """越小越先填；未列入的排最后。"""
    try:
        return int(field_priority(path).get(key))
    except (TypeError, ValueError):
        return 999


def sort_by_priority(items, key_field="key", path=None):
    """
    按字段优先级**稳定**排序（同层保持原有顺序 = 页面顺序）。
    预算紧张时这一步决定「先填哪些」。
    """
    return sorted(items, key=lambda it: priority_rank(it.get(key_field), path))


# --------------------------------------------------------------------------
# URL 门禁（安全）
# --------------------------------------------------------------------------
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "0.0.0.0", "[::1]")


def url_gate(url, allow_local=False):
    """
    填表前的 URL 门禁。返回 (ok, 说明)。

    为什么需要
    ----------
    `fill_form --url` 会把**档案里的真值**写进这个页面。若不校验：
      · `file:///` 能打开本机文件，页面脚本可回读本机路径
      · `javascript:` / `data:` 可直接执行脚本
      · 内网地址可被用来探测内网服务
    更根本的风险是「真值被填进一个不受信任的页面」—— 本函数只做**硬拦截**；
    信任判断由使用者自己做（终端会再提示一次）。

    `allow_local=True` 时放行本机/内网地址（仅供开发自测）。
    """
    import ipaddress
    from urllib.parse import urlsplit

    raw = str(url or "").strip()
    if not raw:
        return False, "URL 为空"
    try:
        u = urlsplit(raw)
    except ValueError as e:
        return False, "URL 解析失败：%s" % e

    scheme = (u.scheme or "").lower()
    if scheme not in ("http", "https"):
        return False, ("只允许 http / https，当前是 %r —— "
                       "file://、javascript:、data: 一律拒绝" % scheme)

    host = (u.hostname or "").lower()
    if not host:
        return False, "URL 缺少主机名"
    if allow_local:
        return True, ""

    if (host in LOCAL_HOSTS or host.endswith(".localhost")
            or host.endswith(".local") or host.endswith(".internal")):
        return False, "拒绝本机/内网地址：%s（确需自测请加 --allow-local）" % host

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True, ""          # 是域名 ⇒ 交给使用者判断
    if (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
        return False, "拒绝内网/保留地址：%s（确需自测请加 --allow-local）" % host
    return True, ""


# --------------------------------------------------------------------------
# 统一产物写入（2026-10-16 新增）
# --------------------------------------------------------------------------
# 为什么要有这一个函数
# --------------------
# 四个脚本各自实现了「同名不覆盖」，还有几个根本没实现 —— 于是同一件事有四种
# 写法、三种行为。更要紧的是两条容易漏掉的性质：
#   · **路径基准**：`--out` 是可以被外部传入的，不加校验就能把产物写到仓库外，
#     甚至覆盖别人的文件。这是一条安全性质，不能靠每个脚本自觉。
#   · **原子性**：写到一半崩掉会留下半个文件；下次看到「文件存在」就以为写完了。
#     ⇒ 先写临时文件再 `os.replace`（同目录内替换在 POSIX/NTFS 上都是原子操作）。
#
# 覆盖策略（与 .gitignore 的注释同源）
# ------------------------------------
# 默认 **不覆盖**：存在同名就加时间戳另存。用户显式给了 `--out` 且加 `--overwrite`
# 时才覆盖 —— 「我让你写这个名字」和「脚本自己拼的名字」是两回事，前者是授权。
def ensure_dir(path):
    """确保某文件所在目录存在。"""
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    return d


def unique_path(path):
    """
    同名则加时间戳（再撞则补序号）。返回一个**当前不存在**的路径。
    与 make_sheet.py / make_checklist.py 的既有约定一致。
    """
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    cand = "%s_%s%s" % (stem, stamp, ext)
    n = 1
    while os.path.exists(cand):
        cand = "%s_%s-%d%s" % (stem, stamp, n, ext)
        n += 1
    return cand


def in_root(path, root):
    """`path` 是否落在 `root` 之内（含 `root` 自身）。用于路径基准校验。"""
    try:
        a = os.path.abspath(path)
        b = os.path.abspath(root)
        return os.path.commonpath([a, b]) == b
    except (ValueError, OSError):
        return False


def safe_write(path, text, overwrite=False, root=None, binary=False):
    """
    统一产物写入。返回**最终**绝对路径（可能因防覆盖而改了名字）。

    参数
    ----
      overwrite : True 才允许覆盖同名文件；默认 False（改存带时间戳的新名）
      root      : 给了就做路径基准校验，越界抛 ValueError（安全性质，不可绕过）
      binary    : True 时 `text` 按 bytes 写（截图等）

    失败一律抛异常（不吞），调用方自己决定怎么告诉用户。
    """
    path = os.path.abspath(
        path.decode("utf-8") if isinstance(path, bytes) else path)
    if root is not None and not in_root(path, root):
        raise ValueError("拒绝写入基准目录之外：%s（基准 %s）"
                         % (path, os.path.abspath(root)))
    dest = path if overwrite else unique_path(path)
    ensure_dir(dest)
    # 原子替换：临时文件与目标同目录（跨盘替换不原子）
    fd, tmp = tempfile.mkstemp(prefix=".ows-w-", dir=os.path.dirname(dest))
    os.close(fd)
    try:
        with open(tmp, "wb" if binary else "w",
                  **({} if binary else {"encoding": "utf-8", "newline": ""})) as f:
            f.write(text)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return dest


def selftest():
    """core 的自测：只覆盖纯函数，且**不写仓库**（产物落系统临时目录）。"""
    ok, bad = [0], [0]

    def check(name, cond, extra=""):
        if cond:
            ok[0] += 1
            print("  [OK] %s" % name)
        else:
            bad[0] += 1
            print("  [FAIL] %s %s" % (name, extra))

    # ---- 规则库 ----
    fs = load_rules()
    idx = build_index(fs)
    check("① 规则库可读且有字段", len(fs) > 0, len(fs))
    check("② 标签索引非空", len(idx) > 0, len(idx))
    check("③ 字段键唯一", len({f["键"] for f in fs}) == len(fs))
    check("④ 归一化去星号/括号/空白",
          norm("姓名（中文）*") == "姓名" and norm("Full  Name") == "fullname")
    check("⑤ 泛化短标签拒写", map_one("日期", idx, fs)[0] is None)
    check("⑥ 值域别名可归一", canonical_value("大学本科") == "本科")
    check("⑦ 选项匹配走别名", match_option("本科", ["大学本科"]) == "大学本科")
    check("⑧ 优先级排序：越小越先", priority_rank("姓名") <= priority_rank("__不存在__"))

    # ---- URL 门禁 ----
    check("⑨ 公网 http 放行", url_gate("https://job.example.com/x")[0] is True)
    check("⑩ file:// 拒绝", url_gate("file:///C:/x")[0] is False)
    check("⑪ 内网 IP 拒绝", url_gate("http://192.168.1.1/")[0] is False)

    # ---- safe_write ----
    import shutil
    tmp = tempfile.mkdtemp(prefix="core_sw_")
    try:
        p1 = safe_write(os.path.join(tmp, "a.txt"), "第一版")
        check("⑫ safe_write 落盘且内容正确",
              os.path.isfile(p1) and open(p1, encoding="utf-8").read() == "第一版")
        p2 = safe_write(os.path.join(tmp, "a.txt"), "第二版")
        check("⑬ 默认不覆盖：换名另存",
              p2 != p1 and os.path.isfile(p2)
              and open(p1, encoding="utf-8").read() == "第一版")
        p3 = safe_write(os.path.join(tmp, "a.txt"), "第三版", overwrite=True)
        check("⑭ 显式 overwrite 才覆盖",
              p3 == os.path.abspath(os.path.join(tmp, "a.txt"))
              and open(p3, encoding="utf-8").read() == "第三版")
        try:
            safe_write(os.path.join(tmp, "b.txt"), "x", root=os.path.join(tmp, "sub"))
            inside = False
        except ValueError:
            inside = True
        check("⑮ 越过基准目录被拒", inside)
        check("⑯ in_root 判据正确",
              in_root(os.path.join(tmp, "s", "c.txt"), tmp)
              and not in_root(os.path.dirname(tmp) + "_other", tmp))
        # 原子性：临时文件不得残留
        check("⑰ 无临时文件残留",
              not [x for x in os.listdir(tmp) if x.startswith(".ows-w-")],
              os.listdir(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n自测结果：%d 通过 / %d 失败" % (ok[0], bad[0]))
    return 0 if bad[0] == 0 else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
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
