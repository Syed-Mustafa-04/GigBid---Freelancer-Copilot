"""Check the numbers quoted in the docs against the result files they cite.

Why this exists: a README in this repo once described a trace it had never been
re-read against, and asserted content the committed log did not contain. The
fix for that was not to be more careful -- it was to stop checking by eye. This
script does for cited *numbers* what `check_cited_paths` does for cited paths.

It parses the markdown tables in ADR 0004 and compares every cell against
`evals/results/rag_variants.json` and `ablation.json`, allowing only rounding
difference. A number in prose that no longer matches its artefact is the exact
failure this project argues against, and it should break a check rather than
survive to a viva.

Run: python -m scripts.verify_report_numbers
Exit code 1 if any cited number disagrees with its source.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RESULTS = REPO / "evals" / "results"
ADR = REPO / "docs" / "adr" / "0004-rag-variant.md"

# Table label -> key in the results file.
VARIANTS = {
    "V7 HyDE": "v7_hyde",
    "V4 outcome-aware (ships)": "v4_outcome",
    "V2 hybrid": "v2_hybrid",
    "V6 contextual": "v6_contextual",
    "V3 cross-encoder rerank": "v3_rerank",
    "V5 multi-vector": "v5_multivector",
    "V0 naive dense": "v0_naive",
    "V1 requirement decomposition": "v1_decompose",
    "V9 skill graph": "v9_skillgraph",
    "V8 corrective RAG": "v8_corrective",
}
ABLATIONS = {
    "shipped (V4)": "shipped",
    "no_lexical": "no_lexical",
    "no_rrf": "no_rrf",
    "no_rerank": "no_rerank",
    "no_context": "no_context",
    "no_persona": "no_persona",
    "no_dense": "no_dense",
}


def _clean(cell: str) -> str:
    """Strip markdown emphasis and thousands separators from a table cell."""
    return cell.replace("**", "").replace(",", "").strip()


def _num(cell: str) -> float | None:
    c = _clean(cell)
    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*(ms)?", c)
    return float(m.group(1)) if m else None


def _rows(table_marker: str, text: str) -> list[list[str]]:
    """Return the data rows of the first markdown table after `table_marker`."""
    start = text.index(table_marker)
    rows = []
    for line in text[start:].splitlines():
        if not line.startswith("|"):
            if rows:
                break
            continue
        cells = [c for c in line.strip().strip("|").split("|")]
        if all(set(_clean(c)) <= set("-: ") for c in cells):
            continue  # separator row
        rows.append([_clean(c) for c in cells])
    return rows


def main() -> int:
    adr = ADR.read_text(encoding="utf-8")
    rag = {r["variant"]: r for r in json.loads((RESULTS / "rag_variants.json").read_text(encoding="utf-8"))["results"]}
    abl = {r["ablation"]: r for r in json.loads((RESULTS / "ablation.json").read_text(encoding="utf-8"))["results"]}

    problems: list[str] = []
    checked = 0

    def compare(label: str, metric: str, cited: float | None, actual: float | None, tol: float) -> None:
        nonlocal checked
        if cited is None or actual is None:
            return
        checked += 1
        if abs(cited - actual) > tol:
            problems.append(
                f"{label:32} {metric:14} doc says {cited:<8} artefact says {round(actual, 4)}"
            )

    # --- variant table: | Variant | Recall@5 | nDCG@10 | MRR | hit | pMRR | distinct | p95 |
    for row in _rows("| Variant | Recall@5 |", adr):
        key = VARIANTS.get(row[0])
        if not key or key not in rag:
            continue
        r = rag[key]
        compare(row[0], "recall@5", _num(row[1]), r["recall@5"], 0.0006)
        compare(row[0], "ndcg@10", _num(row[2]), r["ndcg@10"], 0.0006)
        compare(row[0], "mrr", _num(row[3]), r["mrr"], 0.0006)
        compare(row[0], "project_hit@5", _num(row[4]), r["project_hit@5"], 0.0006)
        compare(row[0], "project_mrr", _num(row[5]), r["project_mrr"], 0.0006)
        compare(row[0], "distinct@5", _num(row[6]), r["avg_distinct_projects_at_k"], 0.006)
        compare(row[0], "p95_ms", _num(row[7]), r["latency"]["p95_s"] * 1000, 1.0)

    # --- ablation table: | Ablation | Recall@5 | nDCG@10 | hit | p95 | delta |
    for row in _rows("| Ablation | Recall@5 |", adr):
        key = ABLATIONS.get(row[0])
        if not key or key not in abl:
            continue
        r = abl[key]
        compare(row[0], "recall@5", _num(row[1]), r["recall@5"], 0.0006)
        compare(row[0], "ndcg@10", _num(row[2]), r["ndcg@10"], 0.0006)
        compare(row[0], "project_hit@5", _num(row[3]), r["project_hit@5"], 0.0006)
        compare(row[0], "p95_ms", _num(row[4]), r["latency"]["p95_s"] * 1000, 1.0)
        if len(row) > 5 and (d := _num(row[5])) is not None:
            compare(row[0], "delta_recall", d, r["recall@5"] - abl["shipped"]["recall@5"], 0.0011)

    print(f"checked {checked} cited numbers against evals/results/")
    if problems:
        print(f"\n{len(problems)} MISMATCH(ES):\n")
        for p in problems:
            print(" ", p)
        return 1
    print("every number in ADR 0004 matches its artefact")
    return 0


if __name__ == "__main__":
    sys.exit(main())
