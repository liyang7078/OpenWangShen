#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wangshen.py · OpenWangShen 统一入口
====================================
把四个子工具收进一条命令，避免记一堆脚本名。

    python scripts/wangshen.py check   --profile profile.json      档案体检
    python scripts/wangshen.py sheet   --profile profile.json ...  生成填写清单
    python scripts/wangshen.py fill    --profile profile.json ...  自动填表（不提交）
    python scripts/wangshen.py mask    --profile profile.json ...  PII 脱敏
    python scripts/wangshen.py selftest                            全部自测
    python scripts/wangshen.py guide                               打印使用路径

子命令的参数会原样透传给对应脚本：加 `-- -h` 可看该子命令的完整帮助，例如
    python scripts/wangshen.py fill -- -h

许可：MIT
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable

CMDS = {
    "check": ("check_profile.py", "档案体检：看你的档案缺什么"),
    "sheet": ("make_sheet.py", "生成填写清单（保底模式，零浏览器依赖）"),
    "fill": ("fill_form.py", "自动填表：打开岗位页→识别字段→写入（不提交）"),
    "mask": ("mask_pii.py", "PII 脱敏：需要 AI 帮忙写开放题时先脱敏"),
}


def usage():
    print(__doc__)
    print("可用子命令：")
    for k, (_, d) in CMDS.items():
        print("  %-8s %s" % (k, d))
    print("  %-8s %s" % ("selftest", "依次跑全部脚本的内置自测"))
    print("  %-8s %s" % ("guide", "打印推荐使用顺序"))


def guide():
    print("""
────────────────────────────────────────────────────────
OpenWangShen · 推荐使用顺序
────────────────────────────────────────────────────────

第 0 步  准备档案
        复制 fixtures/档案_示例.json 为 profile.json，填成你自己的信息。
        ⚠️ profile.json 只放本机，不要提交到 Git（.gitignore 已屏蔽）。

第 1 步  体检
        python scripts/wangshen.py check --profile profile.json

第 2 步  试填（推荐先干跑一次）
        python scripts/wangshen.py fill --profile profile.json \\
            --url <岗位页地址> --dry-run
        看它能不能识别页面字段。识别不了就换第 3 步。

第 3 步  正式填
        python scripts/wangshen.py fill --profile profile.json \\
            --url <岗位页地址> --screenshot filled.png
        程序填完会停下。**附件、志愿顺序、提交由你本人完成。**

保底路径（第 2 步跑不通时）
        python scripts/wangshen.py sheet --profile profile.json \\
            --labels-file 页面字段.txt --out 填写清单.md
        生成清单，你照着往网页里粘。

需要 AI 帮写开放题时
        python scripts/wangshen.py mask --profile profile.json \\
            --in 草稿.txt --out 脱敏稿.txt
        把「脱敏稿」发给 AI，回来再 --restore 还原。

────────────────────────────────────────────────────────
三条不可越过的线
  1. 绝不代替你点提交
  2. 绝不填密码、绝不碰验证码
  3. 你的身份信息不出本机
────────────────────────────────────────────────────────
""")


def selftest():
    print("=" * 56)
    print("OpenWangShen 自测")
    print("=" * 56)
    results = []
    for name, args in [
        ("core.py", ["core.py"]),
        ("check_profile.py", ["check_profile.py", "--selftest"]),
        ("make_sheet.py", ["make_sheet.py", "--selftest"]),
        ("mask_pii.py", ["mask_pii.py", "--selftest"]),
    ]:
        print("\n--- %s ---" % name)
        rc = subprocess.call([PY, os.path.join(HERE, args[0])] + args[1:])
        results.append((name, rc))

    print("\n" + "=" * 56)
    print("自测汇总")
    for name, rc in results:
        print("  %-20s %s" % (name, "通过" if rc == 0 else "失败 (exit %d)" % rc))
    failed = [n for n, rc in results if rc != 0]
    print("=" * 56)
    if failed:
        print("有 %d 项未通过" % len(failed))
        return 1
    print("全部通过")
    return 0


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        usage()
        return 0

    cmd = sys.argv[1]
    if cmd == "selftest":
        return selftest()
    if cmd == "guide":
        guide()
        return 0
    if cmd not in CMDS:
        print("未知子命令：%s\n" % cmd)
        usage()
        return 1

    script, _ = CMDS[cmd]
    argv = [PY, os.path.join(HERE, script)] + sys.argv[2:]
    try:
        return subprocess.call(argv)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
