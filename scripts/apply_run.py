#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
apply_run.py · 一条命令跑完「起浏览器 → 探通 → 接管 → 填表」
=============================================================

为什么必须是一条命令（而不是分两步）
------------------------------------
浏览器进程活在**启动它的那个命令行会话**里。分两步跑：

    第一步（起浏览器）的会话一结束 → 浏览器随会话被回收 → 第二步接管时端口已经没了。

而且这时的现象**特别有欺骗性**：第一步明明打印了「已就绪」，第二步却报「连不上」——
于是人会掉进「读源码 / 写探针 / 复现根因」的黑洞（LYSKILL 实测有考生在这上面耗掉
22 分 41 秒）。把两个动作压进**同一条命令**，这个坑从结构上就不存在了。

顺序固定，且中途不许人工插队：

    ① 参数体检（--check 可单跑）
    ② **环境能力预检** —— 先探能力，再起窗口（否则你会盯着一个注定没用的窗口）
    ③ 起浏览器（start_browser.py，含 --recover 自愈）
    ④ 接管填表（fill_form.py --connect）

退出码
------
    0 = 已填完并停在提交前；2 = 参数错（**在起浏览器之前**就报）；
    3 = 浏览器起不来；4 = 缺 playwright（已自动降级提示保底路径）。

许可：MIT
"""

import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import start_browser as sb                                # noqa: E402

PY = sys.executable
START_BROWSER = os.path.join(HERE, "start_browser.py")
FILL_FORM = os.path.join(HERE, "fill_form.py")

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ================================================================== 纯函数（可离线自测）
def will_fill(prepare_only=False, check=False):
    """这次会不会**真的填表**？（决定要不要校验填表参数）

    `--check` 是「什么都还没准备时」跑的体检，`--prepare-only` 是「只起浏览器让你
    从容登录」—— 这两种都不该被要求提供 url / 档案。要让它们也得先备齐参数，
    体检就从"排障第一步"变成了"排障第一步之前再加一步"，正好把门槛立错地方。
    """
    return (not prepare_only) and (not check)


def check_args(url="", profile="", need_fill=True):
    """参数体检。返回**问题清单**（空 = 通过）。

    抽成纯函数：这些判据要在**起浏览器之前**就拦住，而"起浏览器之前"是最难在
    集成测试里稳定复现的位置 —— 只能靠一个确定性的执行点盯着它。
    """
    problems = []
    if need_fill:
        if not url:
            problems.append("缺 --url（要填的那张网申页的地址）")
        if not profile:
            problems.append("缺 --profile（你的档案 JSON）")
        elif not os.path.isfile(profile):
            problems.append("档案文件不存在：%s" % profile)
    return problems


def build_prep_argv(port=9222, profile_dir="", url="", recover=False, python=""):
    """拼 `start_browser.py` 的命令行。**纯函数** —— 便于离线断言，不必真起浏览器。"""
    argv = [python or PY, START_BROWSER, "--port", str(int(port))]
    if profile_dir:
        argv += ["--profile-dir", profile_dir]
    if recover:
        argv.append("--recover")
    if url:
        argv += ["--url", url]
    return argv


def build_fill_argv(profile="", url="", port=9222, dry_run=False, screenshot="",
                    out="", budget=0, timeout=0, headed=False, python=""):
    """拼 `fill_form.py` 的命令行。**纯函数**。"""
    argv = [python or PY, FILL_FORM,
            "--profile", profile,
            "--url", url,
            "--connect", "http://127.0.0.1:%d" % int(port)]
    if dry_run:
        argv.append("--dry-run")
    if headed:
        argv.append("--headed")
    if screenshot:
        argv += ["--screenshot", screenshot]
    if out:
        argv += ["--out", out]
    if budget:
        argv += ["--budget", str(int(budget))]
    if timeout:
        argv += ["--timeout", str(int(timeout))]
    return argv


# ================================================================== 环境能力预检
# 依次试：系统 Edge → 系统 Chrome → playwright 自带 chromium（None）
CHANNELS = ("msedge", "chrome", None)


def check_env(launcher=None):
    """探测「自动填表能力」。返回 `(是否可用, 一行原因)`。**不装、不改环境**。

    为什么必须**真起一次**才敢下结论
    --------------------------------
    只查 `p.chromium.executable_path` 会**误判**：本工具默认走系统 Edge
    （`fill_form.py` 的 `--channel` 默认就是 msedge），根本不依赖 playwright 自带那份
    chromium。只查自带路径，会把一台「系统 Edge 好好的」的机器报成「用不了」——
    实测就撞上了：本机端口上正跑着 Chrome/138，却被判「Chromium 还没下载」。

    真起一次（headless，秒级）是唯一不会骗人的判据。起不来才报不可用。
    """
    # ⚠ 注入点必须排在**最前面**（2026-10-16 修）。
    #   反例：把 `import playwright` 放在注入之前，则"注入了 launcher"这件事
    #   只在**装了 playwright 的机器上**才生效 ⇒ 自测结果随环境变，
    #   而 ⑭（注入不可用）会因为"导入失败也返回 False"**恰好通过** ——
    #   假绿。假绿的危害：它在真机上不报警、在别人机器上报警，最难查的一类。
    #   判据：注入优先 ⇒ 自测与「装没装 playwright」彻底解耦。
    if launcher is not None:
        return launcher()
    try:
        import playwright.sync_api  # noqa: F401
    except Exception as e:                                   # noqa: BLE001
        return False, ("没装 playwright（%s）。装法：pip install playwright" % type(e).__name__)
    from playwright.sync_api import sync_playwright
    errs = []
    for ch in CHANNELS:
        try:
            with sync_playwright() as p:
                kw = {"headless": True}
                if ch:
                    kw["channel"] = ch
                b = p.chromium.launch(**kw)
                ver = b.version
                b.close()
            return True, "playwright 可用（%s %s）" % (ch or "自带 chromium", ver)
        except Exception as e:                               # noqa: BLE001
            errs.append("%s：%s" % (ch or "chromium", str(e)[:60]))
    return False, "playwright 装了，但一个浏览器都起不来（%s）" % " ｜ ".join(errs)


def run(argv, runner=None):
    """跑子进程并把它的输出**原样透传**（用户要看那张报告）。返回退出码。"""
    if runner is not None:
        return runner(argv)
    return subprocess.call(argv)


# ================================================================== 主流程
def main(argv=None, preparer=None, runner=None, env_checker=None):
    ap = argparse.ArgumentParser(
        description="一条命令跑完「起浏览器 → 探通 → 接管 → 填表」（**不要拆成两条命令**）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="典型用法：\n"
               "  # 一条命令走完全链（拆开跑，浏览器会随上一个会话死掉）\n"
               "  python scripts/apply_run.py --url <岗位页> --profile 档案.json\n"
               "  # 上一条被残留实例拦下 → 加 --recover 让它自己清掉再跑\n"
               "  python scripts/apply_run.py --url <岗位页> --profile 档案.json --recover\n"
               "  # 只想先把浏览器起好（让你从容登录），不填表\n"
               "  python scripts/apply_run.py --prepare-only\n"
               "  # 先体检：解释器 / 能力 / 端口 / 配置档占用，一次全说清\n"
               "  python scripts/apply_run.py --check\n")
    ap.add_argument("--url", default="", help="岗位页 URL")
    ap.add_argument("--profile", default="", help="你的档案 JSON")
    ap.add_argument("--port", type=int, default=sb.DEFAULT_PORT,
                    help="调试端口（默认 %d）" % sb.DEFAULT_PORT)
    ap.add_argument("--profile-dir", default="", help="浏览器配置档目录（默认落机器缓存）")
    ap.add_argument("--recover", action="store_true",
                    help="端口不通且配置档被残留实例占着时，自动清掉再起")
    ap.add_argument("--dry-run", action="store_true", help="只扫描与映射，不写入")
    ap.add_argument("--screenshot", default="", help="填完后截图输出路径")
    ap.add_argument("--out", default="", help="填表报告 JSON 输出路径")
    ap.add_argument("--budget", type=int, default=0, help="单站填写预算秒数（0 = 用默认）")
    ap.add_argument("--timeout", type=int, default=0, help="单步超时毫秒（0 = 用默认）")
    ap.add_argument("--headed", action="store_true", help="接管时显示浏览器窗口")
    ap.add_argument("--prepare-only", action="store_true", help="只把浏览器起好，不填表")
    ap.add_argument("--check", action="store_true", help="只体检，不起浏览器")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    env_checker = env_checker or check_env

    # ---- ① 参数体检（**在起浏览器之前**）----
    filling = will_fill(prepare_only=args.prepare_only, check=args.check)
    problems = check_args(url=args.url, profile=args.profile, need_fill=filling)
    if problems:
        print("[参数] 有 %d 处要改：" % len(problems))
        for p in problems:
            print("       · %s" % p)
        return 2

    # ---- ② 环境能力预检（先探能力，再起窗口）----
    #   --check 也要探（体检就是来看能力的）；只有 --prepare-only 才跳过。
    if args.check or filling:
        env_ok, env_why = env_checker()
    else:
        env_ok, env_why = True, "（跳过：只起浏览器，不填表）"

    if args.check:
        print("[体检] 解释器：%s" % PY)
        print("[体检] 自动填表能力：%s" % ("可用 —— " + env_why if env_ok else "不可用 —— " + env_why))
        alive, info, host = sb.probe(args.port)
        print("[体检] 端口 %d：%s"
              % (args.port,
                 ("已有浏览器（%s，回环 %s）" % (info.get("Browser", "未知"), host)) if alive
                 else "空闲（还没起）"))
        target = args.profile_dir or sb.default_profile_dir()
        state, detail = sb.leftover_holders(target=target)
        _state_cn = {"none": "没有进程占着它", "one": "有 1 个主进程占着（可 --recover 清掉）",
                     "many": "有 %d 个主进程占着（需手工处理）" % len(detail),
                     "unknown": "查不到（进程枚举不可用）——不猜"}
        print("[体检] 配置档：%s" % target)
        print("[体检] 占用情况：%s" % _state_cn.get(state, state))
        if filling and not env_ok:
            print("\n[结论] 自动填表跑不了。保底路径：")
            print("       python scripts/wangshen.py sheet --profile <档案> \\")
            print("           --labels-file <页面字段.txt> --out 填写清单.md")
            print("       python scripts/wangshen.py sheethtml --in 填写清单.md")
            return 4
        return 0

    if filling and not env_ok:
        print("[环境] %s" % env_why)
        print()
        print("自动填表跑不了 —— 但它有**保底路径**（多数人最后都靠这条）：")
        print("  python scripts/wangshen.py sheet --profile <你的档案> \\")
        print("      --labels-file <页面字段.txt> --out 填写清单.md")
        print("  python scripts/wangshen.py sheethtml --in 填写清单.md")
        return 4
    print("[环境] %s" % env_why)

    # ---- ③ 起浏览器 ----
    prep_argv = build_prep_argv(port=args.port, profile_dir=args.profile_dir,
                                url=args.url, recover=args.recover)
    print("[步骤 1/2] 起浏览器…")
    rc = (preparer or run)(prep_argv)
    if rc != 0:
        print()
        print("[停] 浏览器没起来（退出码 %s）。原因见上面那段。" % rc)
        if not args.recover:
            print("     若是「配置档被残留实例占着」，加 --recover 再跑一次。")
        return 3

    if args.prepare_only:
        print()
        print("[完成] 浏览器已起好。请在窗口里**自己登录**，走到「填写简历」那一页，")
        print("       然后重跑本命令（**这次不要带 --prepare-only**）。")
        return 0

    # ---- ④ 接管填表 ----
    fill_argv = build_fill_argv(profile=args.profile, url=args.url, port=args.port,
                                dry_run=args.dry_run, screenshot=args.screenshot,
                                out=args.out, budget=args.budget, timeout=args.timeout,
                                headed=args.headed)
    print("[步骤 2/2] 接管填表…")
    return (runner or run)(fill_argv)


# ================================================================== 自测
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

    # ---- 参数体检：必须在起浏览器之前拦住 ----
    check("① 缺 --url → 报错（且不进入起浏览器）",
          any("--url" in p for p in check_args(url="", profile="x.json")))
    check("② 缺 --profile → 报错",
          any("--profile" in p for p in check_args(url="http://a", profile="")))
    check("③ 档案文件不存在 → 报错（不能拿不存在的档案硬跑）",
          any("不存在" in p for p in check_args(url="http://a", profile="/no/such/x.json")))
    check("④ 两个都给且文件在 → 无问题", check_args(url="http://a", profile=__file__) == [])
    check("⑤ --prepare-only 时不检查填表参数（只起浏览器凭什么要 url）",
          check_args(url="", profile="", need_fill=False) == [])

    # ---- 「要不要填表」的判定（体检/只起浏览器都不算填表）----
    check("⑤b 默认 → 要填表", will_fill() is True)
    check("⑤c --check → 不算填表（体检要能空跑）", will_fill(check=True) is False)
    check("⑤d --prepare-only → 不算填表", will_fill(prepare_only=True) is False)
    check("⑤e --check --prepare-only 也不要求填表参数",
          will_fill(prepare_only=True, check=True) is False)

    # ---- 命令拼装：纯函数，离线断言 ----
    p = build_prep_argv(port=9223, profile_dir="/tmp/pf", url="http://a", recover=True)
    check("⑥ 起浏览器命令：端口/配置档/地址都带上",
          p[1] == START_BROWSER and "9223" in p and "/tmp/pf" in p and "http://a" in p, p)
    check("⑦ --recover 只在要求时才透传（默认不自动清进程）",
          "--recover" in p and "--recover" not in build_prep_argv(port=1), p)

    f = build_fill_argv(profile="x.json", url="http://a", port=9222, dry_run=True,
                        screenshot="s.png", out="r.json", budget=300)
    check("⑧ 填表命令：接管地址按端口拼", "--connect" in f and "http://127.0.0.1:9222" in f, f)
    check("⑨ dry-run 会透传（预演不该写入）", "--dry-run" in f, f)
    check("⑩ 未指定 dry-run 时**不带**该参数（别误把正式填跑成预演）",
          "--dry-run" not in build_fill_argv(profile="x", url="u", port=1), f)
    check("⑪ 截图/报告/预算透传", "s.png" in f and "r.json" in f and "300" in f, f)

    # ---- 环境探测：支持注入（自测不必真起浏览器）----
    # ⑬⑭ 都**必须**核到注入的那一行字**原样返回** —— 只判 True/False 会假绿：
    #   "导入 playwright 失败"也返回 False，于是 ⑭ 在没装的机器上照样过，
    #   掩盖了"注入根本没生效"这件事。核字面量才能证明"确实走了注入那条路"。
    _ok = check_env(launcher=lambda: (True, "假"))
    check("⑬ 环境探测可注入 —— 自测不必真起浏览器",
          _ok[0] is True and _ok[1] == "假", _ok)
    _no = check_env(launcher=lambda: (False, "假"))
    check("⑭ 环境探测如实回报不可用（不假装能跑）",
          _no[0] is False and _no[1] == "假", _no)
    check("⑮ 环境探测返回一行原因（空原因等于没说）",
          bool(check_env(launcher=lambda: (False, "因为 X"))[1]))

    # ---- 反证：空值不许被拼成空参（那会让 argparse 报一个看不懂的错）----
    q = build_fill_argv(profile="x", url="u", port=1)
    check("⑫ 空的可选值不拼进命令行（不留裸参数）",
          all(not (isinstance(t, str) and t.strip() == "") for t in q), q)

    print("\n自测结果：%d 通过 / %d 失败" % (ok[0], bad[0]))
    return 0 if bad[0] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
