#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenWangShen 单元测试
=====================
纯标准库 unittest，不依赖第三方测试框架。

运行：
    python tests/test_core.py
    python -m unittest discover -s tests -v

许可：MIT
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import core          # noqa: E402
import make_sheet    # noqa: E402
import mask_pii      # noqa: E402

FIXTURES = os.path.join(ROOT, "fixtures")
DEMO_PROFILE = os.path.join(FIXTURES, "档案_示例.json")


class TestNorm(unittest.TestCase):
    """标签归一化"""

    def test_strip_required_mark(self):
        self.assertEqual(core.norm("姓名*"), "姓名")

    def test_strip_brackets(self):
        self.assertEqual(core.norm("姓名（中文）"), "姓名")
        self.assertEqual(core.norm("电话(手机)"), "电话")

    def test_english_lowercased(self):
        self.assertEqual(core.norm("Full Name"), "fullname")
        self.assertEqual(core.norm("E-mail"), "email")

    def test_fullwidth_space(self):
        self.assertEqual(core.norm("姓　名"), "姓名")

    def test_empty(self):
        self.assertEqual(core.norm(None), "")
        self.assertEqual(core.norm("   "), "")


class TestRules(unittest.TestCase):
    """规则库与索引"""

    def setUp(self):
        self.fields = core.load_rules()
        self.index = core.build_index(self.fields)

    def test_load_nonempty(self):
        self.assertGreater(len(self.fields), 30)

    def test_metadata_skipped(self):
        for f in self.fields:
            self.assertFalse(str(f["键"]).startswith("_"))

    def test_index_built(self):
        self.assertGreater(len(self.index), len(self.fields))

    def test_key_unique(self):
        keys = [f["键"] for f in self.fields]
        self.assertEqual(len(keys), len(set(keys)), "规则库存在重复键")


class TestMapping(unittest.TestCase):
    """字段映射"""

    def setUp(self):
        self.fields = core.load_rules()
        self.index = core.build_index(self.fields)

    def test_exact(self):
        key, how = core.map_one("姓名", self.index, self.fields)
        self.assertEqual(key, "姓名")
        self.assertEqual(how, "exact")

    def test_alias(self):
        cases = {
            "手机号码": "手机号",
            "联系电话": "手机号",
            "Email": "电子邮箱",
            "邮箱": "电子邮箱",
            "毕业学校": "毕业院校",
            "身份证号码": "身份证号",
        }
        for label, expect in cases.items():
            key, how = core.map_one(label, self.index, self.fields)
            self.assertEqual(key, expect, "标签 %s 应映射到 %s，实际 %s"
                             % (label, expect, key))

    def test_required_mark_ignored(self):
        key, _ = core.map_one("姓名*", self.index, self.fields)
        self.assertEqual(key, "姓名")

    def test_state_code_not_pii(self):
        """政治面貌这类非 PII 字段应正常映射（不该被隐私规则误伤）"""
        key, _ = core.map_one("政治面貌", self.index, self.fields)
        self.assertEqual(key, "政治面貌")

    def test_unmatched(self):
        key, how = core.map_one("火星文栏位XYZ", self.index, self.fields)
        self.assertIsNone(key)
        self.assertIsNone(how)

    def test_single_char_not_matched(self):
        """单字不该乱命中"""
        key, _ = core.map_one("名", self.index, self.fields)
        self.assertIsNone(key)

    def test_batch(self):
        labels = ["姓名", "手机号", "不存在字段"]
        matched, unmatched = core.map_fields(labels, self.fields)
        self.assertEqual(len(matched), 2)
        self.assertEqual(unmatched, ["不存在字段"])

    def test_attachment_flag(self):
        self.assertTrue(core.is_attachment("附件_简历"))
        self.assertFalse(core.is_attachment("姓名"))
        self.assertEqual(core.attachment_label("附件_简历"), "简历")


class TestProfile(unittest.TestCase):
    """档案读取与体检"""

    def setUp(self):
        self.fields = core.load_rules()
        self.profile = core.load_profile(DEMO_PROFILE)

    def test_load(self):
        self.assertIn("姓名", self.profile)

    def test_report_no_values(self):
        """体检报告结构里不能夹带真实值"""
        rpt = core.profile_report(self.profile, self.fields)
        blob = str(rpt)
        for v in self.profile.values():
            if isinstance(v, str) and len(v) >= 6:
                self.assertNotIn(v, blob, "体检结果里泄漏了真实值：%s" % v)

    def test_value_of(self):
        self.assertEqual(core.value_of(self.profile, "性别"), "男")
        self.assertEqual(core.value_of(self.profile, "不存在的键"), "")

    def test_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            core.load_profile(os.path.join(FIXTURES, "不存在.json"))


class TestSheet(unittest.TestCase):
    """填写清单"""

    def setUp(self):
        self.fields = core.load_rules()
        self.index = core.build_index(self.fields)
        self.profile = core.load_profile(DEMO_PROFILE)

    def test_rows_from_labels(self):
        labels = ["姓名", "手机号码*", "上传简历*"]
        rows, unmatched, _ = make_sheet.build_rows(
            labels, self.fields, self.profile, self.index)
        keys = [r["键"] for r in rows]
        self.assertIn("姓名", keys)
        self.assertIn("手机号", keys)
        # 附件不进填表表格
        self.assertNotIn("附件_简历", keys)

    def test_render_contains_header(self):
        rows, _, _ = make_sheet.build_rows(
            ["姓名"], self.fields, self.profile, self.index)
        ctx = {"now": "2026-01-01", "company": "X", "job": "Y",
               "url": "", "resume": ""}
        head, ready, miss = make_sheet.render(rows, ctx)
        self.assertIn("网申填写清单", head)
        self.assertIn("勿外发", head)
        self.assertEqual(len(ready), 1)

    def test_attachments_collected(self):
        labels = ["姓名", "上传简历", "成绩单"]
        at = make_sheet.collect_attachments(labels, self.fields,
                                           self.profile, self.index)
        self.assertEqual(len(at), 2)


class TestMask(unittest.TestCase):
    """脱敏通道"""

    def setUp(self):
        self.profile = core.load_profile(DEMO_PROFILE)
        self.mapping = mask_pii.build_map(self.profile)

    def test_pii_whitelist_excludes_gender(self):
        """性别、政治面貌这类不定位到人的字段，不得进 PII 白名单"""
        self.assertNotIn("男", self.mapping)
        self.assertNotIn("中共党员", self.mapping)

    def test_pii_included(self):
        self.assertIn("13800000000", self.mapping)
        self.assertIn("example@example.com", self.mapping)

    def test_mask_and_residual(self):
        text = "手机 13800000000，邮箱 example@example.com"
        masked, n = mask_pii.mask(text, self.mapping)
        self.assertEqual(n, 2)
        self.assertNotIn("13800000000", masked)
        self.assertEqual(mask_pii.residual_scan(masked, self.mapping), [])

    def test_restore(self):
        text = "手机 13800000000"
        masked, _ = mask_pii.mask(text, self.mapping)
        back, _ = mask_pii.restore(masked, self.mapping)
        self.assertIn("13800000000", back)

    def test_residual_detects_leak(self):
        """反证：故意留一处真值，二验必须抓到"""
        leaky = "手机 13800000000 还在"
        left = mask_pii.residual_scan(leaky, self.mapping)
        self.assertTrue(left, "二验应能发现残留")

    def test_non_pii_preserved(self):
        text = "我是中共党员，性别男"
        masked, _ = mask_pii.mask(text, self.mapping)
        self.assertIn("中共党员", masked)
        self.assertIn("男", masked)


class TestDateNormalize(unittest.TestCase):
    """日期归一化（fill_form）"""

    @classmethod
    def setUpClass(cls):
        try:
            import fill_form
            cls.mod = fill_form
        except ImportError:
            cls.mod = None

    def test_partial_completion(self):
        if not self.mod:
            self.skipTest("fill_form 不可导入")
        v, completed = self.mod._norm_date("2026-07")
        self.assertEqual(v, "2026-07-01")
        self.assertTrue(completed)

    def test_full_date_untouched(self):
        if not self.mod:
            self.skipTest("fill_form 不可导入")
        v, completed = self.mod._norm_date("2026-06-30")
        self.assertEqual(v, "2026-06-30")
        self.assertFalse(completed)

    def test_chinese_date(self):
        if not self.mod:
            self.skipTest("fill_form 不可导入")
        v, _ = self.mod._norm_date("2026年7月")
        self.assertEqual(v, "2026-07-01")

    def test_unparsable(self):
        if not self.mod:
            self.skipTest("fill_form 不可导入")
        v, _ = self.mod._norm_date("待定")
        self.assertIsNone(v)


if __name__ == "__main__":
    unittest.main(verbosity=2)
