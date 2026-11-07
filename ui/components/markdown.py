"""Markdown helpers for the Streamlit UI."""
from __future__ import annotations

import re


def md(text: str | None) -> str:
    """Markdown with literal dollar signs. Streamlit treats text between two '$' as a LaTeX formula, so
    "Within your $1,500 budget ($229 left)" would lose its dollar signs and spacing. Escaped once only."""
    return re.sub(r"(?<!\\)\$", r"\\$", text or "")
