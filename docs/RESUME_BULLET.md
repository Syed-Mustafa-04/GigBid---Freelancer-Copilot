# Résumé bullet

Required portfolio deliverable: two lines, numbers taken from
`evals/results/`. Every figure below is checked against its artefact by
`scripts/verify_report_numbers.py`, which runs as a unit test.

---

**GigBid — multi-agent freelance copilot (LangGraph, MCP, FastAPI, Next.js)**

- Built a 10-agent LangGraph system with a human approval gate and exactly-once
  submission; chose the retrieval design by measuring 10 RAG variants over 90
  gold-labelled jobs, shipping hybrid outcome-aware retrieval at **1.000 project
  hit@5 and 484 ms p95** — then published an ablation that **contradicted** that
  choice rather than hiding it.
- Showed that prompt-injection **detection does not generalise**: a pattern
  scanner scored **8/8 on its own attack set and 0/8 on a held-out set**, and
  Meta's Prompt Guard 2 also scored **0/8**; re-architected to per-agent tool
  allowlists plus an approval token, **blocking 6/6 attempts to reach a write
  tool**.

---

Shorter single-line variant, if space is tight:

- Built a 10-agent LangGraph freelance copilot with a human approval gate and
  exactly-once submission; measured 10 RAG variants over 90 labelled jobs, and
  showed injection detection scoring 8/8 on its own tests but **0/8 held out**
  — including a purpose-trained classifier — then blocked **6/6** write attempts
  by capability denial instead.
