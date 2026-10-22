#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wangshen.py · OpenWangShen 统一入口
====================================
把所有子工具收进一条命令，避免记一堆脚本名。

    python scripts/wangshen.py checklist --out 网申信息清单.md     空白问卷（**第一步**）
    python scripts/wangshen.py resume  --in 我的简历.docx          简历导入：本机抽成预填档案
    python scripts/wangshen.py check   --profile profile.json      档案体检
    python scripts/wangshen.py sheet   --profile profile.json ...  生成填写清单
    python scripts/wangshen.py sheethtml --in 填写清单.md          可复制清单页（点一下就复制）
    python scripts/wangshen.py fill    --profile profile.json ...  自动填表（不提交）
    python scripts/wangshen.py run     --url U --profile p.json    一条命令跑完全程（推荐）
    python scripts/wangshen.py records --profile profile.json      记录组拆分（经历类多段）
    python scripts/wangshen.py probe   --connect 9222              只读探针：看清这页有哪些控件
    python scripts/wangshen.py mask    --profile profile.json ...  PII 脱敏
    python scripts/wangshen.py issue   --list                      问题库：登记 / 查询 / 策略匹配
    python scripts/wangshen.py trial   --issue <id> --path ...     试跑多路径 → 择优 → 固化
    python scripts/wangshen.py report                              反馈包：填不上的框 → 可提交的 Issue
    python scripts/wangshen.py inbox                               回流收件台：汇总别人提的反馈
    python scripts/wangshen.py template --check                    Issue 模板与脚本选项集是否一致
    python scripts/wangshen.py docs                                文档数字与示例产物是否与资产一致
    python scripts/wangshen.py selftest                            全部自测
    python scripts/wangshen.py guide                               打印使用路径

子命令的参数会原样透传给对应脚本：加 `-- -h` 可看该子命令的完整帮助，例如
    python scripts/wangshen.py fill -- -h

许可：MIT
"""

import os
import subprocess
import sys

# Windows 控制台默认编码往往不是 UTF-8（GBK 常见）。
# 本入口会打印 ⚠️ / ✅ 这类字符 —— 不兜底就会在第一屏直接 UnicodeEncodeError。
# errors="replace" 保证即便终端不支持也**不崩**，最差显示成问号。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 子进程同样继承，避免「入口活了、子脚本崩了」
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable

CMDS = {
    "checklist": ("make_checklist.py", "空白问卷：投网申之前要先备好哪些信息（第一步）"),
    "resume": ("import_resume.py", "简历导入：在本机把简历抽成预填档案（不联网）"),
    "check": ("check_profile.py", "档案体检：看你的档案缺什么"),
    "sheet": ("make_sheet.py", "生成填写清单（保底模式，零浏览器依赖）"),
    "sheethtml": ("sheet_html.py", "可复制清单页：点一下就复制的本机网页"),
    "fill": ("fill_form.py", "自动填表：打开岗位页→识别字段→写入（不提交）"),
    "run": ("apply_run.py", "一条命令跑完全程：起浏览器 → 探通 → 接管 → 填表"),
    "records": ("split_records.py", "记录组拆分：把经历类自由文本按日期切成可填的多段（只读档案）"),
    "probe": ("probe_page.py", "只读探针：看清这一页有哪些控件、哪些填不上、为什么（不点不填不删）"),
    "index": ("gen_script_index.py", "由 CMDS 现算本文件里的脚本速查表（--check 防漂移）"),
    "browser": ("start_browser.py", "开一个可被接管的浏览器（复用登录态，真实站必备）"),
    "mask": ("mask_pii.py", "PII 脱敏：需要 AI 帮忙写开放题时先脱敏"),
    "issue": ("issue_registry.py", "问题库：登记踩过的坑 / 查解法 / 按指纹匹配策略"),
    "trial": ("issue_trial.py", "问题试验：多路径试跑 → 断言择优 → 固化进策略库"),
    "report": ("report_feedback.py", "反馈包（考生侧）：把填不上的框整理成可提交的 Issue"),
    "inbox": ("inbox.py", "回流收件台（作者侧）：汇总别人提的反馈，逐条决定收不收"),
    "template": ("gen_issue_template.py", "由脚本选项集生成 Issue 模板（--check 防漂移）"),
    "docs": ("doc_check.py", "文档数字一致性门：文档里的数字 vs 资产现算值（--fix 可改写）"),
    "syncfields": ("sync_fields.py", "词表漂移门：比对两份字段规则字典（只读，--check 用于验收）"),
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

⚠️ 本工具是**独立功能**：它不代你注册、不代你登录。
   必须先自己完成注册 / 登录，**进到「填写简历信息」那个页面之后**再用。
   停在登录页、首页、岗位列表页上跑，它只会告诉你「没扫到表单」。

第 0 步  先出空白问卷（**这一步别跳**）
        python scripts/wangshen.py checklist --out 网申信息清单.md
        它把「网申会问到的字段」现算成一张空白问卷。
        **清单没填，就没有档案；没有档案，后面每一步都无从谈起。**

第 0.5 步  手上有现成简历？让它先替你抽一遍（可选，省大半力气）
        python scripts/wangshen.py resume --in 我的简历.docx
        在本机把简历抽成 profile.json：抽到的已填好，抽不到的留着待补。
        （.docx / .pdf 需要额外的库；缺了它会告诉你装哪个。）

第 1 步  体检
        python scripts/wangshen.py check --profile profile.json

第 2 步  试填（推荐先干跑一次）
        python scripts/wangshen.py fill --profile profile.json \\
            --url <岗位页地址> --dry-run
        看它能不能识别页面字段。识别不了就换第 3 步。

第 2.5 步  页面要求登录？（**真实网申站几乎都要求**——先看这一条）
        python scripts/wangshen.py browser
        它会开一个「可被接管」的浏览器。你在那个窗口里**自己登录**，
        走到「填写简历」那一页，然后：
        python scripts/wangshen.py fill --profile profile.json \\
            --url <那个页面的网址> --connect 9222
        程序接管的只是**页面**，不碰你的账号密码，也不会自己点提交。
        不这么做，裸开浏览器会直接停在登录页 —— 一个字段都扫不到。

第 3 步  正式填
        python scripts/wangshen.py fill --profile profile.json \\
            --url <岗位页地址> --screenshot filled.png
        程序填完会停下。**附件、志愿顺序、提交由你本人完成。**
        每站默认 300 秒预算：到点、或识别率过低，它自己停手，转下一行。
        填完会回读一遍核对 —— 写进去又被页面丢掉的，会从「已填」里剔除。

保底路径（第 2、3 步跑不通时 —— **多数人最后都靠这条**）
        python scripts/wangshen.py sheet --profile profile.json \\
            --labels-file 页面字段.txt --out 填写清单.md
        python scripts/wangshen.py sheethtml --in 填写清单.md
        先生成清单，再渲染成一张本机网页：点一下「复制」，粘到网页对应框里。

需要 AI 帮写开放题时
        python scripts/wangshen.py mask --profile profile.json \\
            --in 草稿.txt --out 脱敏稿.txt
        把「脱敏稿」发给 AI，回来再 --restore 还原。

填不上某个框？
        python scripts/wangshen.py report
        答几个问题，它会生成一段可以直接贴成 Issue 的反馈。
        你踩的坑记下来，下一个人就不用再踩。
        没有 GitHub 账号？不用加参数 —— 会自动给一条「一键邮件」的路
        （本项目已配置反馈邮箱：assets/反馈渠道.json）。
        两条都没有的话，把生成的那段文字直接发给项目作者也可以。

────────────────────────────────────────────────────────
术语对照（这个项目里几个词到底指什么）
  · 档案        = 你自己的那份 profile.json，存着姓名/手机号/学历这些真实信息。
                  它**只在你本机**，程序不会把它发给任何地方。
  · 字段标签     = 网页上那个框旁边写的字，比如「联系人手机*」。程序靠它判断
                  「这是哪一项」。带星号的 * 表示必填。
  · 指纹        = 一类**页面特征**的代号，比如「上传框被隐藏了」「下拉是自绘的」。
                  它不认站点：今天 A 站、明天 B 站，形态一样就复用同一条经验。
  · 试用/固化    = 同一个坑想出好几种修法，逐个真跑一遍比成绩，赢的那条才写进
                  策略库长期生效。没试赢过的不许固化。
  · 三条线      = ① 绝不代替你点提交 ② 绝不填密码、绝不碰验证码
                  ③ 你的身份信息不出本机。
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
        ("core.py", ["core.py", "--selftest"]),
        ("check_profile.py", ["check_profile.py", "--selftest"]),
        ("fill_form.py", ["fill_form.py", "--selftest"]),
        ("apply_run.py", ["apply_run.py", "--selftest"]),
        ("split_records.py", ["split_records.py", "--selftest"]),
        ("probe_page.py", ["probe_page.py", "--selftest"]),
        ("start_browser.py", ["start_browser.py", "--selftest"]),
        ("make_sheet.py", ["make_sheet.py", "--selftest"]),
        ("mask_pii.py", ["mask_pii.py", "--selftest"]),
        ("dist_check.py", ["dist_check.py", "--selftest"]),
        ("issue_registry.py", ["issue_registry.py", "--selftest"]),
        ("issue_trial.py", ["issue_trial.py", "--selftest"]),
        ("make_checklist.py", ["make_checklist.py", "--selftest"]),
        ("import_resume.py", ["import_resume.py", "--selftest"]),
        ("sheet_html.py", ["sheet_html.py", "--selftest"]),
        ("report_feedback.py", ["report_feedback.py", "--selftest"]),
        ("inbox.py", ["inbox.py", "--selftest"]),
        ("gen_issue_template.py", ["gen_issue_template.py", "--selftest"]),
        ("doc_check.py", ["doc_check.py", "--selftest"]),
        # 执行点（不是文档承诺）：Issue 模板若与脚本选项集漂移，这里直接红。
        ("Issue 模板一致性", ["gen_issue_template.py", "--check"]),
        # 执行点（不是文档承诺）：脚本速查表若与 CMDS 漂移，这里直接红。
        ("脚本速查表一致性", ["gen_script_index.py", "--check"]),
        # 执行点（不是文档承诺）：文档里的数字若与资产分叉，这里直接红。
        ("文档数字一致性", ["doc_check.py"]),
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
