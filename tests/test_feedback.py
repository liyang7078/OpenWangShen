#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_feedback.py · 回流链的回归测试
====================================
盯住三件事，且都是**执行点**，不是文档里的承诺：

  ① 反馈包掺进真值 → 必须**拒绝**（不是警告、不是脱敏后放行）
  ② 考生侧产出的条目，作者侧收得进来 —— 一条完整闭环，键名逐项对得上
  ③ 链上用到的键名一律落在词表内（否则档案会「看着填了、实际取不到」）

另外钉住六组「后来补上的保证」：
  ④ 统一产物写入 `core.safe_write`：原子替换 / 默认不覆盖 / 基准目录校验
  ⑤ 面板化写入的纯函数（地区切分、控件分类、日期候选、报错翻译）
  ⑥ 作者侧收件台的优先级排序与陈旧提醒（只记时间与计数，不记真值）
  ⑦ Issue 模板与脚本选项集不漂移（模板是生成的，改了选项集必须重生成）
  ⑧ 文档里的数字必须能从资产现算出来，示例产物必须等于生成器输出（`doc_check`）
  ⑨ README 承诺的 Python 3.8+ 有东西盯着（`dist_check` 判据 5；真判据是 CI 那支 3.8 job）

断言的写法约定
--------------
断言**结构化事实**，不匹配文案：改一句提示语不该让测试变红，
但删掉一条保证必须变红。所以这里解析 HTML 成树、断言 `(机器码, 数据)`，
而不是 `assertIn("某句话", 输出)`。

运行：python -m unittest discover -s tests
"""

import argparse
import contextlib
import datetime
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import core                   # noqa: E402
import dist_check             # noqa: E402
import doc_check              # noqa: E402
import fill_form              # noqa: E402
import gen_issue_template     # noqa: E402
import htmlprobe              # noqa: E402
import import_resume          # noqa: E402
import inbox                  # noqa: E402
import issue_registry as reg  # noqa: E402
import make_sheet             # noqa: E402
import report_feedback as rf  # noqa: E402
import sheet_html             # noqa: E402

# 官方假值（取自 fixtures/档案_示例.json，在 dist_check 白名单内）
FAKE_PHONE = "13800000000"
FAKE_MAIL = "example@example.com"
FAKE_ID = "110101199901011234"

DEMO = {"字段标签": "联系人手机*", "系统": "北森",
        "期望键": "手机号", "实际结果": "未命中"}


class TestZeroTruthGate(unittest.TestCase):
    """① 零真值反证 —— 必须是硬门。"""

    def test_clean_payload_passes(self):
        self.assertTrue(reg.guard(rf.render(DEMO))[0])

    def test_phone_rejected(self):
        self.assertFalse(reg.guard(rf.render(DEMO) + "\n电话 " + FAKE_PHONE)[0])

    def test_email_rejected(self):
        self.assertFalse(reg.guard(rf.render(DEMO) + "\n邮箱 " + FAKE_MAIL)[0])

    def test_id_rejected(self):
        self.assertFalse(reg.guard(rf.render(DEMO) + "\n身份证 " + FAKE_ID)[0])

    def test_local_path_rejected(self):
        self.assertFalse(reg.guard(rf.render(DEMO) + "\n路径 C:/Users/me/简历.pdf")[0])

    def test_forbidden_key_rejected(self):
        """带真值的三段（填写结果 / 回读 / 附件结果）不得入库。"""
        ok, msgs = reg.guard("无真值", {"填写结果": [{"任何": "值"}]})
        self.assertFalse(ok)
        # 断言**机器码**与命中的键路径（是数据），不匹配说明文字里的字面词
        self.assertIn("禁用键", [kind for kind, _ in msgs])
        details = [detail for kind, detail in msgs if kind == "禁用键"]
        self.assertTrue(details and all("填写结果" in d for d in details), details)

    def test_render_itself_contains_no_pii(self):
        self.assertEqual(reg.pii_scan(rf.render(DEMO)), [])


class TestRoundTrip(unittest.TestCase):
    """② 闭环：考生侧产出 → 作者侧收进来 → 落进问题库。"""

    def test_both_sides_map_identically(self):
        """同一份内容，两侧各自转出来的条目必须一致（否则作者看到的是另一回事）。"""
        a = rf.to_issue(DEMO, source=42)
        b, parsed = inbox.to_item({"number": 42, "title": "t",
                                   "body": rf.render(DEMO)})
        for k in ("现象", "站点", "指纹", "严重度", "影响", "路径"):
            self.assertEqual(a[k], b[k], "字段 %s 两侧不一致" % k)
        self.assertEqual(parsed["字段标签"], "联系人手机*")
        self.assertEqual(parsed["系统"], "北森")

    def test_end_to_end_lands_in_registry(self):
        payload = dict(DEMO)
        text = rf.render(payload)
        item, _ = inbox.to_item({"number": 7, "title": "t", "body": text})

        tmp = tempfile.mkdtemp(prefix="ows_fb_")
        path = os.path.join(tmp, "q.jsonl")
        rc, qid = reg.do_add([], item, path)
        self.assertEqual(rc, 0, "登记失败")
        self.assertTrue(qid.startswith("Q-"))

        back, broken = reg.load_issues(path)
        self.assertEqual(broken, 0)
        self.assertEqual(len(back), 1)
        self.assertEqual(back[0]["状态"], "待试验", "新条目必须落在待试验")
        self.assertEqual(back[0]["来源Issue"], 7)
        # 收进来的东西本身也得是零真值的
        self.assertEqual(reg.pii_scan(json.dumps(back, ensure_ascii=False)), [])

    def test_dirty_body_does_not_leak_into_item(self):
        """正文里混了真值 → 真值不得被搬进条目（只搬四个结构化字段）。"""
        body = rf.render(DEMO) + "\n补充：我的手机 " + FAKE_PHONE
        item, _ = inbox.to_item({"number": 8, "title": "t", "body": body})
        blob = json.dumps(item, ensure_ascii=False)
        self.assertNotIn(FAKE_PHONE, blob)
        self.assertEqual(reg.pii_scan(blob), [])
        self.assertTrue(reg.guard(blob, item)[0])

    def test_dirty_field_label_is_refused_by_registry(self):
        """真值被塞进「字段标签」（能搬进条目）→ 入库时仍被 guard 拦住。

        这是最后一道闸：即使前面对正文的过滤被绕过，写盘前还有一次反证。
        """
        body = rf.render({"字段标签": FAKE_PHONE, "系统": "北森",
                          "期望键": "手机号", "实际结果": "未命中"})
        item, _ = inbox.to_item({"number": 9, "title": "t", "body": body})
        self.assertIn(FAKE_PHONE, json.dumps(item, ensure_ascii=False))

        tmp = tempfile.mkdtemp(prefix="ows_fb_")
        path = os.path.join(tmp, "q.jsonl")
        rc, _qid = reg.do_add([], item, path)
        self.assertEqual(rc, 3, "含真值的条目必须被拒绝")
        self.assertFalse(os.path.isfile(path), "被拒绝时一个字节都不该落盘")


class TestInboxParse(unittest.TestCase):
    def test_parse_full_body(self):
        p = inbox.parse_body(rf.render(DEMO))
        self.assertEqual(p["字段标签"], "联系人手机*")
        self.assertEqual(p["系统"], "北森")
        self.assertEqual(p["期望键"], "手机号")
        self.assertEqual(p["实际结果"], "未命中")

    def test_parse_empty_body(self):
        p = inbox.parse_body("")
        self.assertEqual(set(p.values()), {""})

    def test_dup_already_taken(self):
        item, _ = inbox.to_item({"number": 7, "title": "t", "body": rf.render(DEMO)})
        self.assertEqual(inbox.dup_of(item, [{"id": "Q-1", "来源Issue": 7}]),
                         ("已收", "Q-1"))

    def test_dup_same_field(self):
        item, _ = inbox.to_item({"number": 7, "title": "t", "body": rf.render(DEMO)})
        tag, qid = inbox.dup_of(item, [{"id": "Q-2", "现象": "字段「联系人手机*」没填上"}])
        self.assertEqual((tag, qid), ("同字段", "Q-2"))

    def test_dup_none(self):
        item, _ = inbox.to_item({"number": 7, "title": "t", "body": rf.render(DEMO)})
        self.assertEqual(inbox.dup_of(item, []), ("", ""))


class TestResumeImport(unittest.TestCase):
    def setUp(self):
        self.fields = core.load_rules()
        self.sample = "\n".join([
            "姓名：示例考生",
            "最高学历：硕士研究生",
            "手机号：" + FAKE_PHONE,
            "电子邮箱：" + FAKE_MAIL,
            "身份证号：" + FAKE_ID,
        ])
        self.got, _ = import_resume.extract(self.sample, self.fields)

    def test_extracts_label_lines(self):
        self.assertEqual(self.got.get("姓名"), "示例考生")
        self.assertEqual(self.got.get("最高学历"), "硕士研究生")

    def test_extracts_by_shape(self):
        self.assertEqual(self.got.get("手机号"), FAKE_PHONE)
        self.assertEqual(self.got.get("电子邮箱"), FAKE_MAIL)

    def test_id_derives_birth_and_sex(self):
        self.assertEqual(self.got.get("出生日期"), "1999-01-01")
        self.assertEqual(self.got.get("性别"), "男")

    def test_all_keys_are_rule_keys(self):
        """③ 抽出来的键必须都能在词表里找到 —— 否则填了也取不到。"""
        keys = {f["键"] for f in self.fields}
        self.assertEqual([k for k in self.got if k not in keys], [])

    def test_no_guess_when_absent(self):
        """抽不到就是抽不到，不许瞎填。"""
        self.assertNotIn("通讯地址", self.got)
        self.assertNotIn("毕业院校", self.got)

    def test_required_keys_exclude_attachments(self):
        req = import_resume.required_keys(self.fields)
        self.assertTrue(req)
        self.assertFalse([k for k in req if k.startswith("附件_")])


class TestSheetHtml(unittest.TestCase):
    """可复制清单页：断言**结构**与**往返**，不匹配文案。"""

    def test_parse_two_sections(self):
        md = "\n".join([
            "| 生成时间 | 2026-10-15 22:00 |",
            "## 一、照着往网页里粘（1 项就绪）", "",
            "| # | 页面字段 | 填写内容 | 必填 |", "|---|---|---|---|",
            "| 1 | 姓名 | 示例考生 | ✔ |", "",
            "## 二、档案里没有、需要你补的（1 项）", "",
            "| # | 页面字段 | 必填 | 对应档案键 |", "|---|---|---|---|",
            "| 1 | 通讯地址 | ✔ | `通讯地址` |",
        ])
        ready, miss, meta = sheet_html.parse_md(md)
        self.assertEqual(len(ready), 1)
        self.assertEqual(len(miss), 1)
        self.assertEqual(meta.get("生成时间"), "2026-10-15 22:00")
        self.assertTrue(ready[0]["必填"])

    def test_miss_key_has_backticks_stripped(self):
        """清单里的代码跨度反引号必须在 HTML 消费端剥掉。

        漏剥的后果有两层：页面上直接显示 `通讯地址`；以及 data-key 属性被污染。
        """
        md = "\n".join([
            "## 二、档案里没有、需要你补的（2 项）", "",
            "| # | 页面字段 | 必填 | 对应档案键 |", "|---|---|---|---|",
            "| 1 | 甲 | ✔ | `键甲` |",
            "| 2 | 乙 |  | 键乙 |",
        ])
        _ready, miss, _meta = sheet_html.parse_md(md)
        self.assertEqual([m["键"] for m in miss], ["键甲", "键乙"])

    def test_html_escapes_values_by_round_trip(self):
        """转义是否正确，看**解析回来等不等于原文**，而不是看实体串长什么样。"""
        raw = "<b>&x</b>"
        h = sheet_html.render_html([{"页面字段": "备注", "值": raw, "必填": False}],
                                   [], {})
        body = htmlprobe.parse(h)
        vals = body.find_all(**{"class": "v"})
        self.assertEqual(len(vals), 1)
        self.assertEqual(vals[0].text(), raw)
        self.assertEqual(body.find_all("b"), [], "值里的 <b> 被当成了标签")

    def test_attributes_are_escaped(self):
        """属性级转义：元信息里的引号不得溢出成新属性。"""
        evil = '" onmouseover="alert(1)'
        h = sheet_html.render_html([], [], {"企业": evil})
        body = htmlprobe.parse(h)
        metas = body.find_all(**{"class": "meta"})
        self.assertEqual(len(metas), 1)
        self.assertEqual(body.find_attr("onmouseover"), [])
        self.assertIn(evil, metas[0].text())

    def test_structure_is_well_formed(self):
        h = sheet_html.render_html([], [], {})
        body = htmlprobe.parse(h)
        self.assertTrue(h.lstrip().startswith("<!DOCTYPE html>"))
        for tag in ("html", "head", "body"):
            self.assertEqual(len(body.find_all(tag)), 1, "缺少或重复 <%s>" % tag)

    def test_accessibility_hooks_present(self):
        """无障碍不靠颜色/表情：语义由 data-* 钩子承载（措辞可自由改）。"""
        h = sheet_html.render_html([], [], {})
        body = htmlprobe.parse(h)
        self.assertEqual(len(body.find_all(**{"data-notice": "no-share"})), 1)
        self.assertEqual(len(body.find_all(**{"data-legend": "todo"})), 1)

    def test_miss_badge_is_text_not_colour_only(self):
        h = sheet_html.render_html(
            [], [{"页面字段": "通讯地址", "必填": True, "键": "通讯地址"}], {})
        badge = htmlprobe.parse(h).find_all(**{"data-badge": "todo"})
        self.assertEqual(len(badge), 1)
        self.assertTrue(badge[0].text().strip(), "待补标记只靠颜色、没有文字")
        # 待补值的前景色不能是弱化的灰
        self.assertEqual(sheet_html._css_decls(h, ".miss .v").get("color"),
                         "var(--ink)")


class TestGuardDeformation(unittest.TestCase):
    """
    红队反证：真值的**变形写法**以前能穿透 `guard()`。
    每一条都必须被判为「拦下」—— 这是执行点，不是文档承诺。

    ⚠️ 探针一律**拼接构造**，不写字面量：否则 dist_check 会举报本仓库自己。
    """

    def setUp(self):
        self.phone = "138" + "12345678"
        self.mail = "zhangsan" + chr(64) + "qq" + ".com"
        self.addr = "北京市" + "海淀区中关村大街1号院"

    def _blocked(self, text):
        return not reg.guard(text, {"现象": text})[0]

    def test_fullwidth_digits_blocked(self):
        full = "".join(chr(0xFF10 + int(c)) for c in self.phone)
        self.assertTrue(self._blocked("联系人手机*" + full))

    def test_space_separated_blocked(self):
        self.assertTrue(self._blocked("138 1234 5678"))

    def test_hyphen_separated_blocked(self):
        self.assertTrue(self._blocked("138-1234-5678"))

    def test_dotted_blocked(self):
        self.assertTrue(self._blocked("138.1234.5678"))

    def test_non_placeholder_email_blocked(self):
        self.assertTrue(self._blocked(self.mail))

    def test_fullwidth_email_blocked(self):
        full = "".join(chr(0xFF10 + int(c)) if c.isdigit() else
                       (chr(ord(c) + 0xFEE0) if 'a' <= c <= 'z' or 'A' <= c <= 'Z' else c)
                       for c in self.mail)
        self.assertTrue(self._blocked(full))

    def test_detailed_address_blocked(self):
        self.assertTrue(self._blocked(self.addr))

    def test_local_path_blocked(self):
        self.assertTrue(self._blocked("D:" + "/" + "私密/简历.pdf"))

    def test_http_url_not_false_positive(self):
        """http:// 里的 `p:/` 不能被误判成本机路径。"""
        self.assertTrue(reg.guard("页面是 https://job.example.com/a", {})[0])

    def test_normal_description_still_clean(self):
        for s in ("北森上传控件被隐藏，标签取不到",
                  "户籍所在地区县填写规范",
                  "下拉是虚拟列表，先输入关键词再选"):
            self.assertTrue(reg.guard(s, {})[0], s)

    def test_hits_are_masked_not_echoed(self):
        """命中样本必须脱敏 —— 否则调用方一 print，真值就上了终端。"""
        hits = reg.pii_scan("联系" + self.phone)
        self.assertTrue(hits)
        for _kind, sample in hits:
            self.assertNotIn(self.phone, sample)
            self.assertIn("*", sample)


class TestDistCheckGate(unittest.TestCase):
    """分发前扫描器本身的可信度：覆盖面必须完整，否则不得判「可以分发」。"""

    def setUp(self):
        import dist_check
        self.dc = dist_check

    def test_all_tracked_files_are_readable(self):
        files = self.dc.tracked_files()
        self.assertTrue(files)
        unread = [p for p in files
                  if not p.lower().endswith(self.dc.SKIP_SUFFIX)
                  and self.dc.read_lines(os.path.join(ROOT, p)) is None]
        self.assertEqual(unread, [], "有入库文件读不到，扫描覆盖面不完整")

    def test_no_git_escaped_paths(self):
        """`git ls-files` 默认会把中文路径转义成八进制串。"""
        bad = [p for p in self.dc.tracked_files() if p.startswith('"')]
        self.assertEqual(bad, [])

    def test_placeholder_rules(self):
        self.assertTrue(self.dc.is_placeholder_mobile("13800000000"))
        self.assertTrue(self.dc.is_placeholder_mobile("13333333333"))
        self.assertFalse(self.dc.is_placeholder_mobile("138" + "12345678"))
        self.assertTrue(self.dc.is_placeholder_email("a@example.com"))
        self.assertFalse(self.dc.is_placeholder_email("a" + chr(64) + "qq.com"))

    def test_repo_itself_passes(self):
        fails, _ = self.dc.check(verbose=False)
        self.assertEqual(fails, [], "本仓库自身未通过分发扫描：%s" % fails[:3])


class TestMappingSafety(unittest.TestCase):
    """识别引擎的两条安全阀：泛化短标签拒写、值域别名救回。"""

    @classmethod
    def setUpClass(cls):
        cls.rules = core.load_rules()
        cls.index = core.build_index(cls.rules)

    def test_generic_labels_are_refused(self):
        # 原实现会把它们静默填错（日期→加入党派时间、状态→健康状况…）
        for lab in ("日期", "时间", "状态", "性质", "方式", "请选择", "请选择日期"):
            key, how = core.map_one(lab, self.index, self.rules)
            self.assertIsNone(key, "泛化标签「%s」不该被映射（会静默填错）" % lab)

    def test_exact_labels_still_work(self):
        for lab in ("备注", "成绩", "姓名", "专业", "岗位"):
            key, how = core.map_one(lab, self.index, self.rules)
            self.assertTrue(key, "精确标签「%s」被误拦了" % lab)
            self.assertEqual(how, "exact")

    def test_strict_refuses_ties(self):
        """并列最长 ⇒ 歧义 ⇒ 拒写（原先按书写顺序先到先得 = 掷骰子）。"""
        fields = [
            {"键": "紧急联系人", "标签": ["紧急联系人"]},   # 5 字
            {"键": "联系人电话", "标签": ["联系人电话"]},   # 也是 5 字
        ]
        idx = core.build_index(fields)
        key, how = core.map_one_strict("紧急联系人电话", idx, fields)
        self.assertIsNone(key)
        self.assertEqual(how, "ambiguous")

    def test_strict_unique_longest_still_wins(self):
        """只有一个最长候选时不算歧义，正常返回。"""
        fields = [
            {"键": "电话", "标签": ["电话"]},
            {"键": "紧急联系人电话", "标签": ["紧急联系人电话"]},
        ]
        idx = core.build_index(fields)
        key, how = core.map_one_strict("紧急联系人电话", idx, fields)
        self.assertEqual(key, "紧急联系人电话")

    def test_value_alias_rescue(self):
        self.assertEqual(core.match_option("本科", ["大学本科", "硕士"]), "大学本科")
        self.assertEqual(core.match_option("硕士", ["本科", "硕士研究生"]), "硕士研究生")
        self.assertIsNone(core.match_option("不存在的值", ["甲", "乙"]))

    def test_canonical_value(self):
        self.assertEqual(core.canonical_value("大学本科"), "本科")
        self.assertEqual(core.canonical_value("火星语"), "火星语")

    def test_all_rule_keys_are_strings(self):
        for f in self.rules:
            self.assertIsInstance(f["键"], str)
            self.assertTrue(f["键"].strip())


class TestUrlGate(unittest.TestCase):
    """URL 门禁：程序要把真实信息写进这个页面，所以必须挑地方。"""

    def test_public_http_passes(self):
        for u in ("https://job.iguopin.com/x", "http://www.51job.com/a"):
            self.assertTrue(core.url_gate(u)[0], u)

    def test_dangerous_schemes_blocked(self):
        for u in ("file:///C:/secret.txt", "javascript:alert(1)",
                  "data:text/html,x", "ftp://x.com"):
            self.assertFalse(core.url_gate(u)[0], u)

    def test_internal_addresses_blocked(self):
        for u in ("http://127.0.0.1:8000/", "http://localhost/x",
                  "http://192.168.1.5/", "http://10.0.0.1/", "http://169.254.1.1/"):
            self.assertFalse(core.url_gate(u)[0], u)

    def test_allow_local_escape_hatch(self):
        self.assertTrue(core.url_gate("http://127.0.0.1:8000/", allow_local=True)[0])

    def test_empty_and_hostless(self):
        self.assertFalse(core.url_gate("")[0])
        self.assertFalse(core.url_gate("https://")[0])


class TestResumeRegexCost(unittest.TestCase):
    """
    邮箱正则必须**有界**：原实现 O(N²)，4 万字 4.2 秒、12 万字 37.8 秒，
    一份长简历能把本机拖死。
    """

    def test_long_text_is_fast(self):
        import re
        import time
        rx = dict(import_resume.PATTERNS)["电子邮箱"]
        s = "x" * 200_000          # 最坏情形：整段词字符、无 @
        t0 = time.time()
        rx.search(s)
        cost = time.time() - t0
        self.assertLess(cost, 1.0, "邮箱正则仍然过慢：%.2fs ⇒ 有二次回溯" % cost)

    def test_regex_still_matches(self):
        rx = dict(import_resume.PATTERNS)["电子邮箱"]
        m = rx.search("联系我：" + "zhangsan" + chr(64) + "example.com")
        self.assertTrue(m)


class TestBudgetExitCode(unittest.TestCase):
    """预算停手必须以退出码 5 反映出来，不能被收尾保险丝污染成 0。

    这里断言**行为**，不去匹配 `run()` 的源码文本 —— 后者会在改格式时假红，
    而且源码里写了 ≠ 真的这么跑。
    """

    def test_stop_reason_maps_to_5(self):
        self.assertEqual(fill_form.exit_code({"停止原因": "超预算（300 秒）"}), 5)

    def test_no_stop_reason_maps_to_0(self):
        self.assertEqual(fill_form.exit_code({"可自动填": 5}), 0)
        self.assertEqual(fill_form.exit_code({}), 0)

    def test_empty_stop_reason_is_not_a_stop(self):
        """空串不是「停手」—— 否则任何一次空赋值都会把成功报成失败。"""
        self.assertEqual(fill_form.exit_code({"停止原因": ""}), 0)

    def test_dangerous_url_is_refused_before_anything_else(self):
        """URL 门禁在 run() 里最先执行：file:// 直接退 2，连档案都不加载。

        这是真行为断言（不需要 playwright）：程序绝不把真值写进本机文件页。
        """
        ns = argparse.Namespace(url="file:///C:/secret.txt", allow_local=False)
        with contextlib.redirect_stdout(io.StringIO()):
            rc = fill_form.run(ns)
        self.assertEqual(rc, 2)

    def test_fill_form_selftest_passes(self):
        import subprocess
        rc = subprocess.call([sys.executable,
                              os.path.join(ROOT, "scripts", "fill_form.py"),
                              "--selftest"],
                             stdout=subprocess.DEVNULL)
        self.assertEqual(rc, 0, "fill_form 自测未通过")


class TestSafeWrite(unittest.TestCase):
    """统一产物写入器：原子替换 + 默认不覆盖 + 基准目录校验（不可绕过）。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ows_sw_")

    def _read(self, p):
        with open(p, encoding="utf-8") as fh:
            return fh.read()

    def test_first_write_lands_at_exact_path(self):
        p = os.path.join(self.dir, "a.txt")
        self.assertEqual(core.safe_write(p, "hello"), os.path.abspath(p))
        self.assertEqual(self._read(p), "hello")

    def test_default_refuses_to_overwrite(self):
        p = os.path.join(self.dir, "b.txt")
        first = core.safe_write(p, "第一版")
        second = core.safe_write(p, "第二版")
        self.assertNotEqual(first, second, "默认必须换名另存")
        self.assertEqual(self._read(first), "第一版", "旧文件被动了")
        self.assertEqual(self._read(second), "第二版")

    def test_overwrite_flag_replaces_in_place(self):
        p = os.path.join(self.dir, "c.txt")
        core.safe_write(p, "旧")
        again = core.safe_write(p, "新", overwrite=True)
        self.assertEqual(again, os.path.abspath(p))
        self.assertEqual(self._read(p), "新")
        self.assertEqual([f for f in os.listdir(self.dir) if f.startswith("c")],
                         ["c.txt"], "覆盖时不该多出副本")

    def test_root_baseline_refuses_outside(self):
        outside_dir = tempfile.mkdtemp(prefix="ows_out_")
        outside = os.path.join(outside_dir, "x.txt")
        with self.assertRaises(ValueError):
            core.safe_write(outside, "x", root=self.dir)
        self.assertFalse(os.path.exists(outside), "越界被拒后不该留下任何文件")

    def test_root_baseline_allows_inside(self):
        p = os.path.join(self.dir, "sub", "in.txt")
        self.assertTrue(core.safe_write(p, "ok", root=self.dir))

    def test_creates_missing_directories(self):
        p = os.path.join(self.dir, "深", "层", "e.txt")
        core.safe_write(p, "x")
        self.assertTrue(os.path.isfile(p))

    def test_no_temp_residue_left_behind(self):
        core.safe_write(os.path.join(self.dir, "d.txt"), "x")
        self.assertEqual([f for f in os.listdir(self.dir) if f.startswith(".ows-w-")],
                         [], "原子替换的临时文件必须清理干净")

    def test_binary_mode_round_trips(self):
        p = os.path.join(self.dir, "f.bin")
        core.safe_write(p, b"\x00\x01\xff", binary=True)
        with open(p, "rb") as fh:
            self.assertEqual(fh.read(), b"\x00\x01\xff")


class TestPanelHelpers(unittest.TestCase):
    """面板化写入的纯函数：地区切分 / 控件分类 / 日期候选 / 报错翻译。

    真站点上「自绘日历、级联下拉」填不进去，靠的就是这几个判据。
    它们不碰浏览器，所以能在任何机器上回归。
    """

    def test_region_splits_into_levels(self):
        self.assertEqual(fill_form.split_region("山东省济南市历下区"),
                         ["山东省", "济南市", "历下区"])
        self.assertEqual(fill_form.split_region("北京市海淀区"),
                         ["北京市", "海淀区"])
        self.assertEqual(fill_form.split_region("内蒙古自治区呼和浩特市"),
                         ["内蒙古自治区", "呼和浩特市"])

    def test_region_unparsable_stays_whole(self):
        """切不动就整段试，宁可试错也不猜。"""
        self.assertEqual(fill_form.split_region("历下区"), ["历下区"])
        self.assertEqual(fill_form.split_region(""), [])

    def test_panel_kind_detects_cascade_and_date(self):
        cascade = {"label": "户籍所在地", "key": "户籍", "value": "山东省济南市历下区",
                   "type": "text", "panel": True, "readonly": False}
        self.assertEqual(fill_form.panel_kind(cascade, {"panel": True}), "cascade")
        date = {"label": "毕业时间", "key": "毕业时间", "value": "2026-06",
                "type": "text", "panel": True, "readonly": False}
        self.assertEqual(fill_form.panel_kind(date, {"panel": True}), "date")

    def test_panel_kind_ignores_plain_inputs(self):
        """普通输入框走常规写入 —— 面板化是**兜底**，不是默认路径。"""
        plain = {"label": "姓名", "key": "姓名", "value": "张三",
                 "type": "text", "panel": False, "readonly": False}
        self.assertEqual(fill_form.panel_kind(plain, {"panel": False, "readonly": False}), "")

    def test_date_candidates_from_strict_to_loose(self):
        cands = fill_form._date_candidates("2026-06-30")
        self.assertEqual(cands[0], "2026-06-30", "整串必须排在最前（最严）")
        self.assertIn("2026/06/30", cands)
        self.assertIn("30", cands, "日历格子里的文本通常只有「日」")
        self.assertEqual(fill_form._date_candidates("2026-06")[0], "2026-06")
        self.assertEqual(fill_form._date_candidates(""), [])

    def test_friendly_error_translates_and_keeps_original(self):
        """超时/被拦截这类常见异常要译成人话，但**原文不能丢**。"""
        msg = fill_form.friendly_error(Exception("Timeout 30000ms exceeded"))
        self.assertTrue(msg.strip())
        self.assertIn("30000ms", msg, "翻译时把原始信息弄丢了")

    def test_friendly_error_never_returns_empty(self):
        self.assertTrue(fill_form.friendly_error(Exception("随便什么")).strip())


class TestInboxPriority(unittest.TestCase):
    """作者侧收件台：优先级排序与陈旧提醒。状态文件只记时间与计数，不记真值。"""

    def _row(self, sev="中", tag="新", number=1):
        return {"item": {"严重度": sev}, "tag": tag, "issue": {"number": number}}

    def test_severe_new_ranks_first(self):
        rows = [self._row("低", number=1), self._row("高", number=2),
                self._row("中", number=3)]
        ordered = [r["issue"]["number"] for r in inbox.sort_rows(rows)]
        self.assertEqual(ordered[0], 2, "高危新问题必须排最前")

    def test_handled_rows_sink_to_the_bottom(self):
        rows = [self._row("高", tag="已收", number=1), self._row("低", number=2)]
        self.assertEqual([r["issue"]["number"] for r in inbox.sort_rows(rows)][-1], 1)

    def test_priority_is_a_bounded_rank(self):
        for sev in ("高", "中", "低"):
            for tag in ("新", "批内重复", "同字段", "已收", "已忽略"):
                rank, why = inbox.priority_of(self._row(sev, tag))
                self.assertIn(rank, (1, 2, 3, 4), (sev, tag, rank))
                self.assertTrue(why.strip())

    def test_unknown_severity_falls_back_to_middle(self):
        """没见过的新取值不许崩，也不许当成「高危」插队。"""
        rank, _why = inbox.priority_of(self._row("闻所未闻"))
        self.assertEqual(rank, 2)

    def test_stale_days_none_on_first_run(self):
        self.assertIsNone(inbox.stale_days({}), "首次运行不该提醒")

    def test_stale_days_counts_from_last_view(self):
        now = datetime.datetime(2026, 10, 16, 9, 0)
        self.assertEqual(inbox.stale_days({"上次查看": "2026-10-10 09:00"}, now=now), 6)

    def test_stale_days_ignores_garbage(self):
        self.assertIsNone(inbox.stale_days({"上次查看": "不是时间"}))

    def test_state_file_holds_only_time_and_counts(self):
        """状态文件是长期留存的产物，不许落任何条目级内容。"""
        st = {"上次查看": "2026-10-16 09:00", "未决数": 3, "已收数": 1}
        tmp = tempfile.mkdtemp(prefix="ows_state_")
        p = os.path.join(tmp, "st.json")
        inbox.save_state(st, path=p)
        with open(p, encoding="utf-8") as fh:
            blob = fh.read()
        for banned in ("现象", "字段标签", "期望键", "标题", "手机", "邮箱"):
            self.assertNotIn(banned, blob, "状态文件里出现了条目级内容：%s" % banned)


class TestMakeSheet(unittest.TestCase):
    """清单生成：标签归一、未命中如实告知、附件单列、缺项带出规则键。"""

    @classmethod
    def setUpClass(cls):
        fx = os.path.join(ROOT, "fixtures")
        cls.fields = core.load_rules()
        cls.profile = core.load_profile(os.path.join(fx, "档案_示例.json"))
        with open(os.path.join(fx, "页面字段_示例.txt"), encoding="utf-8") as fh:
            cls.labels = [ln.strip() for ln in fh if ln.strip()]
        cls.ctx = {"now": "2026-10-16 09:00", "company": "示例集团"}

    def _build(self, labels, profile=None):
        return make_sheet.build(labels, self.fields,
                                self.profile if profile is None else profile,
                                self.ctx)

    def test_every_label_has_a_home(self):
        _c, rows, _r, _m, attach, unmatched = self._build(self.labels)
        self.assertEqual(len(rows) + len(attach) + len(unmatched), len(self.labels))
        self.assertGreater(len(rows), 0)

    def test_unknown_label_is_reported_not_guessed(self):
        bogus = "这个标签规则库里没有"
        _c, rows, _r, _m, _a, unmatched = self._build(self.labels + [bogus])
        self.assertEqual(unmatched, [bogus], "不认识的标签必须如实点名")
        self.assertEqual(len(rows), len(self._build(self.labels)[1]),
                         "未命中的标签不该凭空多出一行")

    def test_missing_key_becomes_a_miss_row_with_its_rule_key(self):
        stripped = {k: v for k, v in self.profile.items() if k != "通讯地址"}
        content, _rows, ready, miss, _a, _u = self._build(["通讯地址"], stripped)
        self.assertEqual([r["键"] for r in miss], ["通讯地址"])
        self.assertEqual(ready, [])
        self.assertIn("`通讯地址`", content, "缺项行没带出该填哪个键")

    def test_attachments_are_kept_out_of_the_data_rows(self):
        _c, rows, _r, _m, attach, _u = self._build(self.labels)
        self.assertFalse(set(r["键"] for r in attach) & set(r["键"] for r in rows))

    def test_full_listing_puts_required_first(self):
        _c, rows, _r, _m, attach, unmatched = self._build(None)
        self.assertEqual(unmatched, [])
        self.assertEqual(attach, [], "不给页面标签时不该推断出附件项")
        flags = [r["必填"] for r in rows]
        self.assertEqual(flags, sorted(flags, key=lambda b: not b))

    def test_no_overwrite_by_default(self):
        tmp = tempfile.mkdtemp(prefix="ows_ms_")
        target = os.path.join(tmp, "清单.md")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("旧内容")
        names = os.path.join(ROOT, "fixtures", "页面字段_示例.txt")
        prof = os.path.join(ROOT, "fixtures", "档案_示例.json")
        with contextlib.redirect_stdout(io.StringIO()):
            rc = make_sheet.main(["--profile", prof, "--labels-file", names,
                                  "--out", target])
        with open(target, encoding="utf-8") as fh:
            self.assertEqual(rc, 0)
            self.assertEqual(fh.read(), "旧内容", "默认不该覆盖已有文件")
        self.assertEqual(len(os.listdir(tmp)), 2, "应当另存出一个新文件")


class TestIssueTemplateDrift(unittest.TestCase):
    """Issue 模板是**生成的**：改了选项集却忘了重生成，必须能被发现。"""

    def test_generated_template_is_in_sync(self):
        text = gen_issue_template.build()
        path = os.path.join(ROOT, ".github", "ISSUE_TEMPLATE", "字段识别失败.md")
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), text,
                             "模板与脚本选项集漂移了：跑 python scripts/gen_issue_template.py")

    def test_template_covers_every_system_choice(self):
        text = gen_issue_template.build()
        for name in rf.system_choices():
            self.assertIn(name, text, "选项「%s」没进模板" % name)

    def test_check_flags_a_tampered_template(self):
        """反向：模板被改坏时 --check 必须返回非 0（否则漂移门是摆设）。

        探针写在系统临时目录，**不碰仓库里的真模板** —— 测试不该有副作用。
        """
        tmp = tempfile.mkdtemp(prefix="ows_tpl_")
        probe = os.path.join(tmp, "tampered.md")
        with open(probe, "w", encoding="utf-8", newline="") as fh:
            fh.write(gen_issue_template.build() + "\n<!-- 手改一行 -->\n")
        self.assertEqual(gen_issue_template.main(["--check", "--out", probe]), 3)

    def test_check_accepts_an_intact_template(self):
        tmp = tempfile.mkdtemp(prefix="ows_tpl_")
        probe = os.path.join(tmp, "ok.md")
        with open(probe, "w", encoding="utf-8", newline="") as fh:
            fh.write(gen_issue_template.build())
        self.assertEqual(gen_issue_template.main(["--check", "--out", probe]), 0)

    def test_missing_template_file_counts_as_drift(self):
        """模板文件丢了也是漂移（不能把「读不到」当成「一致」）。"""
        tmp = tempfile.mkdtemp(prefix="ows_tpl_")
        self.assertEqual(
            gen_issue_template.main(["--check", "--out", os.path.join(tmp, "无.md")]), 3)


class TestInboxResilience(unittest.TestCase):
    """Issue 正文完全不可信（公开仓库，任何人能提）。"""

    def test_malformed_json_is_friendly(self):
        """非法 JSON 要「像人一样拒绝」：退出码 2、说清是 JSON 的问题、不留 traceback。"""
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as fh:
            fh.write('{"a": [1,2,')
            bad = fh.name
        try:
            r = subprocess.run(
                [sys.executable, os.path.join(ROOT, "scripts", "inbox.py"),
                 "--from-file", bad],
                capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 2)
            self.assertNotIn("Traceback", r.stderr, "非法输入不该抛出栈")
            # 提「JSON」这个词是**必要信息**（告诉用户错在哪一类），不是措辞
            self.assertIn("JSON", r.stdout)
        finally:
            os.unlink(bad)

    def test_oversized_body_is_truncated(self):
        huge = "| 字段标签 | " + "长" * 999999 + " |\n"
        item, _ = inbox.to_item({"number": 1, "title": "t", "body": huge})
        self.assertLessEqual(len(item["现象"]), 200)

    def test_body_injection_cannot_forge_record(self):
        """正文里塞换行 + 伪 JSON，不能变成一条新记录。"""
        body = ("| 字段标签 | 手机* |\n"
                "\n"
                '{"id":"Q-19700101-001","现象":"伪造"}\n')
        item, _ = inbox.to_item({"number": 2, "title": "t", "body": body})
        self.assertNotIn("\n", item["现象"])
        self.assertNotIn("Q-19700101-001", json.dumps(item, ensure_ascii=False))


class TestFixturePageMapping(unittest.TestCase):
    """
    离线端到端：拿仓库自带的模拟网申页 `fixtures/模拟网申页.html` 走一遍完整映射。

    意义：`fill_form.py` 的正经验证需要浏览器（playwright），而大多数环境不装。
    这里用标准库 `html.parser` 复刻 JS 扫描器的取标签口径，
    让「取标签 → 映射 → 分类」这条链**在任何机器上都能回归**，
    而且能钉住「示例档案键改名后，真实页面标签还认不认」这件事。
    """

    class _Scan(HTMLParser):
        SKIP = ("hidden", "submit", "button", "image", "reset")

        def __init__(self):
            super().__init__()
            self.forlabels, self.bare, self.ctrls = {}, [], []
            self._lb, self._buf, self._has_ctrl = None, [], False

        def handle_starttag(self, tag, attrs):
            d = dict(attrs)
            if tag == "label":
                self._lb, self._buf, self._has_ctrl = (d.get("for") or ""), [], False
            elif tag in ("input", "select", "textarea"):
                self._has_ctrl = True
                self.ctrls.append((tag, d))

        def handle_data(self, data):
            if self._lb is not None:
                self._buf.append(data)

        def handle_endtag(self, tag):
            if tag == "label" and self._lb is not None:
                txt = "".join(self._buf).strip()
                if not self._has_ctrl:
                    if self._lb:
                        self.forlabels.setdefault(self._lb, txt)
                    else:
                        self.bare.append(txt)
                self._lb = None

    @classmethod
    def setUpClass(cls):
        import fill_form
        cls.ff = fill_form
        path = os.path.join(ROOT, "fixtures", "模拟网申页.html")
        p = cls._Scan()
        with open(path, encoding="utf-8") as fh:
            p.feed(fh.read())

        cls.fields, idx = [], 0
        for tag, d in p.ctrls:
            t = (d.get("type") or ("select" if tag == "select" else tag)).lower()
            if t in cls._Scan.SKIP:
                continue
            lab = (p.forlabels.get(d.get("id", ""), "")
                   or d.get("aria-label", "") or "")
            if t in ("radio", "checkbox") and not lab:
                lab = p.bare[0] if p.bare else ""
            cls.fields.append({"idx": idx, "label": lab.strip(), "type": t,
                               "name": d.get("name", ""), "id": d.get("id", ""),
                               "options": None, "visible": True,
                               "inForm": True, "readonly": False})
            idx += 1

        cls.rules = core.load_rules()
        cls.index = core.build_index(cls.rules)
        cls.profile = core.load_profile(
            os.path.join(ROOT, "fixtures", "档案_示例.json"))
        cls.plan, cls.gates, cls.unmatched, cls.notes = cls.ff.plan_mapping(
            cls.fields, [x["label"] for x in cls.fields],
            cls.profile, cls.index, cls.rules)

    def _key_of(self, label):
        """按归一化后的标签找（页面上带必填星号，如「应聘岗位*」）。"""
        target = core.norm(label)
        for p in self.plan:
            if core.norm(p["label"]) == target:
                return p["key"]
        return None

    def test_page_has_fields(self):
        self.assertGreaterEqual(len(self.fields), 20)

    def test_nothing_unmatched(self):
        """模拟页里的每个数据字段都该被认出来 —— 这条链不许有洞。"""
        self.assertEqual([f["label"] for f in self.unmatched], [])

    def test_renamed_example_keys_still_match_real_labels(self):
        """示例档案改键名后，真实页面标签（户籍所在地 / 英语等级 / 应聘岗位）仍要认。"""
        self.assertEqual(self._key_of("户籍所在地"), "户籍")
        self.assertEqual(self._key_of("英语等级"), "外语水平")
        self.assertEqual(self._key_of("应聘岗位"), "意向岗位")
        self.assertEqual(self._key_of("期望城市"), "意向城市")
        self.assertEqual(self._key_of("手机号码*"), "手机号")

    def test_human_gates_are_detected(self):
        """密码 / 验证码 / 上传框必须进人工门 —— 程序永不代填。"""
        labels = " ".join(g["label"] for g in self.gates)
        for must in ("登录密码", "验证码", "上传简历"):
            self.assertIn(must, labels, "人工门漏了 %s" % must)

    def test_priority_order_puts_name_first(self):
        """字段优先级生效：高频字段排在最前（预算到点时先填的就是它）。"""
        first = [p["key"] for p in self.plan if p["状态"] == "待填"][:1]
        self.assertEqual(first, ["姓名"], first)


class TestDocCheck(unittest.TestCase):
    """文档数字一致性门：数字必须能从资产现算出来，示例产物必须等于生成器输出。

    为什么值得单测：这道门**本身就是「承诺」**。如果它只会说「全部一致」，
    那它比没有更糟 —— 会让人以为数字被核对过了。所以下面每条断言都配了反证。
    """

    def test_facts_are_self_consistent(self):
        fx = doc_check.facts()
        self.assertEqual(fx["字段总数"], fx["可填值字段"] + fx["附件类"], fx)
        self.assertEqual(fx["必填档"] + fx["常用档"] + fx["其余选填档"],
                         fx["可填值字段"], fx)
        self.assertTrue(all(isinstance(v, int) and v > 0 for v in fx.values()), fx)

    def test_facts_come_from_assets_not_constants(self):
        """事实是现算的：两次调用必须一致，且确实等于资产里的条数。"""
        self.assertEqual(doc_check.facts(), doc_check.facts())
        fields = core.load_rules()
        self.assertEqual(doc_check.facts()["字段总数"], len(fields))

    def test_no_doc_drift_today(self):
        """当前状态：文档里的数字都算得出来（有漂移就是红）。"""
        bad = doc_check.check_docs(doc_check.facts())
        self.assertEqual(bad, [], "文档数字已与资产分叉：%s" % (bad[:3],))

    def test_examples_match_generators(self):
        """examples/ 下两份产物必须等于**现在**的生成器输出。"""
        bad = doc_check.check_examples()
        self.assertEqual(bad, [], "示例产物已与生成器分叉：%s" % (bad[:2],))

    def test_gate_bites_on_wrong_number(self):
        """反证：数字写错必须被抓到，且要指出是哪一个事实键。"""
        fx = doc_check.facts()
        _rel, pat, keys = doc_check.CLAIMS[0]
        text = "字段知识库（%d 字段 / %d 标签 / %d 组值域别名）" % (
            fx["字段总数"] + 1, fx["标签写法"], fx["值域别名组"])
        hits = doc_check.claims_in(text, pat, keys, fx)
        self.assertEqual([h[0] for h in hits], ["字段总数"], hits)
        self.assertEqual(hits[0][1], fx["字段总数"] + 1)
        self.assertEqual(hits[0][2], fx["字段总数"])

    def test_gate_bites_on_deleted_claim(self):
        """反证：把声明整句删掉也得报 —— 门不许随着文案改动静默失效。"""
        fx = doc_check.facts()
        _rel, pat, keys = doc_check.CLAIMS[0]
        self.assertTrue(doc_check.claims_in("这段话里没有那条声明", pat, keys, fx))

    def test_every_claim_key_is_real(self):
        """声明表里不许出现打错的键名（打错就会 IndexError 或静默通过）。"""
        fx = doc_check.facts()
        used = set()
        for _rel, _pat, ks in doc_check.CLAIMS:
            used.update(ks)
        self.assertTrue(used <= set(fx), sorted(used - set(fx)))

    def test_example_stamps_are_fixed(self):
        """示例产物的生成时间必须是固定示例戳 —— 否则校验不可能可复现。"""
        for rel, content in doc_check.regenerate_examples():
            with self.subTest(rel=rel):
                self.assertIn("生成时间", content)
        self.assertIn(doc_check.SHEET_STAMP,
                      dict(doc_check.regenerate_examples())[doc_check.SHEET_EXAMPLE])


class TestPythonFloor(unittest.TestCase):
    """README 承诺 Python 3.8+。判据 5 要真的会咬，且有反证撑住。"""

    def test_repo_passes_the_floor(self):
        files = [f for f in dist_check.tracked_files() if f.endswith(".py")]
        self.assertTrue(files, "没扫到 .py，判据 5 形同虚设")
        bad = {f: dist_check.py_min_problems(f) for f in files}
        bad = {k: v for k, v in bad.items() if v}
        self.assertEqual(bad, {}, "有 .py 过不了 3.8 下限：%s" % bad)

    def test_catches_runtime_only_breakage(self):
        """这三种都是**语法合法**、3.8 运行期才炸 —— 只靠 feature_version 抓不到。"""
        cases = {
            "注解联合": "def f(x: int | None):\n    pass\n",
            "内建泛型下标": "def f(x: list[int]):\n    pass\n",
            "return 注解": "def f() -> dict[str, int]:\n    pass\n",
            "removeprefix": "y = 'ab'.removeprefix('a')\n",
            "BooleanOptionalAction": (
                "import argparse\n"
                "p = argparse.ArgumentParser()\n"
                "p.add_argument('--x', action=argparse.BooleanOptionalAction)\n"),
            "BooleanOptionalAction 直接导入": (
                "from argparse import BooleanOptionalAction\n"
                "x = BooleanOptionalAction\n"),
            "tomllib": "import tomllib\n",
            "zoneinfo": "from zoneinfo import ZoneInfo\n",
        }
        for name, src in cases.items():
            with self.subTest(case=name):
                self.assertTrue(dist_check.py_min_problems_in(src), name)

    def test_no_false_positive_on_bitwise_or(self):
        """位运算 `|` 不是类型联合，不许误报。"""
        self.assertEqual(dist_check.py_min_problems_in("a = 1 | 2\n"), [])
        self.assertEqual(dist_check.py_min_problems_in("def f(x=None):\n    return x or 0\n"), [])

    def test_annotation_scan_does_not_leak_into_bodies(self):
        """只扫注解：函数体里的 `a | b` 是位运算/集合并，不该被当成类型联合。"""
        self.assertEqual(
            dist_check.py_min_problems_in("def f():\n    return {1} | {2}\n"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
