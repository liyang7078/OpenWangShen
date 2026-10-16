#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
start_browser.py · 开一个「可被接管」的浏览器
=============================================
为什么需要它
------------
真实网申站**几乎全部要登录**。裸开一个无头浏览器 → 页面直接跳登录页 →
一个字段都扫不到。正确的做法是：

    ① 用本脚本开一个带调试端口的浏览器
    ② 你**自己**在这个浏览器里登录、并走到「填写简历」那一页
    ③ 再让 fill_form 接管这个浏览器（复用登录态）

```bash
python scripts/start_browser.py            # ① 开浏览器（默认端口 9222）
#   → 在弹出的窗口里登录，走到要填的页面
python scripts/wangshen.py fill --profile profile.json \\
    --url "https://某公司岗位页" --connect 9222      # ③ 接管填写
```

配置档落在哪（2026-10-16 改）
------------------------------
以前落在**本仓库内**的 `.browser_profile/`。现在改到**机器缓存**：

    ~/.resume-autofill-agent/browser-profile        （可用 OPENWANGSHEN_HOME 换根）

为什么必须搬：这里头装的是**各站登录 cookie**。它是「这台机器上的你」，
不是「这个项目的产物」——放在仓库里，一次换目录克隆、一次清理归档就把它当垃圾带走。
改落点后**旧目录不会自动搬**；检测到就提示你加 `--migrate`（见下）。

红线
----
- 本脚本**不碰任何账号密码**：登录完全由你亲手在浏览器窗口里完成。
- 它只做一件事：带 `--remote-debugging-port` 启动浏览器进程。
- **不替你关窗口**：用完自己关。

许可：MIT
"""

import argparse
import base64
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# ------------------------------------------------------------------ 落点
APP_HOME_ENV = "OPENWANGSHEN_HOME"          # 覆盖缓存根（多份隔离的登录态 / 测试用）
CACHE_DIR_NAME = ".resume-autofill-agent"   # 缓存根目录名
PROFILE_SUBDIR = "browser-profile"          # 缓存根下的配置档子目录
LEGACY_DIR = os.path.join(ROOT, ".browser_profile")   # 旧落点：只用于**检测**与 --migrate
DEFAULT_PORT = 9222
WAIT_SECONDS = 15.0

# 回环地址**两个都探**（IPv4 / IPv6）——
#   实测同一台 Windows 上，Edge 有时把调试端口绑在 `127.0.0.1`、有时绑在 `[::1]`。
#   只探一个会把**明明活着**的浏览器读成「没起来」；而被误判的代价不是"多试一次"：
#   它触发**重复 launch**，Chromium 把新进程的启动参数转交给已在跑的实例
#   ⇒ 窗口里多出一个 about:blank，调试端口照样没开。
LOOPBACKS = ("127.0.0.1", "[::1]")

# 命令行里 `--user-data-dir` 的值（带引号 / 不带引号两种写法都认）
_UDD_RE = re.compile(r'--user-data-dir(?:=|\s+)(?:"([^"]*)"|(\S+))', re.I)

# PowerShell 用的环境变量名：**环境块是 Unicode 通道，不经控制台代码页** ——
#   中文用户名（C:\\Users\\<中文>\\…）走管道会乱码，而乱码的后果是**静默匹配失败**，
#   那正是本模块要根治的那类故障。
_ENUM_ENV = "OPENWANGSHEN_RECOVER_TARGET"

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ================================================================== 落点解析
def app_home():
    """机器缓存根。登录态是**机器资产**，不随仓库走。

    `OPENWANGSHEN_HOME` 可覆盖。**绝不硬编码盘符** —— 换台机器照样得能用。
    """
    v = (os.environ.get(APP_HOME_ENV) or "").strip()
    if v:
        return os.path.abspath(os.path.expanduser(v))
    return os.path.join(os.path.expanduser("~"), CACHE_DIR_NAME)


def default_profile_dir():
    return os.path.join(app_home(), PROFILE_SUBDIR)


# ================================================================== 端口探测
def probe(port, timeout=1.0):
    """探测调试端口。返回 `(在跑?, 版本信息, 命中的回环地址)`。"""
    for host in LOOPBACKS:
        url = "http://%s:%d/json/version" % (host, port)
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return True, json.load(r), host
        except (urllib.error.URLError, OSError, ValueError):
            continue
    return False, {}, ""


# ================================================================== 浏览器候选
def candidates():
    """按平台列出可能的浏览器可执行文件。返回 [(名字, 路径)]。"""
    sysname = platform.system()
    out = []
    if sysname == "Windows":
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        local = os.environ.get("LOCALAPPDATA", "")
        for name, paths in (
            ("Edge", [os.path.join(pf86, r"Microsoft\Edge\Application\msedge.exe"),
                      os.path.join(pf, r"Microsoft\Edge\Application\msedge.exe")]),
            ("Chrome", [os.path.join(pf, r"Google\Chrome\Application\chrome.exe"),
                        os.path.join(pf86, r"Google\Chrome\Application\chrome.exe"),
                        os.path.join(local, r"Google\Chrome\Application\chrome.exe")]),
        ):
            for p in paths:
                if p and os.path.isfile(p):
                    out.append((name, p))
    elif sysname == "Darwin":
        for name, paths in (
            ("Chrome", ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]),
            ("Edge", ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]),
            ("Chromium", ["/Applications/Chromium.app/Contents/MacOS/Chromium"]),
        ):
            for p in paths:
                if os.path.isfile(p):
                    out.append((name, p))
    else:
        for name, cmd in (("Chrome", "google-chrome"),
                          ("Chromium", "chromium"),
                          ("Chromium", "chromium-browser"),
                          ("Edge", "microsoft-edge")):
            p = shutil.which(cmd)
            if p:
                out.append((name, p))
    return out


def _chromium_names():
    """本机认得的 Chromium 系**映像名**（小写）。只有它们才可能被结束。"""
    return {os.path.basename(p).lower() for _n, p in candidates()}


# ================================================================== 残留实例识别
# 判据整体移植自 LYSKILL-apply `start_browser.py::leftover_holders`（2026-10-09 定）。
# 移植的是**判据**不是代码：那边 89985 字节里多数是本项目专属逻辑。
def _norm_path(path):
    """路径归一化后比较（大小写不敏感、统一分隔符、去尾部斜杠）。"""
    if not path:
        return ""
    try:
        return os.path.normcase(os.path.normpath(os.path.abspath(path)))
    except Exception:                                        # noqa: BLE001
        return os.path.normcase(str(path).replace("/", "\\").rstrip("\\"))


def _udd_of(cmdline):
    """从命令行里取 `--user-data-dir` 的值。取不到返回 None。"""
    m = _UDD_RE.search(cmdline or "")
    if not m:
        return None
    return m.group(1) if m.group(1) is not None else m.group(2)


def _parse_enum_lines(raw):
    """把 PS 回传的 `PID|映像名|main/child` 逐行解析成四元组。

    **抽成纯函数是为了可离线自测** —— 解析规则（尤其「第三个字段是子进程标记」）
    一旦改坏，判据会**静默失效**，所以它必须有一个确定性的执行点，
    而不是只能靠真机碰运气。
    """
    out = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if line.count("|") < 2:
            continue
        pid_s, name, kind = line.split("|", 2)
        if pid_s.strip().isdigit():
            out.append((int(pid_s), name.strip().lower(), None,
                        kind.strip().lower() == "child"))
    return out


# PowerShell 只回 `PID|映像名|main/child`（纯 ASCII），**路径比较在 PS 侧完成**。
# 为什么不把命令行捞回 Python 比：中文用户名经管道会乱码 ⇒ 静默匹配失败。
_PS_ENUM = (
    "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
    "$p=[IO.Path]::GetFullPath($env:" + _ENUM_ENV + ");"
    "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine } | ForEach-Object {"
    "$c=$_.CommandLine;"
    "$i=$c.IndexOf('--user-data-dir');"
    "if ($i -ge 0) { $r=$c.Substring($i+15).TrimStart('=').TrimStart();"
    "if ($r.StartsWith('\"')) { $q=$r.IndexOf('\"',1); if ($q -gt 0) { $v=$r.Substring(1,$q-1) } else { $v=$r.Substring(1) } }"
    "else { $v=($r -split '\\s+')[0] }"
    "try { $v=[IO.Path]::GetFullPath($v) } catch { $v='' }"
    "if ($v -and $v -eq $p) {"
    "$k='main'; if ($c.IndexOf('--type=') -ge 0) { $k='child' };"
    "\"$($_.ProcessId)|$($_.Name)|$k\" }"
    "} }"
)


def _ps_encoded(script):
    """PowerShell `-EncodedCommand`（UTF-16LE + base64）。

    为什么不用 `-Command`：脚本里含引号与 `$`，经 subprocess 列表在 Windows 上会被
    二次转义拆坏。编码通道没有转义问题。
    """
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def _enum_processes(target):
    """`[(pid, 映像名小写, 命令行或 None, 是否子进程)]`；**枚举不了返回 None —— 不假装知道**。

    命令行位为 `None` = 该通道**已经按路径筛过了**（Windows 在 PS 侧比较），
    调用方不必再比一次；这不是「缺数据」，两种含义不许混。
    「是否子进程」= 命令行里含 `--type=`（Chromium 的 renderer/gpu/utility/crashpad 都带）。
    """
    if os.name == "nt":
        env = dict(os.environ)
        env[_ENUM_ENV] = os.path.abspath(target)
        try:
            p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                                "-EncodedCommand", _ps_encoded(_PS_ENUM)],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               timeout=25, env=env,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if p.returncode == 0:
                return _parse_enum_lines(p.stdout.decode("utf-8", "replace"))
        except Exception:                                    # noqa: BLE001
            pass
        return None
    # POSIX：`ps -ww` 保证命令行不被截断（不加 -ww 会砍在 80 列，长参数直接丢）
    try:
        p = subprocess.run(["ps", "-ww", "-eo", "pid=,comm=,args="],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15)
        if p.returncode != 0:
            return None
        out = []
        for line in p.stdout.decode("utf-8", "replace").splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) == 3 and parts[0].isdigit():
                cmd = parts[2]
                out.append((int(parts[0]), os.path.basename(parts[1]).lower(), cmd,
                            "--type=" in cmd))
        return out
    except Exception:                                        # noqa: BLE001
        return None


def leftover_holders(probe_fn=None, target=None):
    """占用**本工具配置档**的**主进程**。返回 `(状态, 明细)`。

    状态：
      · `"none"`    没有
      · `"one"`     恰好一个（明细 = `[(pid, 名字)]`）→ 可以清
      · `"many"`    多个 → **拒绝动手**（不逐个猜）
      · `"unknown"` 枚举不了 → **拒绝动手**（fail-closed，不退回「按名字猜」）

    ⚠️ 只数**主进程**（命令行里没有 `--type=` 的）—— 一个实例会派生几十个带同款
    `--user-data-dir` 的子进程（LYSKILL 实机样本：46 行匹配行里 44 行是子进程），
    不滤会把「一个」误判成「几十个」。
    """
    probe_fn = probe_fn or _enum_processes
    tgt = _norm_path(target or default_profile_dir())
    if not tgt:
        return "unknown", []
    got = probe_fn(tgt)
    if got is None:
        return "unknown", []
    names = _chromium_names()
    hits = []
    for pid, name, cmdline, is_child in got:
        if is_child:
            continue                      # 子进程随主进程一起死，不计数
        if name not in names:
            continue                      # 非 Chromium 系映像不看
        if cmdline is None:
            hits.append((pid, name))      # 枚举通道已在它那一侧比过路径
            continue
        v = _udd_of(cmdline)
        if v and _norm_path(v) == tgt:
            hits.append((pid, name))
    if not hits:
        return "none", []
    if len(hits) > 1:
        return "many", hits
    return "one", hits


def _kill_pids(pids):
    """结束给定 PID（含子进程）。返回 `(是否全部成功, 失败明细)`。

    只在 `leftover_holders` 判为**恰好一个**、且路径**精确匹配**时才被调用。
    """
    failed = []
    for pid in pids:
        try:
            if os.name == "nt":
                p = subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   timeout=15,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if p.returncode != 0:
                    failed.append((pid, p.stdout.decode("utf-8", "replace").strip()[:80]))
            else:
                os.kill(int(pid), 15)     # SIGTERM
        except Exception as exc:                              # noqa: BLE001
            failed.append((pid, repr(exc)[:80]))
    return (not failed), failed


def recover_leftover(target=None, probe_fn=None, killer=None):
    """清掉占用配置档的残留实例。返回 `(是否已清干净, 人话, [pid…])`。

    **只在「端口探不到」的情况下才该被调用** —— 端口活着说明那个实例正是我们要用的，
    清掉它等于把你开着的登录窗口关掉。
    """
    killer = killer or _kill_pids
    state, detail = leftover_holders(probe_fn=probe_fn, target=target)
    if state == "none":
        return True, "没找到占用配置档的残留实例（多半是上次崩溃留下的空壳）。", []
    if state == "unknown":
        return False, ("查不到进程命令行（枚举通道不可用）—— **不猜、不动手**。"
                       "请手工确认并关掉占用该配置档的浏览器窗口。"), []
    if state == "many":
        return False, ("占用配置档的主进程有 %d 个，**不逐个猜**。"
                       "请手工只留一个、或全部关掉。" % len(detail)), [p for p, _n in detail]
    pid = detail[0][0]
    good, failed = killer([pid])
    if not good:
        return False, "试图结束 PID %d 失败：%s" % (pid, failed), [pid]
    return True, "已清掉残留实例（PID %d，%s）。" % (pid, detail[0][1]), [pid]


# ================================================================== 旧落点迁移
def migrate_legacy(dst=None, legacy=None):
    """把**旧落点**（仓库内 `.browser_profile/`）搬到机器缓存。返回 `(是否成功, 人话)`。

    **只在显式 `--migrate` 时执行，绝不自动搬。** 两个理由：
      · 那目录里是登录 cookie，属**机器资产**，不该由程序擅自挪动；
      · 它**可能正被一个开着的浏览器用着**，悄悄搬会把它拽空。
    新落点已有内容时**拒绝合并** —— 两套登录态混在一起只会互相打架。
    """
    dst = dst or default_profile_dir()
    legacy = legacy or LEGACY_DIR
    if not os.path.isdir(legacy):
        return False, "没找到旧落点（%s）—— 不用搬。" % legacy
    if os.path.abspath(legacy) == os.path.abspath(dst):
        return False, "新旧落点是同一个目录 —— 不用搬。"
    if os.path.isdir(dst) and os.listdir(dst):
        return False, ("新落点已有内容（%s），**不合并** —— 两套登录态混在一起会互相打架。"
                       "请自己确认后手工处理。" % dst)
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(legacy, dst)
    except Exception as e:                                   # noqa: BLE001
        return False, "搬运失败：%s" % e
    return True, "已把旧配置档搬到 %s（连同里面的登录态）。" % dst


# ================================================================== 主流程
def _report_holders(state, detail, target):
    if state == "none":
        print("[探测] 配置档 %s\n       没有进程占着它。" % target)
    elif state == "one":
        print("[探测] 配置档 %s\n       占用者（主进程）：PID %d（%s）" % (target, detail[0][0], detail[0][1]))
    elif state == "many":
        print("[探测] 配置档 %s\n       有 %d 个主进程占着它：" % (target, len(detail)))
        for pid, name in detail:
            print("         PID %d  %s" % (pid, name))
    else:
        print("[探测] 配置档 %s\n       查不到（进程枚举不可用）—— 不猜。" % target)


def main():
    ap = argparse.ArgumentParser(
        description="开一个带调试端口的浏览器（供 fill --connect 复用登录态）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="调试端口（默认 9222）")
    ap.add_argument("--profile-dir", default="",
                    help="配置档目录（默认落机器缓存：%s）" % default_profile_dir())
    ap.add_argument("--url", default="", help="启动时顺便打开这个网址")
    ap.add_argument("--check", action="store_true", help="只检查端口，不启动")
    ap.add_argument("--recover", action="store_true",
                    help="端口探不到时，自动清掉占用配置档的残留实例再起")
    ap.add_argument("--probe-leftover", action="store_true",
                    help="只探测谁占着配置档，不做任何动作（排障用）")
    ap.add_argument("--migrate", action="store_true",
                    help="把旧落点（仓库内 .browser_profile/）搬到机器缓存，再继续")
    ap.add_argument("--selftest", action="store_true", help="自测")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    target = args.profile_dir or default_profile_dir()

    # ---- --probe-leftover：纯只读，任何情况下都不动作 ----
    if args.probe_leftover:
        state, detail = leftover_holders(target=target)
        _report_holders(state, detail, target)
        return 0

    # ---- --migrate：显式搬运，随后照常启动 ----
    if args.migrate:
        ok, msg = migrate_legacy(dst=target)
        print(("[搬运] " if ok else "[跳过] ") + msg)
        if not ok and os.path.isdir(LEGACY_DIR) and os.listdir(LEGACY_DIR):
            return 2                    # 旧目录确实有东西却没搬成 → 停下来问，不硬起
        print()

    alive, info, host = probe(args.port)

    if args.check:
        if alive:
            print("[OK] 端口 %d 上已有可接管的浏览器：%s（回环 %s）"
                  % (args.port, info.get("Browser", "未知"), host))
            print("     接着跑：python scripts/wangshen.py fill --profile profile.json \\")
            print("                 --url <岗位页> --connect %d" % args.port)
            return 0
        print("[--] 端口 %d 上没有浏览器在跑。" % args.port)
        print("     先跑：python scripts/start_browser.py")
        return 1

    if alive:
        print("[提示] 端口 %d 已被占用，多半是你上次开的那个还在。直接用它即可：" % args.port)
        print("       python scripts/wangshen.py fill --profile profile.json \\")
        print("           --url <岗位页> --connect %d" % args.port)
        return 0

    # ---- 端口没通：先看是不是被残留实例占着配置档 ----
    state, detail = leftover_holders(target=target)
    if state == "one":
        if args.recover:
            ok, msg, _pids = recover_leftover(target=target)
            print("[清理] " + msg)
            if not ok:
                return 3
        else:
            print("[拦下] 配置档正被一个残留实例占着：PID %d（%s）" % detail[0])
            print("       两种走法：")
            print("         a) 让它自己清掉再起： python scripts/start_browser.py --recover")
            print("         b) 自己关掉那个窗口再来")
            print("       （不加 --recover 本脚本**不动手** —— 那可能正是你开着的登录窗口）")
            return 3
    elif state == "many":
        print("[拦下] 有 %d 个主进程占着配置档，**不逐个猜**：" % len(detail))
        for pid, name in detail:
            print("         PID %d  %s" % (pid, name))
        print("       请自己只留一个、或全部关掉再来。")
        return 3
    elif state == "unknown":
        print("[提示] 查不到进程列表（枚举通道不可用）。若下面起不来，"
              "多半是配置档被占 —— 关掉相关浏览器窗口再来。")

    found = candidates()
    if not found:
        print("[ERR] 没找到 Chrome / Edge / Chromium。")
        print("      请先装一个，或用 --profile-dir 手动指定一个位置。")
        return 2

    name, exe = found[0]
    if len(found) > 1:
        print("找到多个浏览器，用第一个：%s" % name)
        for n, p in found:
            print("    %-8s %s" % (n, p))

    os.makedirs(target, exist_ok=True)
    cmd = [exe,
           "--remote-debugging-port=%d" % args.port,
           "--user-data-dir=%s" % os.path.abspath(target),
           "--no-first-run",
           "--no-default-browser-check"]
    if args.url:
        cmd.append(args.url)

    print("[启动] %s" % name)
    print("       调试端口：%d" % args.port)
    print("       配置档：%s" % target)
    print("       （里面是各站登录 cookie，属**机器资产**：不随本目录走，也别提交到 Git）")
    try:
        subprocess.Popen(cmd)
    except OSError as e:
        print("[ERR] 启动失败：%s" % e)
        return 2

    # 等端口就绪（最多 15 秒）
    for _ in range(int(WAIT_SECONDS * 2)):
        time.sleep(0.5)
        alive, info, host = probe(args.port)
        if alive:
            print("\n[OK] 已就绪：%s（回环 %s）" % (info.get("Browser", name), host))
            print()
            print("接下来（顺序很重要）：")
            print("  1) 在弹出的浏览器窗口里**自己登录**，走到「填写简历」那一页")
            print("  2) 回到终端，跑：")
            print("     python scripts/wangshen.py fill --profile profile.json \\")
            print("         --url \"<那个页面的网址>\" --connect %d" % args.port)
            print()
            print("说明：本脚本不碰账号密码；程序也不会点提交；用完请自己关窗口。")
            return 0

    print("[WARN] 浏览器起来了，但 %d 秒内没等到调试端口。" % int(WAIT_SECONDS))
    print("       常见原因：该浏览器版本已禁用远程调试，或杀软拦了端口。")
    print("       可以试 --port 换一个端口；若怀疑配置档被占，加 --probe-leftover 看看。")
    return 1


# ================================================================== 自测
def selftest():
    import tempfile

    ok = [0]
    bad = [0]

    def check(name, cond, extra=""):
        if cond:
            ok[0] += 1
            print("  [OK] %s" % name)
        else:
            bad[0] += 1
            print("  [FAIL] %s %s" % (name, extra))

    # ---- 基础可用性 ----
    check("① 能找到至少一个浏览器（本机实测）", bool(candidates()),
          [n for n, _ in candidates()])
    check("② 端口探测不抛异常", isinstance(probe(1, timeout=0.3), tuple))
    check("③ 未监听时不误报为在跑", probe(1, timeout=0.3)[0] is False)
    check("④ 回环地址两个都探（IPv4/IPv6 都试）",
          list(LOOPBACKS) == ["127.0.0.1", "[::1]"], LOOPBACKS)

    # ---- 落点（2026-10-16 改：仓库内 → 机器缓存）----
    d = default_profile_dir()
    check("⑤ 默认配置档落**机器缓存**，不再落仓库内",
          os.path.abspath(d) != os.path.abspath(LEGACY_DIR), d)
    check("⑥ 落点在家目录下（**不硬编码盘符**）",
          os.path.abspath(d).startswith(os.path.abspath(os.path.expanduser("~"))), d)

    saved = os.environ.get(APP_HOME_ENV)
    os.environ[APP_HOME_ENV] = os.path.join(os.path.expanduser("~"), "_ows_test_home")
    try:
        want = os.path.join(os.path.abspath(os.path.join(os.path.expanduser("~"), "_ows_test_home")),
                            PROFILE_SUBDIR)
        check("⑦ OPENWANGSHEN_HOME 能覆盖缓存根",
              _norm_path(default_profile_dir()) == _norm_path(want), default_profile_dir())
    finally:
        if saved is None:
            os.environ.pop(APP_HOME_ENV, None)
        else:
            os.environ[APP_HOME_ENV] = saved

    # ---- 命令行解析（`--user-data-dir` 的两种写法）----
    check("⑧ 认得带引号写法",
          _udd_of('chrome.exe --user-data-dir="C:\\a b\\pf" --x') == "C:\\a b\\pf",
          _udd_of('chrome.exe --user-data-dir="C:\\a b\\pf" --x'))
    check("⑨ 认得等号无引号写法",
          _udd_of("chrome.exe --user-data-dir=/tmp/x --y") == "/tmp/x",
          _udd_of("chrome.exe --user-data-dir=/tmp/x --y"))
    check("⑩ 取不到就返回 None（不瞎猜）", _udd_of("chrome.exe --no-such-flag") is None)

    # ---- 枚举行解析（纯函数，必须可离线验）----
    check("⑪ 枚举行：main / child 分得开",
          _parse_enum_lines("11|msedge.exe|main\n22|msedge.exe|child")
          == [(11, "msedge.exe", None, False), (22, "msedge.exe", None, True)],
          _parse_enum_lines("11|msedge.exe|main\n22|msedge.exe|child"))
    check("⑫ 残缺行不混进来（否则会凭空多出一个「占用者」）",
          _parse_enum_lines("garbage\n33|x|main") == [(33, "x", None, False)],
          _parse_enum_lines("garbage\n33|x|main"))

    # ---- 残留识别：五档 + 两条边界 ----
    tgt = os.path.join(os.path.expanduser("~"), "_ows_t", PROFILE_SUBDIR)
    exe = os.path.basename((candidates() or [("Edge", "msedge.exe")])[0][1]).lower()
    good_cmd = '%s --user-data-dir="%s" --remote-debugging-port=9222' % (exe, tgt)
    child_cmd = '%s --user-data-dir="%s" --type=renderer' % (exe, tgt)

    def _rows(rows):
        return lambda _t: rows

    st, dt = leftover_holders(probe_fn=_rows([]), target=tgt)
    check("⑬ 没有占用 → none", st == "none", (st, dt))
    st, dt = leftover_holders(probe_fn=_rows([(11, exe, good_cmd, False)]), target=tgt)
    check("⑭ 恰好一个主进程 → one", st == "one" and dt == [(11, exe)], (st, dt))
    st, dt = leftover_holders(
        probe_fn=_rows([(11, exe, good_cmd, False), (22, exe, good_cmd, False)]), target=tgt)
    check("⑮ 多个主进程 → many（**拒绝动手**）", st == "many", (st, dt))
    st, dt = leftover_holders(probe_fn=_rows(None), target=tgt)
    check("⑯ 枚举不了 → unknown（fail-closed，不猜）", st == "unknown", (st, dt))
    st, dt = leftover_holders(probe_fn=_rows([(33, exe, child_cmd, True)]), target=tgt)
    check("⑰ **子进程不计数**（否则 1 个会被看成几十个）", st == "none", (st, dt))
    st, dt = leftover_holders(
        probe_fn=_rows([(44, "explorer.exe", good_cmd, False)]), target=tgt)
    check("⑱ 非 Chromium 系映像不看", st == "none", (st, dt))
    other = good_cmd.replace(tgt, tgt + "_other")
    st, dt = leftover_holders(probe_fn=_rows([(55, exe, other, False)]), target=tgt)
    check("⑲ 路径不**精确相等** → 不算（宁可漏判，不可误杀）", st == "none", (st, dt))

    # ---- 迁移：显式动作，且有反证 ----
    with tempfile.TemporaryDirectory() as td:
        old = os.path.join(td, "old")
        os.makedirs(old)
        open(os.path.join(old, "cookie"), "w").close()
        new = os.path.join(td, "new")
        ok1, msg1 = migrate_legacy(dst=new, legacy=old)
        check("⑳ 迁移：旧目录 → 新落点，内容跟着走",
              ok1 and os.path.isfile(os.path.join(new, "cookie")), msg1)

        old2 = os.path.join(td, "old2")
        os.makedirs(old2)
        open(os.path.join(old2, "cookie"), "w").close()
        new2 = os.path.join(td, "new2")
        os.makedirs(new2)
        open(os.path.join(new2, "keep"), "w").close()
        ok2, msg2 = migrate_legacy(dst=new2, legacy=old2)
        check("㉑ **反证**：新落点已有内容 → 拒绝合并，旧的不动",
              (not ok2) and ("不合并" in msg2) and os.path.isdir(old2), msg2)

        ok3, msg3 = migrate_legacy(dst=os.path.join(td, "new3"),
                                   legacy=os.path.join(td, "nope"))
        check("㉒ 没有旧目录 → 如实说「不用搬」，不假装成功",
              (not ok3) and ("不用搬" in msg3), msg3)

    print("\n自测结果：%d 通过 / %d 失败" % (ok[0], bad[0]))
    return 0 if bad[0] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
