"""
Markdown 转 PDF 工具

负责把 Markdown 文本解析成 ReportLab 文档元素，再生成 PDF。
该方案不依赖 Microsoft Word、浏览器或系统级 PDF 工具，可在 macOS、Windows 和 Linux 上运行。
"""

from __future__ import annotations

import html
import logging
import re
from pathlib import Path

try:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.fonts import addMapping
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import (
        HRFlowable,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )
except ImportError:
    colors = None
    TA_LEFT = None
    addMapping = None
    A4 = None
    ParagraphStyle = None
    cm = None
    pdfmetrics = None
    UnicodeCIDFont = None
    TTFont = None
    HRFlowable = None
    Paragraph = None
    SimpleDocTemplate = None
    Spacer = None
    Table = None
    TableStyle = None

_CJK_FONT = "STSong-Light"
_EMBEDDED_FONT = "PSA-CJK"
_FONT_NAME = _CJK_FONT
_FONTS_READY = False
_FONT_CANDIDATES = (
    Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
    Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttf"),
    Path("/usr/share/fonts/wqy-microhei/wqy-microhei.ttc"),
    Path("/usr/share/fonts/wqy-microhei/wqy-microhei.ttf"),
    Path("/usr/share/fonts/wqy-zenhei/wqy-zenhei.ttc"),
    Path("/usr/share/fonts/wqy-zenhei/wqy-zenhei.ttf"),
    Path("/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf"),
    Path("/usr/share/fonts/google-droid/DroidSansFallbackFull.ttf"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc"),
    Path("/System/Library/Fonts/PingFang.ttc"),
    Path("/System/Library/Fonts/STHeiti Light.ttc"),
    Path("/System/Library/Fonts/Hiragino Sans GB.ttc"),
    Path("/System/Library/Fonts/Supplemental/Songti.ttc"),
    Path("/Library/Fonts/Arial Unicode.ttf"),
    Path("C:/Windows/Fonts/msyh.ttc"),
    Path("C:/Windows/Fonts/msyh.ttf"),
    Path("C:/Windows/Fonts/msyhbd.ttc"),
    Path("C:/Windows/Fonts/simhei.ttf"),
    Path("C:/Windows/Fonts/simsun.ttc"),
)
_PAGE_MARGIN = 1.8 * cm if cm else 0
_CONTENT_WIDTH = (A4[0] - 2 * _PAGE_MARGIN) if A4 else 0

_INLINE_TOKEN = re.compile(
    r"(\*\*[^*]+?\*\*|`[^`]+?`|\[[^\]]+\]\([^)]+\)|\*[^*\n]+?\*)"
)
_ORDERED_ITEM = re.compile(r"^(\d+)\.\s+(.+)$")
_BULLET_ITEM = re.compile(r"^[-*+]\s+(.+)$")
_HEADING = re.compile(r"^(#{1,6})\s+(.+)$")
_HR = re.compile(r"^(-{3,}|\*{3,}|_{3,})$")
_TABLE_SEPARATOR = re.compile(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?$")


def convert_md_to_pdf(md_abs_path: Path, pdf_abs_path: Path) -> str:
    """
    将 Markdown 文件转换为 PDF

    :param md_abs_path: Markdown 文件绝对路径
    :param pdf_abs_path: 输出 PDF 文件绝对路径
    :return: 转换结果说明
    """
    if SimpleDocTemplate is None:
        return "缺少依赖库，请安装 reportlab"

    try:
        with open(md_abs_path, "r", encoding="utf-8") as f:
            md_content = f.read()

        pdf_abs_path.parent.mkdir(parents=True, exist_ok=True)
        _register_fonts()

        doc = SimpleDocTemplate(
            str(pdf_abs_path),
            pagesize=A4,
            leftMargin=_PAGE_MARGIN,
            rightMargin=_PAGE_MARGIN,
            topMargin=_PAGE_MARGIN,
            bottomMargin=_PAGE_MARGIN,
            title=md_abs_path.stem,
            author="personal-search-assistant",
        )
        styles = _build_styles()
        story = _markdown_to_story(md_content, styles)
        doc.build(story)

        if pdf_abs_path.exists():
            return f"成功转换: {pdf_abs_path}"
        return f"转换完成但未生成文件: {pdf_abs_path}"

    except Exception as e:
        logging.error(f"Markdown 转 PDF 失败: {e}", exc_info=True)
        return f"转换失败: {str(e)}"


def _map_font_family(font_name: str) -> None:
    if addMapping is None:
        return
    addMapping(font_name, 0, 0, font_name)
    addMapping(font_name, 1, 0, font_name)
    addMapping(font_name, 0, 1, font_name)
    addMapping(font_name, 1, 1, font_name)


def _register_fonts() -> str:
    """
    优先嵌入系统 CJK TTF/TTC，保证中文度量准确、可换行。
    找不到时回退到 ReportLab 内置 STSong-Light。
    """
    global _FONT_NAME, _FONTS_READY
    if pdfmetrics is None:
        return _FONT_NAME
    if _FONTS_READY:
        return _FONT_NAME

    if TTFont is not None:
        for font_path in _FONT_CANDIDATES:
            if not font_path.exists():
                continue
            try:
                pdfmetrics.registerFont(TTFont(_EMBEDDED_FONT, str(font_path), subfontIndex=0))
                _map_font_family(_EMBEDDED_FONT)
                _FONT_NAME = _EMBEDDED_FONT
                _FONTS_READY = True
                return _FONT_NAME
            except Exception:
                continue

    try:
        pdfmetrics.registerFont(UnicodeCIDFont(_CJK_FONT))
        _map_font_family(_CJK_FONT)
        _FONT_NAME = _CJK_FONT
    except Exception:
        pass
    _FONTS_READY = True
    return _FONT_NAME


def _cjk_style(**kwargs) -> ParagraphStyle:
    """
    所有正文样式开启 CJK 换行，避免中文长句溢出或挤成一团。
    """
    kwargs.setdefault("fontName", _register_fonts())
    kwargs.setdefault("wordWrap", "CJK")
    kwargs.setdefault("splitLongWords", 1)
    kwargs.setdefault("alignment", TA_LEFT)
    return ParagraphStyle(**kwargs)


def _build_styles() -> dict[str, ParagraphStyle]:
    """
    构建 PDF 文档样式
    """
    _register_fonts()
    return {
        "body": _cjk_style(
            name="Body",
            fontSize=10.5,
            leading=17,
            spaceAfter=8,
        ),
        "h1": _cjk_style(
            name="Heading1",
            fontSize=20,
            leading=26,
            spaceBefore=4,
            spaceAfter=12,
            textColor=colors.HexColor("#111827") if colors else None,
        ),
        "h2": _cjk_style(
            name="Heading2",
            fontSize=15,
            leading=22,
            spaceBefore=12,
            spaceAfter=8,
            textColor=colors.HexColor("#1f2937") if colors else None,
        ),
        "h3": _cjk_style(
            name="Heading3",
            fontSize=12.5,
            leading=18,
            spaceBefore=8,
            spaceAfter=6,
            textColor=colors.HexColor("#374151") if colors else None,
        ),
        "list": _cjk_style(
            name="List",
            fontSize=10.5,
            leading=16,
            leftIndent=16,
            bulletIndent=4,
            spaceAfter=3,
        ),
        "quote": _cjk_style(
            name="Quote",
            fontSize=10,
            leading=16,
            leftIndent=6,
            textColor=colors.HexColor("#4b5563") if colors else None,
            spaceAfter=0,
        ),
        "cell": _cjk_style(
            name="Cell",
            fontSize=8.5,
            leading=13,
            spaceAfter=0,
            alignment=TA_LEFT,
        ),
        "cell_header": _cjk_style(
            name="CellHeader",
            fontSize=8.5,
            leading=13,
            spaceAfter=0,
            alignment=TA_LEFT,
            textColor=colors.HexColor("#111827") if colors else None,
        ),
        "code": _cjk_style(
            name="Code",
            fontSize=8,
            leading=12,
            leftIndent=4,
            rightIndent=4,
            spaceAfter=0,
            textColor=colors.HexColor("#111827") if colors else None,
        ),
    }


def _markdown_to_story(
    md_content: str,
    styles: dict[str, ParagraphStyle],
) -> list:
    """
    将常见 Markdown 结构转换为 ReportLab story
    """
    story = []
    lines = md_content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    index = 0
    paragraph_lines: list[str] = []
    pending_blank = False

    def flush_paragraph() -> None:
        nonlocal pending_blank
        if paragraph_lines:
            text = " ".join(line.strip() for line in paragraph_lines if line.strip())
            if text:
                story.append(Paragraph(_format_inline(text), styles["body"]))
            paragraph_lines.clear()
            pending_blank = False
        elif pending_blank:
            story.append(Spacer(1, 6))
            pending_blank = False

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        if not stripped:
            flush_paragraph()
            pending_blank = True
            index += 1
            continue

        if stripped.startswith("```"):
            flush_paragraph()
            code_lines: list[str] = []
            index += 1
            while index < len(lines) and not lines[index].strip().startswith("```"):
                code_lines.append(lines[index])
                index += 1
            story.append(_build_code_block("\n".join(code_lines), styles))
            index += 1
            continue

        if _is_table_start(lines, index):
            flush_paragraph()
            table_rows, index = _collect_table(lines, index)
            story.append(_build_table(table_rows, styles))
            story.append(Spacer(1, 10))
            continue

        heading = _parse_heading(stripped)
        if heading:
            flush_paragraph()
            level, text = heading
            style_name = "h1" if level == 1 else "h2" if level == 2 else "h3"
            story.append(Paragraph(_format_inline(text), styles[style_name]))
            index += 1
            continue

        if _HR.match(stripped) and HRFlowable is not None:
            flush_paragraph()
            story.append(
                HRFlowable(
                    width="100%",
                    thickness=0.6,
                    color=colors.HexColor("#cbd5e1"),
                    spaceBefore=8,
                    spaceAfter=10,
                )
            )
            index += 1
            continue

        if stripped.startswith(">"):
            flush_paragraph()
            quote_lines, index = _collect_prefixed(lines, index, ">")
            story.append(_build_quote(quote_lines, styles))
            continue

        ordered = _ORDERED_ITEM.match(stripped)
        if ordered:
            flush_paragraph()
            while index < len(lines):
                item = _ORDERED_ITEM.match(lines[index].strip())
                if not item:
                    break
                marker, text = item.group(1), item.group(2)
                story.append(Paragraph(f"{marker}. {_format_inline(text)}", styles["list"]))
                index += 1
            continue

        bullet = _parse_bullet(stripped)
        if bullet:
            flush_paragraph()
            while index < len(lines):
                item = _parse_bullet(lines[index].strip())
                if item is None:
                    break
                story.append(Paragraph(f"- {_format_inline(item)}", styles["list"]))
                index += 1
            continue

        paragraph_lines.append(line)
        index += 1

    flush_paragraph()
    return story


def _parse_heading(line: str) -> tuple[int, str] | None:
    """
    解析 Markdown 标题
    """
    match = _HEADING.match(line)
    if not match:
        return None
    return len(match.group(1)), match.group(2)


def _parse_bullet(line: str) -> str | None:
    """
    解析无序列表项
    """
    if _HR.match(line):
        return None
    match = _BULLET_ITEM.match(line)
    if not match:
        return None
    return match.group(1)


def _collect_prefixed(lines: list[str], index: int, prefix: str) -> tuple[list[str], int]:
    collected: list[str] = []
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped.startswith(prefix):
            break
        collected.append(stripped[len(prefix) :].lstrip())
        index += 1
    return collected, index


def _is_table_start(lines: list[str], index: int) -> bool:
    """
    判断当前位置是否是 Markdown 表格起点
    """
    if index + 1 >= len(lines):
        return False
    current = lines[index].strip()
    separator = lines[index + 1].strip()
    return "|" in current and bool(_TABLE_SEPARATOR.match(separator))


def _collect_table(lines: list[str], index: int) -> tuple[list[list[str]], int]:
    """
    收集连续 Markdown 表格行
    """
    rows = [_split_table_row(lines[index])]
    index += 2
    while index < len(lines) and "|" in lines[index].strip():
        rows.append(_split_table_row(lines[index]))
        index += 1
    return rows, index


def _split_table_row(line: str) -> list[str]:
    """
    拆分 Markdown 表格行
    """
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _table_col_widths(rows: list[list[str]], column_count: int) -> list[float]:
    """
    按内容权重把表格列宽限制在页面内，避免宽表冲出 A4。
    """
    min_width = 1.15 * cm
    weights: list[float] = []
    for column in range(column_count):
        longest = 1
        for row in rows:
            if column < len(row):
                longest = max(longest, len(row[column].strip()) or 1)
        weights.append(max(4.0, min(float(longest), 28.0)))
    total = sum(weights) or float(column_count)
    widths = [_CONTENT_WIDTH * weight / total for weight in weights]
    widths = [max(min_width, width) for width in widths]
    scale = _CONTENT_WIDTH / sum(widths)
    return [width * scale for width in widths]


def _build_table(rows: list[list[str]], styles: dict[str, ParagraphStyle]):
    """
    构建 PDF 表格
    """
    column_count = max(len(row) for row in rows) if rows else 1
    normalized_rows = [row + [""] * (column_count - len(row)) for row in rows]
    col_widths = _table_col_widths(normalized_rows, column_count)
    data = []
    for row_index, row in enumerate(normalized_rows):
        style = styles["cell_header"] if row_index == 0 else styles["cell"]
        data.append([Paragraph(_format_inline(cell) or "&nbsp;", style) for cell in row])
    table = Table(data, colWidths=col_widths, repeatRows=1, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eef6")),
                ("BACKGROUND", (0, 1), (-1, -1), colors.HexColor("#fbfcfe")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#111827")),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#94a3b8")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4f7fb")]),
            ]
        )
    )
    return table


def _build_quote(lines: list[str], styles: dict[str, ParagraphStyle]):
    text = "<br/>".join(_format_inline(line) if line else "&nbsp;" for line in lines) or "&nbsp;"
    inner = Paragraph(text, styles["quote"])
    table = Table([[inner]], colWidths=[_CONTENT_WIDTH])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f3f4f6")),
                ("LINEBEFORE", (0, 0), (0, -1), 2.4, colors.HexColor("#64748b")),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    return table


def _build_code_block(text: str, styles: dict[str, ParagraphStyle]):
    wrapped_lines: list[str] = []
    for raw_line in (text or "").split("\n"):
        line = raw_line.replace("\t", "    ")
        if len(line) <= 92:
            wrapped_lines.append(line)
            continue
        for start in range(0, len(line), 92):
            wrapped_lines.append(line[start : start + 92])
    escaped = html.escape("\n".join(wrapped_lines) or " ").replace(" ", "&nbsp;").replace("\n", "<br/>")
    inner = Paragraph(escaped, styles["code"])
    table = Table([[inner]], colWidths=[_CONTENT_WIDTH])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f1f5f9")),
                ("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    return table


def _soft_wrap(text: str) -> str:
    """
    给超长 URL / 无空格英文插入零宽空格，配合 CJK 换行避免撑破版心。
    """
    return re.sub(r"([A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=\-]{18})", r"\1&#8203;", text)


def _format_inline(text: str) -> str:
    """
    处理基础行内 Markdown 标记：加粗、斜体、行内代码、链接。
    """
    if not text:
        return ""

    parts: list[str] = []
    cursor = 0
    for match in _INLINE_TOKEN.finditer(text):
        parts.append(_soft_wrap(html.escape(text[cursor : match.start()])))
        token = match.group(1)
        if token.startswith("**") and token.endswith("**"):
            parts.append(f"<b>{_soft_wrap(html.escape(token[2:-2]))}</b>")
        elif token.startswith("`") and token.endswith("`"):
            parts.append(f'<font name="{_register_fonts()}" size="8">{html.escape(token[1:-1])}</font>')
        elif token.startswith("[") and "](" in token:
            label, href = token[1 : token.index("]")], token[token.index("(") + 1 : -1]
            safe_href = html.escape(href, quote=True)
            parts.append(
                f'<link href="{safe_href}" color="navy"><u>{_soft_wrap(html.escape(label))}</u></link>'
            )
        elif token.startswith("*") and token.endswith("*"):
            parts.append(f"<i>{_soft_wrap(html.escape(token[1:-1]))}</i>")
        else:
            parts.append(_soft_wrap(html.escape(token)))
        cursor = match.end()
    parts.append(_soft_wrap(html.escape(text[cursor:])))
    return "".join(parts)
