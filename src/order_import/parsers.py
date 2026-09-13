"""Разбор файла заказа: Excel, Word, PDF, CSV.

Парсер только извлекает строки и ячейки. Где заголовок, что артикул, а что цена,
решает нормализатор; соответствует ли позиция каталогу и нормативу — оценка.
Новых зависимостей нет: xlsx и docx — zip с XML, PDF читает уже подключённый pypdf.

Скан PDF без текстового слоя не распознаётся (OCR не делаем, Р6): заказ получает
предупреждение и уходит на ручную проверку.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from xml.etree import ElementTree as ET

from core.errors import InvalidRequest, Notice
from ingest.xlsx_reader import XlsxFile, column_index

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_COLUMNS_GAP = re.compile(r"\s{2,}")

MEDIA_TYPES = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pdf": "application/pdf",
    ".csv": "text/csv",
}


@dataclass(frozen=True)
class RawRow:
    number: int
    cells: tuple[str, ...]


@dataclass(frozen=True)
class RawTable:
    name: str
    rows: tuple[RawRow, ...]


@dataclass(frozen=True)
class RawDocument:
    parser: str
    tables: tuple[RawTable, ...]
    warnings: tuple[Notice, ...] = ()


class OrderParser(Protocol):
    name: str
    extensions: tuple[str, ...]

    def parse(self, path: Path) -> RawDocument: ...


def unreadable(kind: str, exc: Exception) -> InvalidRequest:
    return InvalidRequest(
        f"Файл {kind} не читается: {exc}", code="FILE_UNREADABLE", details={"parser": kind}
    )


class ExcelOrderParser:
    name = "excel"
    extensions = (".xlsx",)

    def parse(self, path: Path) -> RawDocument:
        tables: list[RawTable] = []
        try:
            with XlsxFile(path) as book:
                for sheet in book.sheet_names:
                    rows: list[RawRow] = []
                    for number, row in book.numbered_rows(sheet):
                        if not row:
                            continue
                        cells = [""] * (max(column_index(column) for column in row) + 1)
                        for column, value in row.items():
                            cells[column_index(column)] = value
                        rows.append(RawRow(number, tuple(cells)))
                    tables.append(RawTable(sheet, tuple(rows)))
        except (zipfile.BadZipFile, KeyError, ET.ParseError, OSError) as exc:
            raise unreadable(self.name, exc) from exc
        return RawDocument(self.name, tuple(tables))


class WordOrderParser:
    name = "word"
    extensions = (".docx",)

    def parse(self, path: Path) -> RawDocument:
        try:
            with zipfile.ZipFile(path) as package:
                root = ET.fromstring(package.read("word/document.xml"))
        except (zipfile.BadZipFile, KeyError, ET.ParseError, OSError) as exc:
            raise unreadable(self.name, exc) from exc
        tables: list[RawTable] = []
        for number, table in enumerate(root.iter(f"{W}tbl"), 1):
            rows = []
            for index, row in enumerate(table.findall(f"{W}tr"), 1):
                cells = tuple(_cell_text(cell) for cell in row.findall(f"{W}tc"))
                if any(cells):
                    rows.append(RawRow(index, cells))
            tables.append(RawTable(f"Таблица {number}", tuple(rows)))
        warnings = ()
        if not tables:
            warnings = (
                Notice("NO_TABLES", "В документе Word нет таблиц — позиции заказа не найдены."),
            )
        return RawDocument(self.name, tuple(tables), warnings)


class PdfOrderParser:
    name = "pdf"
    extensions = (".pdf",)

    def parse(self, path: Path) -> RawDocument:
        try:
            from pypdf import PdfReader
            from pypdf.errors import PdfReadError

            reader = PdfReader(str(path))
            pages = [page.extract_text(extraction_mode="layout") or "" for page in reader.pages]
        except (OSError, ValueError) as exc:
            raise unreadable(self.name, exc) from exc
        except PdfReadError as exc:
            raise unreadable(self.name, exc) from exc
        rows: list[RawRow] = []
        number = 0
        for text in pages:
            for line in text.splitlines():
                number += 1
                cells = tuple(cell for cell in _COLUMNS_GAP.split(line.strip()) if cell)
                if cells:
                    rows.append(RawRow(number, cells))
        warnings = ()
        if not rows:
            warnings = (
                Notice(
                    "PDF_NO_TEXT",
                    "В PDF нет текстового слоя (скан?) — распознавание не выполняется, "
                    "нужна ручная проверка.",
                ),
            )
        return RawDocument(self.name, (RawTable("PDF", tuple(rows)),), warnings)


class CsvOrderParser:
    name = "csv"
    extensions = (".csv",)

    def parse(self, path: Path) -> RawDocument:
        data = path.read_bytes()
        for encoding in ("utf-8-sig", "cp1251"):
            try:
                text = data.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:  # pragma: no cover — cp1251 декодирует любые байты
            raise unreadable(self.name, ValueError("неизвестная кодировка"))
        sample = text[:4096]
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=";,\t").delimiter
        except csv.Error:
            delimiter = ";"
        rows = [
            RawRow(number, tuple(cell.strip() for cell in row))
            for number, row in enumerate(csv.reader(io.StringIO(text), delimiter=delimiter), 1)
            if any(cell.strip() for cell in row)
        ]
        return RawDocument(self.name, (RawTable("CSV", tuple(rows)),))


PARSERS: tuple[OrderParser, ...] = (
    ExcelOrderParser(),
    WordOrderParser(),
    PdfOrderParser(),
    CsvOrderParser(),
)


def parser_for(filename: str) -> OrderParser:
    suffix = Path(filename).suffix.lower()
    for parser in PARSERS:
        if suffix in parser.extensions:
            return parser
    raise InvalidRequest(
        f"Файлы «{suffix or 'без расширения'}» не принимаются: нужен .xlsx, .docx, .pdf или .csv.",
        code="UNSUPPORTED_FILE_TYPE",
        details={"allowed": sorted(MEDIA_TYPES)},
    )


def _cell_text(cell: ET.Element) -> str:
    paragraphs = ["".join(t.text or "" for t in p.iter(f"{W}t")) for p in cell.iter(f"{W}p")]
    return " ".join(" ".join(paragraph.split()) for paragraph in paragraphs if paragraph.strip())
