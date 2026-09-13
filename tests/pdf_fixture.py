"""PDF с кириллическим текстом без внешних библиотек — для теста разбора PDF.

Стандартный шрифт Courier с кодировкой /Differences: коды 128+ получают имена
глифов Adobe (afii10017 = «А»), по ним pypdf восстанавливает Юникод. Моноширинный
шрифт сохраняет колонки, разделённые пробелами.
"""

from __future__ import annotations

import io


def _glyph_names() -> dict[str, str]:
    from pypdf._codecs import adobe_glyphs

    glyphs = adobe_glyphs if isinstance(adobe_glyphs, dict) else adobe_glyphs.adobe_glyphs
    return {char: name for name, char in glyphs.items() if len(char) == 1}


def make_pdf(lines: list[str]) -> bytes:
    names = _glyph_names()
    chars = sorted({char for line in lines for char in line if ord(char) > 126})
    codes = {char: 128 + index for index, char in enumerate(chars)}

    def encode(text: str) -> str:
        out = []
        for char in text:
            code = codes.get(char, ord(char))
            if char in "()\\":
                out.append("\\" + char)
            elif code > 126:
                out.append(f"\\{code:03o}")
            else:
                out.append(char)
        return "".join(out)

    commands = ["BT /F1 9 Tf"]
    for number, line in enumerate(lines):
        commands.append(f"1 0 0 1 30 {800 - 14 * number} Tm ({encode(line)}) Tj")
    commands.append("ET")
    stream = "\n".join(commands).encode("latin-1")
    differences = " ".join(names[char] for char in chars)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 842 842] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        (
            "<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding << /Type /Encoding "
            f"/BaseEncoding /WinAnsiEncoding /Differences [128 {differences}] >> >>"
        ).encode("latin-1"),
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % number + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref))
    return out.getvalue()
