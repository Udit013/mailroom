"""Builds small real PDFs for tests. No test PDF is checked into the repository."""

import io

from pypdf import PdfReader, PdfWriter


def _pdf(content: bytes) -> bytes:
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    start = len(output)
    output += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        output += b"%010d 00000 n \n" % offset
    output += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        start,
    )
    return bytes(output)


def text_pdf(*lines: str) -> bytes:
    commands = [b"BT /F1 12 Tf 14 TL 72 720 Td"]
    for line in lines:
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        commands.append(b"(" + escaped.encode("latin-1") + b") Tj T*")
    commands.append(b"ET")
    return _pdf(b"\n".join(commands))


def blank_pdf() -> bytes:
    return _pdf(b"")


def encrypted_pdf(*lines: str) -> bytes:
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(text_pdf(*lines))))
    writer.encrypt("secret", algorithm="RC4-128")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()
