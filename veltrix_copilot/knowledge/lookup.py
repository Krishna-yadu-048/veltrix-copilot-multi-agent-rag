"""Documents a question points at directly, found in code before (and in addition to) vector search.

Vector search returns the few passages most similar to the question. That fails for three kinds of question,
which this module answers from the files themselves:

1. Counting / listing a collection: "How many incidents happened in 2025?" needs every incident, not the top 4
   passages. The collection's index table (incidents/index.md) is read whole, filtered to the year and values
   the question names (SEV1, ClimaTrack, India…), and counted in code; the model then reads the result.
2. Fiscal quarters: "the first quarter of 2025" -> finance/quarterly-reports/fy2025-q1.md. Veltrix's fiscal
   year runs April-March, so the answer also says which months that quarter covers.
3. Documents named by ID or file name: "INC-2025-0042", "POL-FIN-001", "fy2025-q1.md".
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

# folder -> (words that name the collection, its index file or None = list the folder's files)
COLLECTIONS: dict[str, tuple[str, str | None]] = {
    "incidents": (r"incidents|outages|post-?incident reviews", "incidents/index.md"),
    "customers": (r"customers|clients|customer accounts", "customers/index.md"),
    "policies": (r"policies", "policies/index.md"),
    "projects": (r"projects", "projects/portfolio.md"),
    "meeting-notes": (r"meetings|meeting notes|kickoffs|retrospectives|retros", "meeting-notes/index.md"),
    "knowledge-base": (r"kb articles|knowledge[- ]base articles|support articles", "knowledge-base/index.md"),
    "press-releases": (r"press releases|announcements", None),
    "case-studies": (r"case studies", None),
    "offices": (r"offices", None),
    "all-hands": (r"all-hands(?: meetings)?|all hands(?: meetings)?", None),
}
COUNT_RE = re.compile(r"\b(how many|number of|count|list|which|what are|show( me)?|all|total|per|each|most|least|"
                      r"fewest|breakdown|by (product|severity|status|country|industry|year|type|owner))\b", re.I)
YEAR_RE = re.compile(r"\b(20\d\d)\b")
MAX_ROWS = 60                     # rows passed to the model; the counts always cover every matching row
LISTING = "_listing"              # doc name suffix of a folder listing built from file names

ORDINAL = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4, "last": 4}
_FISCAL = r"(?:(fy|fiscal(?:\s+year)?)\s*'?)?"
QUARTER_RES = [
    # FY2025 Q1, FY25-Q1, fy2025-q1(.md), fiscal 2025 Q1
    re.compile(r"\b(fy|fiscal(?:\s+year)?)\s*'?(\d{2}|\d{4})\s*[- ]?\s*q([1-4])\b", re.I),
    # Q1 2025, Q1 FY2025, Q1 of 2025
    re.compile(r"\bq([1-4])\s*(?:of\s+)?" + _FISCAL + r"(\d{4}|\d{2})\b", re.I),
    # first quarter of 2025, 2nd quarter of FY2026
    re.compile(r"\b(first|1st|second|2nd|third|3rd|fourth|4th|last)\s+quarter\s+(?:of\s+)?(?:the\s+)?"
               + _FISCAL + r"(\d{4})\b", re.I),
]
FISCAL_MONTHS = {1: "April – June", 2: "July – September", 3: "October – December", 4: "January – March"}
ID_RE = re.compile(r"\b((?:inc|pol|prj|adr|kb)-[a-z0-9]+(?:-[a-z0-9]+)*)\b", re.I)
FILE_RE = re.compile(r"\b([\w-]+\.(?:md|pdf|docx|txt))\b", re.I)


@dataclass
class Found:
    chunks: list[dict] = field(default_factory=list)   # passages to put first in the model's context
    note: str = ""                                     # shown under the answer (e.g. which months a quarter is)

    def names(self) -> set[str]:
        return {c["doc_name"] for c in self.chunks}


def _chunk(doc_name: str, content: str, section: str = "") -> dict:
    return {"doc_name": doc_name, "chunk_index": 0, "content": content, "distance": 0.0, "page": None,
            "section": section or None}


# ------------------------------------------------------------------ 1. collections
def _cells(line: str) -> list[str]:
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    return [re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", c) for c in cells]   # "[Name](file.md)" -> "Name"


def read_table(path: Path) -> tuple[str, list[str], list[list[str]]]:
    """(title, header, rows) of the first Markdown table in a file."""
    title, header, rows = path.stem, [], []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# ") and not header:
            title = line[2:].strip()
        elif line.lstrip().startswith("|"):
            cells = _cells(line)
            if not header:
                header = cells
            elif not all(set(c) <= set("-: ") for c in cells):
                rows.append(cells)
        elif header and rows:
            break
    return title, header, rows


def folder_table(docs_dir: Path, folder: str) -> tuple[str, list[str], list[list[str]]]:
    """A table built from the folder's files when it has no index: date (from the file name) and title."""
    rows = []
    for f in sorted((docs_dir / folder).rglob("*.md")):
        if f.name == "index.md" or any(p.startswith(".") for p in f.parts):
            continue
        first = next((ln[2:].strip() for ln in f.read_text(encoding="utf-8").splitlines() if ln.startswith("# ")), f.stem)
        m = re.match(r"(\d{4}-\d{2}-\d{2})", f.name)
        rows.append([m.group(1) if m else "", first, f.relative_to(docs_dir).as_posix()])
    return f"Documents in {folder}", ["Date", "Title", "File"], rows


def _mentioned_values(question: str, header: list[str], rows: list[list[str]]) -> dict[int, set[str]]:
    """Column -> cell values the question names, e.g. {Severity: {'sev1'}, Product: {'climatrack'}}."""
    q = question.lower()
    found: dict[int, set[str]] = {}
    for col in range(len(header)):
        values = {r[col].lower() for r in rows if col < len(r)}
        if len(values) > max(12, len(rows) // 2):          # IDs, names, dates: not a category to filter on
            continue
        for v in values:
            if len(v) >= 3 and not v.isdigit() and re.search(rf"(?<![\w-]){re.escape(v)}(?![\w-])", q):
                found.setdefault(col, set()).add(v)
    return found


def _breakdown(header: list[str], rows: list[list[str]]) -> list[str]:
    """Counts per category column (Severity, Product, Status…), computed in code."""
    out = []
    for col, name in enumerate(header):
        counts = Counter(r[col] for r in rows if col < len(r) and r[col])
        if 1 < len(counts) <= 12 and len(counts) < len(rows):
            out.append(f"By {name}: " + ", ".join(f"{v} {n}" for v, n in counts.most_common()))
    return out


def collection_listing(question: str, docs_dir: Path) -> dict | None:
    """The filtered, counted index of the collection a counting/listing question is about."""
    folder = next((f for f, (words, _) in COLLECTIONS.items()
                   if re.search(rf"\b(?:{words})\b", question, re.I)), None)
    years = set(YEAR_RE.findall(question))
    if not folder or not (COUNT_RE.search(question) or years):
        return None
    index = COLLECTIONS[folder][1]
    if index and (docs_dir / index).exists():
        doc_name, (title, header, rows) = index, read_table(docs_dir / index)
    else:
        doc_name, (title, header, rows) = f"{folder}/{LISTING}", folder_table(docs_dir, folder)
    if not rows:
        return None
    filters = _mentioned_values(question, header, rows)
    matching = [r for r in rows
                if (not years or any(y in c for c in r for y in years))
                and all(col < len(r) and r[col].lower() in vals for col, vals in filters.items())]
    named = sorted(years) + sorted(v for vals in filters.values() for v in vals)
    what = f"{len(matching)} of {len(rows)} entries match ({', '.join(named)})" if named else f"{len(rows)} entries in total"
    lines = [f"{title}: {what}.", *_breakdown(header, matching), "",
             "| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(r) + " |" for r in matching[:MAX_ROWS]]
    if len(matching) > MAX_ROWS:
        lines.append(f"(first {MAX_ROWS} of {len(matching)} shown)")
    return _chunk(doc_name, "\n".join(lines), section=title)


# ------------------------------------------------------------------ 2. fiscal quarters
def quarter_named(question: str) -> tuple[int, int, bool] | None:
    """(fiscal year, quarter, said 'FY'/'fiscal') for 'FY2025 Q1', 'Q1 2025', 'first quarter of 2025'."""
    for i, rx in enumerate(QUARTER_RES):
        if m := rx.search(question):
            if i == 0:
                fiscal, year, q = True, m.group(2), int(m.group(3))
            elif i == 1:
                q, fiscal, year = int(m.group(1)), bool(m.group(2)), m.group(3)
            else:
                q, fiscal, year = ORDINAL[m.group(1).lower()], bool(m.group(2)), m.group(3)
            year = int(year) + (2000 if len(year) == 2 else 0)
            return year, q, fiscal
    return None


def quarter_note(year: int, q: int, fiscal: bool) -> str:
    months = f"{FISCAL_MONTHS[q]} {year if q == 4 else year - 1}"
    note = f"_FY{year} Q{q} covers {months} (Veltrix's fiscal year runs April – March)."
    if not fiscal:
        cal_fy, cal_q = (year, 4) if q == 1 else (year + 1, q - 1)
        cal = {1: "January – March", 2: "April – June", 3: "July – September", 4: "October – December"}[q]
        note += f" If you meant {cal} {year}, ask about FY{cal_fy} Q{cal_q}."
    return note + "_"


# ------------------------------------------------------------------ 3. documents named by ID / file name
def named_documents(question: str, names: list[str]) -> list[str]:
    out: list[str] = []
    for m in FILE_RE.finditer(question):
        out += [n for n in names if n.lower().rsplit("/", 1)[-1] == m.group(1).lower()]
    for m in ID_RE.finditer(question):
        ident = m.group(1).lower()
        out += [n for n in names if re.search(rf"(^|/){re.escape(ident)}(\.|-|$)", n.lower())]
    return list(dict.fromkeys(out))


# ------------------------------------------------------------------ all together
def find(question: str, names: list[str], docs_dir: Path, document_chunks, skip: dict | None = None) -> Found:
    """`document_chunks(name)` returns a document's passages (from the database)."""
    found = Found()
    usable = [n for n in names if n not in (skip or {})]
    if listing := collection_listing(question, docs_dir):
        found.chunks.append(listing)
    docs = named_documents(question, usable)
    if quarter := quarter_named(question):
        year, q, fiscal = quarter
        report = f"finance/quarterly-reports/fy{year}-q{q}.md"
        if report in names:
            docs.insert(0, report)
            found.note = quarter_note(year, q, fiscal)
        else:
            have = sorted(n.rsplit("/", 1)[-1][:-3].upper().replace("-", " ") for n in names
                          if n.startswith("finance/quarterly-reports/"))
            found.note = (f"_There is no report for FY{year} Q{q}. Quarterly reports exist for "
                          f"{', '.join(have)}._" if have else "")
    for name in list(dict.fromkeys(docs))[:2]:
        found.chunks += document_chunks(name)
    return found
