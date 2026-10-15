#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
htmlprobe.py · 极小的 HTML 结构探针（脚本自测与单元测试共用）
==============================================================
为什么要有它
------------
自测里 `assertIn("某句话", html)` 这种写法盯的是**文案**，两个方向都会骗人：

  · 把提示的措辞改一下 → 测试变红（假红，改动是安全的）
  · 把整段提示删掉、只要别处还残留同一句话 → 测试仍是绿（假绿，保证没了）

所以本项目的 HTML 产物一律**先解析成树**，再断言结构与语义：
元素在不在、属性对不对、行数够不够、文本往返等不等。

只依赖标准库，不追求完整 HTML 规范，够用即可：
  · 容错：标签乱掉不抛异常，最坏退化成一棵浅树
  · `convert_charrefs=True` ⇒ 文本已反转义，可做「往返相等」断言
  · `style` / `script` 的内容按原样取出（HTMLParser 的 CDATA 模式）

许可：MIT
"""

import re
from html.parser import HTMLParser

__all__ = ["El", "parse", "css_decls", "VOID"]

# 自闭合 / 空元素：不进栈，否则会把后面的兄弟节点全吞成子节点
VOID = {"meta", "br", "hr", "img", "input", "link", "source", "area",
        "base", "col", "embed", "param", "track", "wbr"}


class El(object):
    """一个元素节点。`children` 里混着 `El` 与 `str`（文本）。"""

    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag, attrs=None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children = []

    def text(self):
        """本节点子树内的可见文本（已反转义）。"""
        return "".join(c if isinstance(c, str) else c.text() for c in self.children)

    def find_all(self, tag=None, **attrs):
        """按标签名与属性**值**找后代。

        属性名带连字符（`data-x`）时不能写成关键字参数，
        用 `find_all(**{"data-x": "y"})`。
        """
        out = []
        for c in self.children:
            if isinstance(c, str):
                continue
            if (tag is None or c.tag == tag) and \
                    all(c.attrs.get(k) == v for k, v in attrs.items()):
                out.append(c)
            out.extend(c.find_all(tag, **attrs))
        return out

    def find_attr(self, name):
        """按「属性**是否存在**」找后代 —— 不管值是什么。

        值是 `None`（裸属性写法）时 `find_all(name=True)` 会漏掉，所以单列一个判据。
        """
        out = []
        for c in self.children:
            if isinstance(c, str):
                continue
            if name in c.attrs:
                out.append(c)
            out.extend(c.find_attr(name))
        return out


def parse(src):
    """把 HTML 解析成一棵树，返回根节点（tag 为 `#root`）。"""
    root = El("#root")
    stack = [root]

    class _P(HTMLParser):
        def handle_starttag(self, tag, attrs):
            el = El(tag, dict(attrs))
            stack[-1].children.append(el)
            if tag not in VOID:
                stack.append(el)

        def handle_startendtag(self, tag, attrs):
            stack[-1].children.append(El(tag, dict(attrs)))

        def handle_endtag(self, tag):
            for i in range(len(stack) - 1, 0, -1):
                if stack[i].tag == tag:
                    del stack[i:]
                    return

        def handle_data(self, data):
            stack[-1].children.append(data)

    _P(convert_charrefs=True).feed(src)
    return root


def css_decls(html_text, selector):
    """取出 `<style>` 里某个选择器的声明字典。取不到返回 `{}`。

    真解析，不是子串匹配 —— 否则「同一句声明出现在别的选择器里」也会算通过。
    """
    m = re.search(r"<style>(.*?)</style>", html_text, re.S)
    if not m:
        return {}
    rule = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", m.group(1))
    if not rule:
        return {}
    decls = {}
    for part in rule.group(1).split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            decls[k.strip()] = v.strip()
    return decls
