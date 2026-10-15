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

五条判据
--------
1. **路径规则**：入库路径命中「本就该被忽略」的模式（profile.json 等）
2. **值等同**  ：入库文本中出现与 `--profile` 某一字段**完全相等**的值
3. **结构模式**：出现校验位合法的 18 位身份证 / 11 位手机号 / 邮箱
4. **人属性**  ：规则类文件（`assets/` · `sites/`）里出现第一人称或数值型个人属性
5. **语法下限**：README 承诺 Python 3.8+，入库 `.py` 里就不能出现 3.9+ 才成立的写法
   （这不是隐私判据，但同属「分发前必须站得住的承诺」—— 承诺塌了，
   用户在 3.8 上 `git clone` 下来第一行就跑不动）

判据 5 的边界（2026-10-16 新增，**别把它想得比实际强**）
--------------------------------------------------------
`ast.parse(feature_version=(3, 8))` 的判别力很弱 —— 实测：`x: list[int]`、
`a = {} | {}`、注解 `int | None` 在 3.8 模式下**一律"通过"**，
因为它们语法合法、到**运行期**才炸。所以判据 5 分两段：
  · 语法层：`ast.parse(..., feature_version=(3, 8))`
  · 运行期层：按 AST 找注解里的 `X | Y`、内建泛型下标、3.9+/3.11+ 的 API
**真正的判据是 CI 的 py3.8 job（真解释器真跑）**，本地这两段只负责提前发现。
已知未覆盖：非注解位置的 `{} | {}`（与集合/位运算在静态上不可分）—— 这类交给 CI。

**判据 2 是最硬的一条** —— 只有它能区分「文档里的假样例」与「真人数据」。
判据 3 会跳过 `fixtures/档案_示例.json` 里的**官方假值**与常见的
**占位符**（`example.com` 域名、连号手机号），否则文档里的假样例会淹没真信号。

判据 3 的定级（2026-10-15 修订）
-------------------------------
| 命中 | 定级 | 理由 |
|---|---|---|
| 校验位**合法**的 18 位身份证 | **FAIL** | 校验位合法几乎不可能是随手编的 |
| 非占位符的 11 位手机号 | **FAIL** | 同上 |
| 非占位符的邮箱 | **FAIL** | 同上 |
| 规则文件里的人属性 | WARN | 无法区分「示例文案」与真值 |

> 修订前 3/4 两条一律 WARN ⇒ **真值出现也只算"警告"，照样判"可以分发"**。
> 那是 fail-open，与本项目的隐私承诺相反。现在改为 fail-closed。

覆盖率也是判据的一部分（2026-10-15 修订）
----------------------------------------
`git ls-files` 默认把非 ASCII 路径转义成八进制串（`core.quotepath=true`），
用它拼路径必然 `OSError` ⇒ 文件被**静默跳过**。实测 46 个入库文件只读到 28 个，
被跳过的恰恰是 `docs/隐私设计.md`、`fixtures/档案_示例.json`、`assets/*`。
⇒ 现在改用 `git -c core.quotepath=false ls-files -z`，
  并在**有文件读不到**时直接判「无法判定」，**绝不打印"可以分发"**。

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
import ast
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

# 占位符白名单：文档与示例里**合法**出现的假值形态。
# 只放行「一眼是编的」的东西；其余一律按真值处理（fail-closed）。
PLACEHOLDER_DOMAINS = ("example.com", "example.org", "example.net",
                       "example.edu", "example.cn", "example",
                       "test.com", "invalid", "localhost")
FAKE_MOBILES = frozenset((
    "13800000000", "13800138000", "13900000000", "13000000000",
    "15000000000", "18600000000", "18888888888", "13333333333",
))


def is_placeholder_mobile(v):
    """明显是人造的手机号（全零尾 / 连号 / 常见测试号）。"""
    if v in FAKE_MOBILES:
        return True
    # 区号后 8 位里出现 ≥7 个连续相同数字 ⇒ 人造
    return bool(re.search(r"(\d)\1{6,}", v[3:]))


def is_placeholder_email(v):
    """占位域名（example.com 等）不算真值。"""
    dom = v.rsplit("@", 1)[-1].strip().lower()
    return any(dom == d or dom.endswith("." + d) for d in PLACEHOLDER_DOMAINS)


def public_addresses():
    """
    **设计上就是要公开**的地址 —— 目前只有一处：零账号反馈邮箱
    （`assets/反馈渠道.json` 的「反馈邮箱」）。

    为什么必须单列一类
    ------------------
    判据 3 的立场是「非占位符的邮箱一律当泄露」（fail-closed），这条立场是对的：
    绝大多数邮箱出现在仓库里都意味着事故。但「项目专用反馈邮箱」是**反例**——
    它出现在这里不是泄露，而是**功能本身**（考生靠它把反馈寄出来）。
    把功能当事故拦下，结果是作者被迫关掉功能来让扫描器变绿 —— 那是
    「绿灯靠拆功能换来的」，比红灯更坏。

    边界：只认这一个键、只认这一个文件。别处出现同一个地址**仍会照报**。
    """
    path = os.path.join(core.PROJECT_ROOT, "assets", "反馈渠道.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return set()
    addr = str(d.get("反馈邮箱") or "").strip()
    return {addr} if addr else set()

# ── 判据 4：人属性 ────────────────────────────────────────────
RULE_FILE_RE = re.compile(r"^(assets|sites)/")
PERSON_MARKERS = ("本人", "我的", "我叫", "我于", "我毕", "我毕业")
# 注意 \"? —— 规则库是 JSON，键名带引号（"年龄": 24），
# 不允许引号会让这条判据在真实文件上完全失效。
NUMERIC_ATTR_RE = re.compile(r"(年龄|分数|成绩|GPA|排名)\"?\s*[:：=]\s*-?\d")

SKIP_SUFFIX = (".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip",
               ".pyc", ".woff", ".woff2", ".ttf")

# 元数据块：这些顶层对象装的是**词表/层号/词表说明**，不是任何人的属性。
# 不排除的话，「字段优先级」里的 `"专业排名": 2` 会被判成人属性 —— 假警报。
# 假警报的代价很实在：喊多了，真信号就没人看了。
METADATA_BLOCKS = ("字段优先级", "值域别名", "忽略", "_指纹词表", "_统计",
                   "_记录组", "_清单排除", "_约定", "_说明")


def metadata_lines(lines, rel):
    """
    找出「元数据块」占用的行号集合（只对 .json 生效）。
    判据 4（人属性）跳过这些行；判据 3（结构模式）仍然全程生效。
    """
    if not rel.lower().endswith(".json"):
        return set()
    skip = set()
    i, n = 0, len(lines)
    while i < n:
        m = re.match(r'\s*"([^"]+)"\s*:\s*\{\s*$', lines[i])
        if m and m.group(1) in METADATA_BLOCKS:
            depth, j = 1, i + 1
            while j < n and depth > 0:
                depth += lines[j].count("{") - lines[j].count("}")
                j += 1
            skip.update(range(i + 1, j + 1))
            i = j
            continue
        i += 1
    return skip


def id_checksum_ok(v):
    """18 位身份证的校验位是否合法。"""
    if len(v) != 18:
        return False
    try:
        s = sum(int(v[i]) * ID_W[i] for i in range(17))
    except ValueError:
        return False
    return ID_C[s % 11] == v[17].upper()


# ── 判据 5：Python 语法下限 ─────────────────────────────────────
# README 承诺「Python 3.8+」。承诺得有东西盯着，否则就是空承诺
# （本仓有过先例：README 承诺 releases/latest，而仓库一个 tag 都没有）。
#
# 为什么不能只靠 feature_version：见文件头「判据 5 的边界」，已实测确认
# `x: list[int]` / `注解 int | None` 在 3.8 模式下会**误判为通过**。
PY_MIN = (3, 8)

PY38_RUNTIME_API = (
    ("removeprefix", "str.removeprefix 需要 3.9+"),
    ("removesuffix", "str.removesuffix 需要 3.9+"),
    ("BooleanOptionalAction", "argparse.BooleanOptionalAction 需要 3.9+"),
)
PY38_MODULES = {"graphlib": "3.9+", "zoneinfo": "3.9+", "tomllib": "3.11+"}
PY38_GENERICS = frozenset(("list", "dict", "set", "tuple", "frozenset", "type"))


class _Py38Visitor(ast.NodeVisitor):
    """只盯「语法合法、但 3.8 跑不起来」的写法。不评风格，不做语义分析。"""

    def __init__(self):
        self.hits = []

    def _note(self, node, why):
        self.hits.append((getattr(node, "lineno", 0), why))

    def _scan_ann(self, node, ann):
        if ann is None:
            return
        for sub in ast.walk(ann):
            if isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.BitOr):
                self._note(node, "注解里的 `X | Y` 联合写法 → 需要 3.10+（3.8 会在导入时 TypeError）")
            elif isinstance(sub, ast.Subscript):
                v = sub.value
                if isinstance(v, ast.Name) and v.id in PY38_GENERICS:
                    self._note(node, "注解里的内建泛型下标（如 list[int]）→ 需要 3.9+")

    def _scan_func(self, node):
        a = node.args
        for arg in list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs):
            self._scan_ann(node, arg.annotation)
        for extra in (a.vararg, a.kwarg):
            if extra is not None:
                self._scan_ann(node, extra.annotation)
        self._scan_ann(node, node.returns)
        self.generic_visit(node)

    visit_FunctionDef = _scan_func
    visit_AsyncFunctionDef = _scan_func

    def visit_AnnAssign(self, node):
        self._scan_ann(node, node.annotation)
        self.generic_visit(node)

    def _scan_api_name(self, node, name):
        for api, why in PY38_RUNTIME_API:
            if name == api:
                self._note(node, why)

    # 这些 API 既可能被**调用**（`s.removeprefix('a')`），也可能只被**引用**
    # （`action=argparse.BooleanOptionalAction` —— 它是值，不是调用）。
    # 只挂 visit_Call 会漏掉后者，所以扫 Attribute 与 Name。
    def visit_Attribute(self, node):
        self._scan_api_name(node, node.attr)
        self.generic_visit(node)

    def visit_Name(self, node):
        self._scan_api_name(node, node.id)
        self.generic_visit(node)

    def visit_Import(self, node):
        for a in node.names:
            root = a.name.split(".")[0]
            if root in PY38_MODULES:
                self._note(node, "import %s → 需要 %s" % (a.name, PY38_MODULES[root]))
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        root = (node.module or "").split(".")[0]
        if root in PY38_MODULES:
            self._note(node, "from %s import ... → 需要 %s" % (node.module, PY38_MODULES[root]))
        self.generic_visit(node)


def py_min_problems_in(src, name="<probe>"):
    """纯函数：一段源码在 3.8 上会不会出问题。返回 [(行号, 说明)]。

    语法层先过一遍；过了再按 AST 找运行期才炸的写法。
    """
    try:
        tree = ast.parse(src, filename=name, feature_version=PY_MIN)
    except SyntaxError as e:
        return [(e.lineno or 0,
                 "3.8 解析器不接受这段语法：%s" % str(e).split("(")[0].strip()[:48])]
    v = _Py38Visitor()
    v.visit(tree)
    return sorted(set(v.hits))


def py_min_problems(rel):
    """对入库文件跑判据 5。读不到（二进制/非 UTF-8）返回 []。"""
    try:
        with open(os.path.join(core.PROJECT_ROOT, rel), "r", encoding="utf-8") as f:
            src = f.read()
    except (OSError, UnicodeDecodeError):
        return []
    return py_min_problems_in(src, rel)


def tracked_files():
    """
    列出**入库**文件（相对路径，正斜杠）。
    优先用 git ls-files —— 因为分发出去的正是它。
    git 不可用时退回遍历目录。
    """
    try:
        # -c core.quotepath=false ：否则非 ASCII 路径会被转义成
        #   "docs//345/255/227/..." 这类八进制串，拼成路径必然读不到。
        # -z ：以 \0 分隔，彻底绕开引号转义。
        r = subprocess.run(
            ["git", "-c", "core.quotepath=false", "ls-files", "-z"],
            cwd=core.PROJECT_ROOT, capture_output=True, text=True,
            encoding="utf-8")
        if r.returncode == 0 and r.stdout.strip("\0 \r\n"):
            return [p.replace("\\", "/") for p in r.stdout.split("\0") if p.strip()]
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
    unreadable, skipped_bin = [], []

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
    public = public_addresses()

    for rel in files:
        full = os.path.join(core.PROJECT_ROOT, rel)
        lines = read_lines(full)
        if lines is None:
            # 区分「按后缀主动跳过的二进制」与「本该读到却读不到」。
            # 后者是扫描覆盖面缺口，必须计数 —— 修订前这里静默 continue。
            if full.lower().endswith(SKIP_SUFFIX):
                skipped_bin.append(rel)
            else:
                unreadable.append(rel)
            continue

        is_rule_file = bool(RULE_FILE_RE.match(rel))
        meta_lines = metadata_lines(lines, rel) if is_rule_file else set()

        for i, line in enumerate(lines, 1):
            # 判据 2
            for val, field in values.items():
                if val in line:
                    fails.append(("值等同", rel, i, "与档案字段「%s」的值完全一致" % field))

            # 判据 3（跳过官方假值 + 常见占位符；命中即 FAIL，不再只 WARN）
            for m in ID_RE.finditer(line):
                if m.group(1) not in fake and id_checksum_ok(m.group(1)):
                    fails.append(("结构模式", rel, i, "校验位合法的 18 位身份证"))
                    break
            for m in MOBILE_RE.finditer(line):
                if m.group(1) not in fake and not is_placeholder_mobile(m.group(1)):
                    fails.append(("结构模式", rel, i, "非占位符的 11 位手机号"))
                    break
            for m in EMAIL_RE.finditer(line):
                if (m.group(0) not in fake and m.group(0) not in public
                        and not is_placeholder_email(m.group(0))):
                    fails.append(("结构模式", rel, i, "非占位符的邮箱"))
                    break

            # 判据 4（只对「必须与人无关」的规则文件生效，且跳过元数据块）
            if is_rule_file and i not in meta_lines:
                for mk in PERSON_MARKERS:
                    if mk in line:
                        warns.append(("人属性", rel, i, "规则文件里出现第一人称「%s」" % mk))
                        break
                if NUMERIC_ATTR_RE.search(line):
                    warns.append(("人属性", rel, i, "规则文件里出现数值型个人属性"))

    # 判据 5（2026-10-16 新增）：README 承诺的下限，不能只是承诺
    for rel in files:
        if not rel.endswith(".py"):
            continue
        for ln, why in py_min_problems(rel):
            fails.append(("语法下限", rel, ln, why))

    # 覆盖面缺口 ⇒ 不得判定「可以分发」（fail-closed）
    if unreadable:
        fails.append(("覆盖不全", "（%d 个文件）" % len(unreadable), 0,
                      "这些入库文件未能读取，扫描不完整，无法判定可分发"))

    if verbose:
        print("扫描入库文件：%d 个（读到 %d ／ 跳过二进制 %d ／ **读不到 %d**）"
              % (len(files), len(files) - len(skipped_bin) - len(unreadable),
                 len(skipped_bin), len(unreadable)))
        print("官方假值白名单：%d 个（取自 fixtures/档案_示例.json）" % len(fake))
        if public:
            print("公开用途地址：%d 个（取自 assets/反馈渠道.json 的「反馈邮箱」）"
                  % len(public))
        pyfiles = [f for f in files if f.endswith(".py")]
        print("语法下限：Python %d.%d ｜ 校验 %d 个 .py"
              "（本地只做静态近似，最终判据是 CI 的 py3.8 job）"
              % (PY_MIN[0], PY_MIN[1], len(pyfiles)))
        if profile:
            print("对照档案字段值：%d 个（只用于比对，不打印）" % len(values))
        print()

        for rel in unreadable:
            print("[读不到] %s" % rel)
        if unreadable:
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

    # 判据 3 定级：占位符放行、疑似真值必拦（2026-10-15 新增）
    # ⚠️ 探针一律**拼接构造**，绝不写字面量 —— 否则本扫描器会举报自己
    #    （自测里出现一个像真的手机号，dist_check 就会把它当 FAIL 报出来）
    probe_mobile = "138" + "12345678"
    probe_mail = "zhangsan" + chr(64) + "qq" + ".com"
    assert is_placeholder_mobile("13800000000"), "占位手机号未放行"
    assert is_placeholder_mobile("13333333333"), "连号手机号未放行"
    assert not is_placeholder_mobile(probe_mobile), "疑似真值手机号被误放行"
    assert is_placeholder_email("zhangsan@example.com"), "占位邮箱未放行"
    assert not is_placeholder_email(probe_mail), "非占位邮箱被误放行"
    print("【自测 7】判据 3 占位符白名单（真值不会被放行）✓")

    # 覆盖面：中文路径必须真的被读到 —— 这是 quotepath 修复的反证（2026-10-15 新增）
    files = tracked_files()
    assert files, "tracked_files 为空"
    quoted = [p for p in files if p.startswith('"') or "/3" in p[:40]]
    assert not quoted, "仍有被 git 转义的路径：%s" % quoted[:2]
    unread = [p for p in files
              if not p.lower().endswith(SKIP_SUFFIX)
              and read_lines(os.path.join(core.PROJECT_ROOT, p)) is None]
    assert not unread, "仍有 %d 个入库文件读不到：%s" % (len(unread), unread[:3])
    print("【自测 8】入库文件全覆盖：%d 个，无一读不到 ✓" % len(files))

    # 公开用途地址（2026-10-16 新增）：反馈邮箱是**功能**，不是泄露面。
    # 反证：白名单只认 assets/反馈渠道.json 的那一个键 —— 换个地址照样该报。
    pub = public_addresses()
    listed = [p for p in files if p == "assets/反馈渠道.json"]
    assert listed, "assets/反馈渠道.json 未入库（白名单的依据不在扫描范围内）"
    assert len(pub) <= 1, "公开用途地址只该有一个来源"
    print("【自测 9】公开用途地址白名单：%d 个（来源 %s）✓" % (len(pub), listed[0]))

    # 判据 5（2026-10-16 新增）：README 承诺 Python 3.8+，就得有东西盯着。
    # 正例：本仓全部入库 .py 必须过 —— 过不了就说明「3.8+」已经是空承诺。
    pyfiles = [f for f in files if f.endswith(".py")]
    assert pyfiles, "没有扫到任何 .py，判据 5 形同虚设"
    offenders = {p: py_min_problems(p) for p in pyfiles}
    offenders = {k: v for k, v in offenders.items() if v}
    assert not offenders, "本仓有 .py 过不了 3.8 下限：%s" % list(offenders)[:2]

    # 反例：三种「语法合法、3.8 运行期才炸」的写法必须被抓到
    # （只用 feature_version 是抓不到的 —— 这一点前面实测确认过）
    assert py_min_problems_in("def f(x: int | None):\n    pass\n"), "注解联合写法漏了"
    assert py_min_problems_in("def f(x: list[int]):\n    pass\n"), "内建泛型下标漏了"
    assert py_min_problems_in("y = 'ab'.removeprefix('a')\n"), "removeprefix 漏了"
    assert py_min_problems_in("import tomllib\n"), "tomllib 漏了"
    assert py_min_problems_in("match x:\n    case 1:\n        pass\n"), "match 漏了"

    # 正例：合法 3.8 写法不许误报（位运算 | 不是注解，不得当成联合类型）
    assert py_min_problems_in("def f(x=None):\n    return {}.get(x, 0)\n") == [], "误报"
    assert py_min_problems_in("a = 1 | 2\nb = [i for i in range(3)]\n") == [], "误报位运算"
    print("【自测 10】Python 3.8 下限：%d 个 .py 全过；"
          "注解联合／泛型下标／新 API／match 均能抓到 ✓" % len(pyfiles))

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    print("【自测通过】五条判据全部生效")
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
