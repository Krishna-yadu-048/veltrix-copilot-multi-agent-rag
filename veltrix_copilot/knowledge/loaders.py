"""Load text from MD / TXT / PDF / DOCX files, keeping where each part came from."""
from __future__ import annotations

import re
from pathlib import Path

SUPPORTED = {".md", ".txt", ".pdf", ".docx"}

Segment = tuple[str, dict]   # (text, location metadata)


def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace(" ", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _markdown_segments(text: str) -> list[Segment]:
    segments, title, buf = [], None, []
    for line in text.splitlines():
        m = re.match(r"^#{2,3}\s+(.+?)\s*#*\s*$", line)
        if m:
            if "".join(buf).strip():
                segments.append(("\n".join(buf), {"section": title} if title else {}))
            title, buf = m.group(1).strip(), [line]
        else:
            buf.append(line)
    if "".join(buf).strip():
        segments.append(("\n".join(buf), {"section": title} if title else {}))
    return [(_clean(t), meta) for t, meta in segments if _clean(t)]


def _docx_segments(path: Path) -> list[Segment]:
    import docx

    d = docx.Document(str(path))
    segments, title, buf = [], None, []
    for p in d.paragraphs:
        style = (p.style.name if p.style is not None else "") or ""
        if style.lower().startswith(("heading", "title")) and p.text.strip():
            if "".join(buf).strip():
                segments.append(("\n".join(buf), {"section": title} if title else {}))
            title, buf = p.text.strip(), [p.text]
        else:
            buf.append(p.text)
    if "".join(buf).strip():
        segments.append(("\n".join(buf), {"section": title} if title else {}))
    rows = [" | ".join(cell.text for cell in row.cells) for table in d.tables for row in table.rows]
    if rows:
        segments.append(("\n".join(rows), {"section": "Tables"}))
    return [(_clean(t), meta) for t, meta in segments if _clean(t)]


def load_segments(path: Path) -> list[Segment]:
    suffix = path.suffix.lower()
    if suffix in {".md", ".txt"}:
        return _markdown_segments(path.read_text(encoding="utf-8"))
    if suffix == ".pdf":
        from pypdf import PdfReader

        pages = [(_clean(page.extract_text() or ""), {"page": i}) for i, page in enumerate(PdfReader(str(path)).pages, 1)]
        return [(t, meta) for t, meta in pages if t]
    if suffix == ".docx":
        return _docx_segments(path)
    raise ValueError(f"Unsupported file type: {path.name}")


def load_file(path: Path) -> str:
    return _clean("\n\n".join(text for text, _ in load_segments(path)))


def supported(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in SUPPORTED and not path.name.startswith(".")
