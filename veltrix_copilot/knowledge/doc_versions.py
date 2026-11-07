"""Old versions of a document: policies/archive/pol-fin-001-travel-policy-v1-3-superseded.md is replaced by
policies/pol-fin-001-travel-policy.md. Only explicit markers count (an archive/ folder, words like 'superseded',
or a version number), so quarterly reports or dated notes are never mistaken for versions of each other."""
from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

OLD_DIRS = {"archive", "archived", "old", "superseded"}
OLD_WORDS = {"superseded", "deprecated", "archived", "old", "obsolete"}
VERSION_RE = re.compile(r"(?:^|[\s_\-.])v(\d{1,3})(?:[\-_.](\d{1,3}))?(?=$|[\s_\-.])", re.I)
ASK_OLD_RE = re.compile(r"\b(superseded|archived|previous|old(er)?|earlier|former|prior)\b[^.?!]{0,30}\b(version|policy|edition)\b"
                        r"|\b(version|policy)\b[^.?!]{0,15}\b(before|prior to)\b", re.I)


def _parts(name: str) -> tuple[list[str], str]:
    p = PurePosixPath(name.replace("\\", "/").lower())
    return [d for d in p.parent.parts if d not in OLD_DIRS], p.stem


def is_old(name: str) -> bool:
    p = PurePosixPath(name.replace("\\", "/").lower())
    return bool(OLD_DIRS & set(p.parent.parts)) or bool(OLD_WORDS & set(re.findall(r"[a-z]+", p.stem)))


def version(name: str) -> tuple[int, int] | None:
    m = VERSION_RE.search(_parts(name)[1])
    return (int(m.group(1)), int(m.group(2) or 0)) if m else None


def family(name: str) -> str:
    """Folder (without archive/) + file name without version and 'superseded'-type words."""
    dirs, stem = _parts(name)
    stem = VERSION_RE.sub(" ", stem)
    words = [w for w in re.findall(r"[a-z0-9]+", stem) if w not in OLD_WORDS | {"final", "draft", "copy"}]
    return "/".join(dirs + [" ".join(words)])


def superseded(names: list[str], added: dict[str, float] | None = None) -> dict[str, str]:
    """{older document: the document that replaces it}. A family needs an explicit marker to count."""
    added = added or {}
    groups: dict[str, list[str]] = {}
    for n in names:
        groups.setdefault(family(n), []).append(n)
    out: dict[str, str] = {}
    for members in groups.values():
        if len(members) < 2 or not any(is_old(n) or version(n) for n in members):
            continue
        newest = max(members, key=lambda n: (not is_old(n), version(n) or (0, 0), added.get(n, 0.0)))
        out.update({n: newest for n in members if n != newest})
    return out


def asked_for(question: str, name: str) -> bool:
    """Does the question ask for this older version (its version number, or 'the previous/superseded policy')?"""
    v = version(name)
    if v and re.search(rf"\bv(?:ersion)?\s?{v[0]}(?:\.{v[1]})?\b", question, re.I) and (v[1] == 0 or f"{v[0]}.{v[1]}" in question):
        return True
    return bool(ASK_OLD_RE.search(question))


def skipped_for(question: str, names: list[str], added: dict[str, float] | None = None) -> dict[str, str]:
    """Older versions to leave out of search for this question: {older: newer}."""
    return {old: new for old, new in superseded(names, added).items() if not asked_for(question, old)}


def file_times(folder: Path, names: list[str]) -> dict[str, float]:
    """When each document file was added/changed (tie-breaker between versions)."""
    return {n: (folder / n).stat().st_mtime for n in names if (folder / n).exists()}
