"""Excel без внешних зависимостей: xlsx — это zip с несколькими XML.

Расширяет подход `orders/sinks.py:_write_xlsx`: добавлены стили (жирный заголовок,
разряды у сумм), ширина колонок и ссылки ячеек. Читается и Excel, и `XlsxFile`.
"""

from __future__ import annotations

import io
import zipfile
from typing import Any
from xml.sax.saxutils import escape

from documents.exporters import cell_values, meta_rows, template, totals_text
from ingest.xlsx_reader import column_name
from procurement.models import Specification

# Индексы стилей из `_STYLES`.
PLAIN, BOLD, NUMBER, BOLD_NUMBER = 0, 1, 2, 3
_NUMERIC_KEYS = {"unit_price", "total_price"}

_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<numFmts count="1"><numFmt numFmtId="164" formatCode="#,##0"/></numFmts>'
    '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
    '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
    '<fills count="2"><fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill></fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="4">'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
    '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '<xf numFmtId="164" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1" applyNumberFormat="1"/>'
    "</cellXfs>"
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    "</styleSheet>"
)
_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
    "</Types>"
)
_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Target="xl/workbook.xml" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"/>'
    "</Relationships>"
)
_BOOK_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Target="worksheets/sheet1.xml" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>'
    '<Relationship Id="rId2" Target="styles.xml" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles"/>'
    "</Relationships>"
)

Cell = tuple[Any, int]


def write_workbook(sheet_name: str, rows: list[list[Cell]], widths: list[float]) -> bytes:
    body = []
    for number, row in enumerate(rows, 1):
        cells = "".join(
            _cell(f"{column_name(index)}{number}", value, style)
            for index, (value, style) in enumerate(row)
            if value is not None and value != ""
        )
        body.append(f'<row r="{number}">{cells}</row>')
    cols = "".join(
        f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>'
        for index, width in enumerate(widths, 1)
    )
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<cols>{cols}</cols><sheetData>{''.join(body)}</sheetData></worksheet>"
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name="{escape(sheet_name[:31])}" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as book:
        book.writestr("[Content_Types].xml", _CONTENT_TYPES)
        book.writestr("_rels/.rels", _ROOT_RELS)
        book.writestr("xl/workbook.xml", workbook)
        book.writestr("xl/_rels/workbook.xml.rels", _BOOK_RELS)
        book.writestr("xl/styles.xml", _STYLES)
        book.writestr("xl/worksheets/sheet1.xml", sheet)
    return buffer.getvalue()


def _cell(ref: str, value: Any, style: int) -> str:
    attrs = f' r="{ref}"' + (f' s="{style}"' if style else "")
    if isinstance(value, int | float) and not isinstance(value, bool):
        return f"<c{attrs}><v>{value}</v></c>"
    text = escape(str(value))
    return f'<c{attrs} t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'


class ExcelExporter:
    format = "xlsx"
    extension = "xlsx"
    media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    def export(self, spec: Specification) -> bytes:
        layout = template()
        columns = layout["columns"]
        rows: list[list[Cell]] = [[(f"{layout['title']} № {spec.id}", BOLD)]]
        rows += [[(label, BOLD), (value, PLAIN)] for label, value in meta_rows(spec)]
        rows.append([])
        rows.append([(column["title"], BOLD) for column in columns])
        for values in cell_values(spec):
            rows.append(
                [
                    (value, NUMBER if column["key"] in _NUMERIC_KEYS else PLAIN)
                    for column, value in zip(columns, values, strict=True)
                ]
            )
        keys = [column["key"] for column in columns]
        totals: list[Cell] = [("", PLAIN)] * len(columns)
        totals[keys.index("name")] = ("Итого", BOLD)
        totals[keys.index("quantity")] = (spec.totals.quantity, BOLD)
        totals[keys.index("total_price")] = (spec.totals.amount, BOLD_NUMBER)
        rows.append(totals)
        rows.append([])
        rows.append([(totals_text(spec), PLAIN)])
        rows.append([(layout["notice"], PLAIN)])
        return write_workbook(layout["sheet"], rows, [column["width"] for column in columns])
