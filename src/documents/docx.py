"""Word без внешних зависимостей: шаблон `templates/word/document.xml` и zip-пакет.

`python-docx` в плане стоял для Word (Р6), но для таблицы спецификации хватает
WordprocessingML: заголовок, строки «поле — значение», таблица и итог. Шаблон
хранит разметку страницы (альбомная), код подставляет только собранные и
экранированные фрагменты.
"""

from __future__ import annotations

import io
import zipfile
from xml.sax.saxutils import escape

from documents.exporters import TEMPLATES, cell_values, meta_rows, template, totals_text
from procurement.models import Specification

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
)
_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Target="word/document.xml" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"/>'
    "</Relationships>"
)
_BORDERS = (
    "<w:tblBorders>"
    + "".join(
        f'<w:{side} w:val="single" w:sz="4" w:space="0" w:color="808080"/>'
        for side in ("top", "left", "bottom", "right", "insideH", "insideV")
    )
    + "</w:tblBorders>"
)


def paragraph(text: str, *, bold: bool = False, size: int | None = None) -> str:
    props = ""
    if bold or size:
        props = "<w:rPr>" + ("<w:b/>" if bold else "") + (f'<w:sz w:val="{size}"/>' if size else "") + "</w:rPr>"
    return f'<w:p><w:r>{props}<w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'


def table(rows: list[list[str]], header: bool = True) -> str:
    body = []
    for number, row in enumerate(rows):
        bold = header and number == 0
        cells = "".join(f"<w:tc>{paragraph(value, bold=bold, size=18)}</w:tc>" for value in row)
        body.append(f"<w:tr>{cells}</w:tr>")
    return f'<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/>{_BORDERS}</w:tblPr>{"".join(body)}</w:tbl>'


def write_document(title: str, meta: list[str], rows: list[list[str]], totals: str, notes: list[str]) -> bytes:
    skeleton = (TEMPLATES / "word" / "document.xml").read_text(encoding="utf-8")
    parts = {
        "{{TITLE}}": paragraph(title, bold=True, size=28),
        "{{META}}": "".join(paragraph(line) for line in meta),
        "{{TABLE}}": table(rows),
        "{{TOTALS}}": paragraph(totals, bold=True),
        "{{NOTES}}": "".join(paragraph(note, size=18) for note in notes),
    }
    for placeholder, xml in parts.items():
        skeleton = skeleton.replace(placeholder, xml)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", _CONTENT_TYPES)
        package.writestr("_rels/.rels", _ROOT_RELS)
        package.writestr("word/document.xml", skeleton)
    return buffer.getvalue()


class WordExporter:
    format = "docx"
    extension = "docx"
    media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

    def export(self, spec: Specification) -> bytes:
        layout = template()
        rows = [[column["title"] for column in layout["columns"]]]
        rows += [["" if value is None else str(value) for value in values] for values in cell_values(spec)]
        notes = [notice.message for notice in spec.warnings] + [layout["notice"]]
        return write_document(
            f"{layout['title']} № {spec.id}",
            [f"{label}: {value}" for label, value in meta_rows(spec)],
            rows,
            totals_text(spec),
            notes,
        )
