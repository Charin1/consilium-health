"""
Minimal PDF renderer for plain-text consultant reports.
Produces a standards-compliant single-page PDF using built-in Helvetica font.
"""
from __future__ import annotations

from typing import Iterable, List


PAGE_WIDTH = 612
PAGE_HEIGHT = 792
MARGIN_LEFT = 50
TOP_Y = 760
LINE_HEIGHT = 14


def _escape_pdf_text(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def _wrap_text_line(text: str, max_chars: int) -> List[str]:
    words = text.split()
    if not words:
        return [""]

    lines: List[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if len(candidate) <= max_chars:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _normalize_lines(lines: Iterable[str]) -> List[str]:
    normalized: List[str] = []
    for line in lines:
        wrapped = _wrap_text_line(line, max_chars=92)
        normalized.extend(wrapped)
    return normalized


def render_text_pdf(title: str, lines: Iterable[str]) -> bytes:
    """Render a simple single-page text PDF."""
    final_lines = _normalize_lines([title, "", *lines])
    max_visible_lines = int((TOP_Y - 40) / LINE_HEIGHT)
    final_lines = final_lines[:max_visible_lines]

    stream_lines = ["BT", "/F1 11 Tf", f"{MARGIN_LEFT} {TOP_Y} Td"]
    for index, line in enumerate(final_lines):
        escaped = _escape_pdf_text(line)
        if index > 0:
            stream_lines.append(f"0 -{LINE_HEIGHT} Td")
        stream_lines.append(f"({escaped}) Tj")
    stream_lines.append("ET")

    stream_data = "\n".join(stream_lines).encode("latin-1", errors="replace")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] "
            "/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>"
        ).encode("ascii"),
        b"<< /Length " + str(len(stream_data)).encode("ascii") + b" >>\nstream\n" + stream_data + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    buffer = bytearray()
    buffer.extend(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")

    offsets = [0]
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(buffer))
        buffer.extend(f"{i} 0 obj\n".encode("ascii"))
        buffer.extend(obj)
        buffer.extend(b"\nendobj\n")

    xref_start = len(buffer)
    total_objects = len(objects) + 1
    buffer.extend(f"xref\n0 {total_objects}\n".encode("ascii"))
    buffer.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        buffer.extend(f"{offset:010d} 00000 n \n".encode("ascii"))

    buffer.extend(
        (
            f"trailer\n<< /Size {total_objects} /Root 1 0 R >>\n"
            f"startxref\n{xref_start}\n%%EOF\n"
        ).encode("ascii")
    )

    return bytes(buffer)
