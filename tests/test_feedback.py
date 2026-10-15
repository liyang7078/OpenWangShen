#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_feedback.py · 回流链的回归测试
====================================
盯住三件事，且都是**执行点**，不是文档里的承诺：

  ① 反馈包掺进真值 → 必须**拒绝**（不是警告、不是脱敏后放行）
  ② 考生侧产出的条目，作者侧收得进来 —— 一条完整闭环，键名逐项对得上
  ③ 链上用到的键名一律落在词表内（否则档案会「看着填了、实际取不到」）

运行：python -m unittest discover -s tests
"""

import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import core                   # noqa: E402
import import_resume          # noqa: E402
import inbox                  # noqa: E402
import issue_registry as reg  # noqa: E402
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
        self.assertTrue(any("禁用键" in k for k, _ in msgs))

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

    def test_html_escapes_values(self):
        h = sheet_html.render_html([{"页面字段": "备注", "值": "<b>&x</b>",
                                     "必填": False}], [], {})
        self.assertIn("&lt;b&gt;&amp;x&lt;/b&gt;", h)
        self.assertNotIn("<b>&x</b>", h)

    def test_html_blocks_are_closed(self):
        h = sheet_html.render_html([], [], {})
        self.assertTrue(h.startswith("<!DOCTYPE html>"))
        self.assertTrue(h.rstrip().endswith("</html>"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
