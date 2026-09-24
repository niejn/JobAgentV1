"""Render a resume content JSON into a layout-optimized .docx.

Extracted from the agent-authored workspace script (2026-09-18, v7 layout):
the layout engine below is verbatim; content now arrives as JSON so every
future resume version (v8, v15, ...) is a data edit, not a code fork.

Usage:
    python build_resume_docx.py --content resume_v7_content.json --out out.docx

Content schema (assets/resume_v7_content.json is the reference):
    header: {"name": str, "contact": str, "footer": str}
    blocks: list of {"t": <type>, ...} in document order —
      section  {text}                      节标题：主色加粗 + 底部细线
      body     {text}                      正文段（左缩进 0.1cm）
      bullet   {text}                      项目符号段（悬挂缩进）
      entry    {left: [[text, {bold,size}], ...], right: str}
                                            经历条目头：左侧 runs + 右侧日期制表位
      project  {text, note, before?}       项目标题：11pt 加粗 + 灰色备注
      meta     {label, text}               "角色：/技术栈：" 行
      label    {text}                      加粗小标签（项目实现：）
      num      {num, text}                 编号段（悬挂缩进）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

# ── 版式常量（v7 实测定稿：A4 窄边距单栏，紧凑行距）──
LATIN = "Calibri"
CJK = "微软雅黑"
ACCENT = "1F4E79"
ACCENT_RGB = RGBColor(0x1F, 0x4E, 0x79)
GRAY = RGBColor(0x59, 0x59, 0x59)
DARK = RGBColor(0x26, 0x26, 0x26)

PAGE_W_CM = 21.0
MARGIN = {"top": 1.35, "bottom": 1.25, "left": 1.6, "right": 1.6}
BODY_PT = 9.5
LINE_SPACING = 1.22


def set_cjk_font(run, latin: str = LATIN, cjk: str = CJK) -> None:
    """Write w:ascii/w:hAnsi/w:eastAsia explicitly (run.font.name misses eastAsia)."""
    run.font.name = latin
    run._element.get_or_add_rPr()  # ensure rPr exists before rFonts lookup
    r_fonts = run._element.rPr.find(qn("w:rFonts"))
    if r_fonts is None:
        r_fonts = OxmlElement("w:rFonts")
        run._element.rPr.insert(0, r_fonts)
    for attr, value in (("ascii", latin), ("hAnsi", latin), ("eastAsia", cjk)):
        r_fonts.set(qn(f"w:{attr}"), value)


class ResumeRenderer:
    def __init__(self) -> None:
        self.doc = Document()
        sec = self.doc.sections[0]
        sec.page_width = Cm(PAGE_W_CM)
        sec.page_height = Cm(29.7)
        sec.top_margin = Cm(MARGIN["top"])
        sec.bottom_margin = Cm(MARGIN["bottom"])
        sec.left_margin = Cm(MARGIN["left"])
        sec.right_margin = Cm(MARGIN["right"])
        normal = self.doc.styles["Normal"]
        normal.font.name = LATIN
        normal.font.size = Pt(BODY_PT)
        normal.font.color.rgb = DARK
        normal.element.rPr.rFonts.set(qn("w:eastAsia"), CJK)
        normal.paragraph_format.space_after = Pt(1.5)
        normal.paragraph_format.line_spacing = LINE_SPACING

    # ── 基础 ──
    def _fmt(self, p, before=0, after=2, line=LINE_SPACING):
        pf = p.paragraph_format
        pf.space_before = Pt(before)
        pf.space_after = Pt(after)
        pf.line_spacing = line
        return p

    def _run(self, p, text, size: float = 10, bold=False, color=DARK, italic=False):
        r = p.add_run(text)
        set_cjk_font(r)
        r.font.size = Pt(size)
        r.font.bold = bold
        r.font.italic = italic
        r.font.color.rgb = color
        return r

    # ── 原语 ──
    def section(self, text):
        p = self.doc.add_paragraph()
        self._fmt(p, before=7, after=3)
        p.paragraph_format.keep_with_next = True
        self._run(p, text, size=12, bold=True, color=ACCENT_RGB)
        pPr = p._p.get_or_add_pPr()
        pBdr = OxmlElement("w:pBdr")
        bottom = OxmlElement("w:bottom")
        bottom.set(qn("w:val"), "single")
        bottom.set(qn("w:sz"), "8")
        bottom.set(qn("w:space"), "2")
        bottom.set(qn("w:color"), ACCENT)
        pBdr.append(bottom)
        pPr.append(pBdr)
        return p

    def bullet(self, text, indent=0.42, hanging=0.42):
        p = self.doc.add_paragraph()
        self._fmt(p, after=2)
        pf = p.paragraph_format
        pf.left_indent = Cm(indent)
        pf.right_indent = Cm(0.15)
        pf.first_line_indent = -Cm(hanging)
        self._run(p, "• ")
        self._run(p, text)
        return p

    def entry(self, left_runs, right_text):
        p = self.doc.add_paragraph()
        self._fmt(p, before=4, after=1)
        p.paragraph_format.keep_with_next = True
        usable = Cm(PAGE_W_CM) - Cm(MARGIN["left"]) - Cm(MARGIN["right"])
        # The workspace original called a non-existent add_right_tab_stop(); the
        # shipped v7 docx therefore has NO tab stops (dates ride the default
        # tab grid). This is the intended, working form.
        p.paragraph_format.tab_stops.add_tab_stop(usable, WD_TAB_ALIGNMENT.RIGHT)
        for txt, kw in left_runs:
            self._run(p, txt, **kw)
        self._run(p, "\t" + right_text, size=BODY_PT, color=GRAY)
        return p

    def project(self, text, note, before=6):
        p = self.doc.add_paragraph()
        self._fmt(p, before=before, after=1)
        p.paragraph_format.keep_with_next = True
        self._run(p, text, size=11, bold=True)
        self._run(p, note, size=BODY_PT, color=GRAY)
        return p

    def meta(self, label, text):
        p = self.doc.add_paragraph()
        self._fmt(p, after=1)
        p.paragraph_format.left_indent = Cm(0.1)
        self._run(p, label, bold=True)
        self._run(p, text)
        return p

    def body(self, text, indent=0.1, after=2):
        p = self.doc.add_paragraph()
        self._fmt(p, after=after)
        p.paragraph_format.left_indent = Cm(indent)
        self._run(p, text)
        return p

    def label(self, text):
        p = self.doc.add_paragraph()
        self._fmt(p, after=1)
        p.paragraph_format.left_indent = Cm(0.1)
        p.paragraph_format.keep_with_next = True
        self._run(p, text, bold=True)
        return p

    def num(self, num, text, indent=0.42):
        p = self.doc.add_paragraph()
        self._fmt(p, after=2)
        pf = p.paragraph_format
        pf.left_indent = Cm(indent + 0.5)
        pf.first_line_indent = -Cm(0.5)
        self._run(p, num + "、")
        self._run(p, text)
        return p

    # ── 页眉/页脚 ──
    def header(self, name: str, contact: str) -> None:
        name_p = self.doc.add_paragraph()
        self._fmt(name_p, before=0, after=1)
        name_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        self._run(name_p, name, size=17, bold=True, color=ACCENT_RGB)
        contact_p = self.doc.add_paragraph()
        self._fmt(contact_p, after=4)
        contact_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        self._run(contact_p, contact, size=BODY_PT, color=GRAY)

    def footer(self, text: str) -> None:
        sec = self.doc.sections[0]
        f = sec.footer
        f.is_linked_to_previous = False
        fp = f.paragraphs[0]
        fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        self._run(fp, text, size=8, color=GRAY)
        self._run(fp, "　—　", size=8, color=GRAY)
        r_pg = fp.add_run()
        set_cjk_font(r_pg)
        r_pg.font.size = Pt(8)
        r_pg.font.color.rgb = GRAY
        fld1 = r_pg._r.makeelement(qn("w:fldChar"), {qn("w:fldCharType"): "begin"})
        r_pg._r.append(fld1)
        r2 = fp.add_run()
        set_cjk_font(r2)
        instr = r2._r.makeelement(qn("w:instrText"), {qn("xml:space"): "preserve"})
        instr.text = " PAGE "
        r2._r.append(instr)
        r3 = fp.add_run()
        fld2 = r3._r.makeelement(qn("w:fldChar"), {qn("w:fldCharType"): "end"})
        r3._r.append(fld2)

    # ── 渲染 ──
    def render(self, content: dict[str, Any], out_path: Path) -> None:
        head = content["header"]
        self.header(head["name"], head["contact"])
        for block in content["blocks"]:
            t = block["t"]
            if t == "section":
                self.section(block["text"])
            elif t == "body":
                self.body(block["text"])
            elif t == "bullet":
                self.bullet(block["text"])
            elif t == "entry":
                self.entry(block["left"], block["right"])
            elif t == "project":
                self.project(block["text"], block["note"], before=block.get("before", 6))
            elif t == "meta":
                self.meta(block["label"], block["text"])
            elif t == "label":
                self.label(block["text"])
            elif t == "num":
                self.num(block["num"], block["text"])
            else:
                raise ValueError(f"unknown block type: {t}")
        self.footer(head.get("footer", head["name"]))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        self.doc.save(str(out_path))
        if sys.platform == "win32":
            print(out_path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--content", required=True, help="content JSON path")
    parser.add_argument("--out", required=True, help="output .docx path")
    args = parser.parse_args()
    content = json.loads(Path(args.content).read_text(encoding="utf-8"))
    ResumeRenderer().render(content, Path(args.out))
    print(f"SAVED: {args.out}")


if __name__ == "__main__":
    main()
