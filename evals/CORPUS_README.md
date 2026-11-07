# Veltrix Systems — Synthetic RAG Practice Corpus

A fully fictional company knowledge base for practising Retrieval-Augmented Generation. Every person, company, address and number is invented; facts are **internally consistent across files** (e.g. an employee's manager, office and projects match across their profile, department page, project pages and meeting notes).

## Contents (651 Markdown documents)

| Folder | Files | What's inside |
|---|---|---|
| all-hands/ | 6 | All-hands meeting notes |
| case-studies/ | 8 | Customer success stories |
| company/ | 6 | Overview, history, values, leadership, org chart, funding |
| customers/ | 31 | Customer account pages |
| departments/ | 12 | One page per department with member table |
| employees/ | 189 | One profile per employee + directory |
| engineering/ | 22 | Architecture overview, on-call handbook, ADRs |
| finance/ | 11 | Quarterly business reports and annual summary |
| handbook/ | 9 | Onboarding, benefits, tools, holidays, glossary, sales playbook |
| incidents/ | 19 | Post-incident reviews |
| knowledge-base/ | 61 | Support KB articles per product |
| meeting-notes/ | 123 | Kickoffs, progress reviews, retrospectives |
| offices/ | 6 | One page per office |
| policies/ | 27 | HR, finance, security, engineering, legal policies (+ a superseded version) |
| press-releases/ | 12 | Public announcements |
| products/ | 72 | 12 products × overview, pricing, specs, release notes, FAQ, troubleshooting |
| projects/ | 37 | Project charters with teams, budgets, milestones |


## Evaluation set
`_eval/qa.jsonl` contains **488 question/answer pairs** with the source file(s) for each, so you can measure retrieval hit-rate and answer accuracy.

| Question type | Count |
|---|---|
| aggregation | 15 |
| multi-hop | 19 |
| multi-hop / versioning | 1 |
| single-hop | 451 |
| unanswerable | 2 |


## Built-in challenges
- **Versioned/conflicting info:** `policies/archive/` holds a superseded travel policy with different numbers — your system should prefer the current version.
- **Tables:** many facts live only in Markdown tables (pricing, metadata, budgets) — test your chunker.
- **Multi-hop:** e.g. project → lead → lead's manager → email.
- **Aggregation:** totals across quarterly reports, counts of departments/projects.
- **Near-duplicates:** KB articles share templates across products — retrieval must respect the product name.
- **Unanswerable questions:** the system should decline rather than hallucinate.

## Ideas for exercises
1. Compare chunking strategies (fixed-size vs. Markdown-header-aware).
2. Add metadata filters (folder, product, policy ID) and measure the gain.
3. Try hybrid search (BM25 + embeddings) on ID-heavy questions like "What did ADR-007 decide?".
4. Add a re-ranker and measure top-k recall on the eval set.

Generated 2026-11-04. Reference date for "current" status in the corpus: 04 Nov 2026.
