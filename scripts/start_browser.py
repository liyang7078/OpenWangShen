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

红线
----
- 本脚本**不碰任何账号密码**：登录完全由你亲手在浏览器窗口里完成。
- 它只做一件事：带 `--remote-debugging-port` 启动浏览器进程。
- 用的是**独立配置文件目录** `.browser_profile/`（已 gitignore）。
  好处：不动你日常浏览器的配置；代价：第一次要登录一次，
  之后这个目录会记住登录态，下次不用再登。

许可：MIT
"""

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_DIR = os.path.join(ROOT, ".browser_profile")
DEFAULT_PORT = 9222

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


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


def probe(port, timeout=1.0):
    """探测调试端口是否已在监听。返回 (在跑?, 版本信息)。"""
    url = "http://127.0.0.1:%d/json/version" % port
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return True, json.load(r)
    except (urllib.error.URLError, OSError, ValueError):
        return False, {}


def main():
    ap = argparse.ArgumentParser(
        description="开一个带调试端口的浏览器（供 fill --connect 复用登录态）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="调试端口（默认 9222）")
    ap.add_argument("--profile-dir", default=DEFAULT_DIR,
                    help="独立的浏览器配置目录（默认 .browser_profile/，已 gitignore）")
    ap.add_argument("--url", default="", help="启动时顺便打开这个网址")
    ap.add_argument("--check", action="store_true", help="只检查端口，不启动")
    ap.add_argument("--selftest", action="store_true", help="自测")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    alive, info = probe(args.port)
    if args.check:
        if alive:
            print("[OK] 端口 %d 上已有可接管的浏览器：%s"
                  % (args.port, info.get("Browser", "未知")))
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

    found = candidates()
    if not found:
        print("[ERR] 没找到 Chrome / Edge / Chromium。")
        print("      请先装一个，或用 --profile 手动指定浏览器可执行文件路径。")
        return 2

    name, exe = found[0]
    if len(found) > 1:
        print("找到多个浏览器，用第一个：%s" % name)
        for n, p in found:
            print("    %-8s %s" % (n, p))

    os.makedirs(args.profile_dir, exist_ok=True)
    cmd = [exe,
           "--remote-debugging-port=%d" % args.port,
           "--user-data-dir=%s" % os.path.abspath(args.profile_dir),
           "--no-first-run",
           "--no-default-browser-check"]
    if args.url:
        cmd.append(args.url)

    print("[启动] %s" % name)
    print("       调试端口：%d" % args.port)
    print("       独立配置目录：%s（内含登录态，**绝不提交到 Git**）" % args.profile_dir)
    try:
        subprocess.Popen(cmd)
    except OSError as e:
        print("[ERR] 启动失败：%s" % e)
        return 2

    # 等端口就绪（最多 15 秒）
    for _ in range(30):
        time.sleep(0.5)
        alive, info = probe(args.port)
        if alive:
            print("\n[OK] 已就绪：%s" % info.get("Browser", name))
            print()
            print("接下来（顺序很重要）：")
            print("  1) 在弹出的浏览器窗口里**自己登录**，走到「填写简历」那一页")
            print("  2) 回到终端，跑：")
            print("     python scripts/wangshen.py fill --profile profile.json \\")
            print("         --url \"<那个页面的网址>\" --connect %d" % args.port)
            print()
            print("说明：本脚本不碰账号密码；程序也不会点提交。")
            return 0

    print("[WARN] 浏览器起来了，但 %d 秒内没等到调试端口。" % 15)
    print("       常见原因：该浏览器版本已禁用远程调试，或杀软拦了端口。")
    print("       可以试 --port 换一个端口。")
    return 1


def selftest():
    ok = [0]
    bad = [0]

    def check(name, cond, extra=""):
        if cond:
            ok[0] += 1
            print("  [OK] %s" % name)
        else:
            bad[0] += 1
            print("  [FAIL] %s %s" % (name, extra))

    check("① 能找到至少一个浏览器（本机实测）", bool(candidates()),
          [n for n, _ in candidates()])
    check("② 端口探测不抛异常", isinstance(probe(1, timeout=0.3), tuple))
    check("③ 未监听时不误报为在跑", probe(1, timeout=0.3)[0] is False)
    check("④ 独立配置目录在项目内且被 gitignore 覆盖",
          DEFAULT_DIR.startswith(ROOT))
    gi = os.path.join(ROOT, ".gitignore")
    covered = False
    if os.path.isfile(gi):
        with open(gi, encoding="utf-8") as f:
            txt = f.read()
        covered = ".browser_profile" in txt
    check("⑤ .gitignore 已屏蔽该目录（内含登录态 cookie）", covered)

    print("\n自测结果：%d 通过 / %d 失败" % (ok[0], bad[0]))
    return 0 if bad[0] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
