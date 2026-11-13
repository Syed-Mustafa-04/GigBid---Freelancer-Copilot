"""Calibrate the LLM judge against human labels (B4).

The assignment is explicit: any LLM judge must be calibrated, with Cohen's
kappa reported. The reason is that an uncalibrated judge is worse than no
judge — it produces a number that looks like evidence, gets into a table, and
nobody can tell it is noise.

So: 30 proposals are labelled by hand in `evals/datasets/judge_labels.json`,
the judge scores the same 30, and agreement is reported as

* **Cohen's kappa** on the binary "would this get a reply?" call, and
* **quadratic weighted kappa** on the 1–5 rubric, because on an ordinal scale
  being off by one is a different failure from being off by four.

Raw agreement is also printed, purely to show how misleading it is: on a
skewed label set two raters who both say "fine" most of the time agree ~80% of
the time by accident.

Interpretation thresholds used in the report (Landis & Koch):
<0.20 slight · 0.21–0.40 fair · 0.41–0.60 moderate · 0.61–0.80 substantial.
Below "moderate" the judge is not usable as evidence and the report says so.

Run: python -m evals.runners.judge_calibration
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from agent.config import REPO_ROOT
from agent.llm import RunLedger, get_llm_client
from agent.nodes.common import system_message
from evals.metrics import accuracy, cohens_kappa, quadratic_weighted_kappa

RESULTS = REPO_ROOT / "evals" / "results"
LABELS = REPO_ROOT / "evals" / "datasets" / "judge_labels.json"


class JudgeVerdict(BaseModel):
    specificity: int = Field(ge=1, le=5)
    grounding: int = Field(ge=1, le=5)
    relevance: int = Field(ge=1, le=5)
    concision: int = Field(ge=1, le=5)
    invitation: int = Field(ge=1, le=5)
    overall: int = Field(ge=1, le=5)
    would_get_reply: bool
    deciding_factor: str = ""


async def score_one(item: dict[str, Any], ledger: RunLedger) -> JudgeVerdict | None:
    client = get_llm_client()
    if client.is_offline:
        return None
    user = (
        f"JOB POST:\n{item.get('job_post', '')[:1200]}\n\n"
        f"PROPOSAL:\n{item['proposal']}\n\n"
        "Score it against the rubric."
    )
    try:
        verdict, _ = await client.structured(
            "judge", [system_message("judge"), {"role": "user", "content": user}],
            JudgeVerdict, ledger=ledger,
        )
        return verdict
    except Exception:  # noqa: BLE001
        return None


async def main() -> None:
    parser = argparse.ArgumentParser(description="Calibrate the LLM judge")
    parser.add_argument("--out", type=Path, default=RESULTS / "judge_calibration.json")
    args = parser.parse_args()

    if not LABELS.exists():
        print(
            f"No hand labels at {LABELS}.\n"
            "Run `python -m scripts.make_judge_labels` to generate the labelling sheet, "
            "label the 30 proposals yourself, then re-run this."
        )
        return

    items = json.loads(LABELS.read_text(encoding="utf-8"))
    client = get_llm_client()
    ledger = RunLedger(max_tokens=10**7, max_usd=5.0)

    if client.is_offline:
        payload = {
            "generated_at": datetime.now(UTC).isoformat(),
            "provider": "offline_stub",
            "status": "skipped",
            "reason": (
                "Calibration requires a real model. The offline provider returns "
                "deterministic placeholder scores, and agreement between human labels "
                "and a hash function is not a calibration - it is noise with a kappa "
                "attached. Set OPEN_LLM_API_KEY and re-run."
            ),
            "n_labelled": len(items),
        }
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(payload["reason"])
        return

    human_binary: list[str] = []
    judge_binary: list[str] = []
    human_overall: list[int] = []
    judge_overall: list[int] = []
    rows: list[dict[str, Any]] = []

    print(f"scoring {len(items)} hand-labelled proposals…")
    for item in items:
        verdict = await score_one(item, ledger)
        if verdict is None:
            continue
        human_binary.append("reply" if item["human_would_reply"] else "no_reply")
        judge_binary.append("reply" if verdict.would_get_reply else "no_reply")
        human_overall.append(int(item["human_overall"]))
        judge_overall.append(verdict.overall)
        rows.append({
            "id": item.get("id"),
            "human_overall": item["human_overall"],
            "judge_overall": verdict.overall,
            "human_would_reply": item["human_would_reply"],
            "judge_would_reply": verdict.would_get_reply,
            "deciding_factor": verdict.deciding_factor[:160],
            "disagreement": abs(int(item["human_overall"]) - verdict.overall),
        })

    kappa_binary = cohens_kappa(human_binary, judge_binary)
    kappa_rubric = quadratic_weighted_kappa(human_overall, judge_overall)
    raw = accuracy(judge_binary, human_binary)

    def band(value: float) -> str:
        if value < 0.20:
            return "slight - the judge is not usable as evidence"
        if value < 0.41:
            return "fair - too weak to quote as a quality measure"
        if value < 0.61:
            return "moderate - usable with the caveat stated"
        if value < 0.81:
            return "substantial"
        return "almost perfect"

    worst = sorted(rows, key=lambda r: -r["disagreement"])[:5]

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "provider": "live_model",
        "n": len(rows),
        "cohens_kappa_binary": kappa_binary,
        "cohens_kappa_interpretation": band(kappa_binary),
        "quadratic_weighted_kappa_rubric": kappa_rubric,
        "qwk_interpretation": band(kappa_rubric),
        "raw_agreement_binary": raw,
        "raw_agreement_caveat": (
            "Raw agreement is shown only to demonstrate how misleading it is. On a skewed "
            "label set two raters who mostly say the same thing agree by accident; kappa "
            "corrects for that and is the number to read."
        ),
        "usable_as_evidence": kappa_binary >= 0.41,
        "biggest_disagreements": worst,
        "rows": rows,
        "tokens": ledger.snapshot(),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(f"\nn = {len(rows)}")
    print(f"Cohen's kappa (binary reply) : {kappa_binary:.3f}  [{band(kappa_binary)}]")
    print(f"QWK (1-5 rubric)             : {kappa_rubric:.3f}  [{band(kappa_rubric)}]")
    print(f"raw agreement                : {raw:.3f}  (misleading; see caveat)")
    print(
        "\nverdict: "
        + (
            "the judge agrees with me often enough to quote, with the kappa stated."
            if payload["usable_as_evidence"]
            else "the judge does NOT agree with me well enough to be quoted as a quality "
                 "measure. The report must rely on GigSim reply rate and the claim "
                 "verifier instead."
        )
    )
    print(f"written: {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
