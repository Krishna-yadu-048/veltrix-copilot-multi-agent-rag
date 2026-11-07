"""Documents found in code before vector search: collection counts, fiscal quarters, IDs (offline, files only)."""
from veltrix_copilot.core.config import settings
from veltrix_copilot.knowledge import lookup
from veltrix_copilot.knowledge.ingest import doc_name, document_files

NAMES = [doc_name(p) for p in document_files()]


def found(question: str) -> lookup.Found:
    return lookup.find(question, NAMES, settings.docs_dir,
                       lambda n: [{"doc_name": n, "chunk_index": 0, "content": "", "distance": 0.0}])


def first_line(question: str) -> str:
    return found(question).chunks[0]["content"].splitlines()[0]


def test_counts_come_from_the_whole_index_filtered_in_code():
    assert first_line("How many incidents have happened in 2025?") == "Incident Index: 9 of 18 entries match (2025)."
    assert "1 of 18 entries match (2025, sev1)" in first_line("How many SEV1 incidents were there in 2025?")
    assert "3 of 18 entries match (climatrack)" in first_line("Which incidents affected ClimaTrack?")
    assert "3 of 30 entries match (germany)" in first_line("How many customers are in Germany?")
    assert "3 of 36 entries match (cancelled)" in first_line("How many projects were cancelled?")


def test_folders_without_an_index_are_listed_from_their_files():
    f = found("How many press releases were published in 2025?")
    assert f.names() == {"press-releases/_listing"} and "2 of 12 entries match (2025)" in f.chunks[0]["content"]


def test_breakdown_per_category_is_computed():
    text = found("How many incidents happened in 2025?").chunks[0]["content"]
    assert "By Severity: SEV2 6, SEV3 2, SEV1 1" in text


def test_quarters_map_to_the_fiscal_report_with_the_months():
    f = found("Give me a report of the company's performance during the first quarter of 2025.")
    assert f.names() == {"finance/quarterly-reports/fy2025-q1.md"}
    assert "FY2025 Q1 covers April – June 2024" in f.note and "January – March 2025, ask about FY2025 Q4" in f.note
    assert found("How did we do in Q3 FY2026?").names() == {"finance/quarterly-reports/fy2026-q3.md"}
    assert "If you meant" not in found("Results for FY2026 Q3").note       # said 'FY': no calendar hint
    assert lookup.quarter_named("Q2 2026") == (2026, 2, False)
    assert "no report for FY2031 Q1" in found("Q1 2031 results").note


def test_documents_named_by_id_or_file_name():
    assert found("What happened in INC-2025-0042?").names() == {"incidents/inc-2025-0042.md"}
    assert found("Summarise fy2025-q1.md").names() == {"finance/quarterly-reports/fy2025-q1.md"}


def test_ordinary_questions_are_left_to_search():
    for q in ["How many sick days does the leave policy allow?", "What is the hotel cap in Singapore?",
              "Who leads project Kestrel?"]:
        assert not found(q).chunks, q
