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
from html.parser import HTMLParser

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
    """预算停手必须以退出码 5 反映出来，不能被收尾保险丝污染成 0。"""

    def test_fill_form_selftest_passes(self):
        import subprocess
        rc = subprocess.call([sys.executable,
                              os.path.join(ROOT, "scripts", "fill_form.py"),
                              "--selftest"],
                             stdout=subprocess.DEVNULL)
        self.assertEqual(rc, 0, "fill_form 自测未通过")

    def test_stop_reason_maps_to_5(self):
        """`run()` 里最终退出码的算法：有停止原因 ⇒ 5。"""
        import inspect
        import fill_form
        src = inspect.getsource(fill_form.run)
        self.assertIn('return 5 if reason else 0', src)


class TestInboxResilience(unittest.TestCase):
    """Issue 正文完全不可信（公开仓库，任何人能提）。"""

    def test_malformed_json_is_friendly(self):
        import subprocess
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
            self.assertIn("不是合法 JSON", r.stdout)
            self.assertNotIn("Traceback", r.stderr)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
