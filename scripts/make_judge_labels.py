"""Build the 30-proposal labelling sheet that the judge is calibrated against.

``evals/runners/judge_calibration.py`` compares an LLM judge against human
labels and reports Cohen's kappa. It can only do that if a human has actually
labelled something, so this script produces the sheet to label:
``evals/datasets/judge_labels.json``, with ``human_would_reply`` and
``human_overall`` left as ``null`` for a person to fill in.

**The labels are deliberately NOT auto-filled from the stored outcome.** Each
past proposal in the corpus already carries a won/replied/ignored label, and
copying it into ``human_would_reply`` would be trivial to do and worthless to
report: the corpus generator produced that outcome from phrasing features, so
an agreement score would be measuring the judge against the generator's rules
with a human nowhere in the loop. The kappa would look like calibration and
would be circular. The outcome is therefore carried only as
``_hint_actual_outcome``, after the labelling decision, as something to argue
with rather than copy.

Where the material comes from: the synthetic past proposals in
``data/processed/evidence.json`` (``type == "proposal"``), each paired with a
plausible job post reconstructed from the project it was written for in
``data/processed/projects.json``. The sample is stratified across the three
outcomes, because the corpus is roughly 59% ignored and an unstratified draw
would hand the labeller a sheet where "no" is almost always right - on which
two raters agree by accident and kappa is unstable.

Identical proposal texts are dropped before sampling. The generator produces
some repeats, and labelling the same letter twice inflates agreement without
adding information.

Run:
    python -m scripts.make_judge_labels
    python -m scripts.make_judge_labels --n 45 --seed 7
"""

from __future__ import annotations

import argparse
import json
import random
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
PROCESSED = REPO_ROOT / "data" / "processed"

OUTCOMES = ("won", "replied", "ignored")


def job_post_from(project: dict[str, Any]) -> str:
    """Reconstruct the post a client would have written for this project.

    The corpus stores the engagement from the freelancer's side; the judge is
    asked whether a proposal answers a *job post*, so one is rebuilt here in
    the shape and register of the live postings in ``jobs_live.json``. It is
    reconstructed rather than invented so the proposal and the post are about
    the same work - otherwise every proposal would look irrelevant and the
    labels would measure the reconstruction, not the proposal.
    """
    skill = project.get("primary_skill", "the work")
    industry = project.get("industry", "an unnamed")
    supporting = [s for s in project.get("supporting_skills", []) if s]
    weeks = int(project.get("duration_weeks", 4) or 4)
    budget = float(project.get("budget_usd", 0.0) or 0.0)
    low, high = round(budget * 0.8), round(budget * 1.15)

    lines = [
        f"We are a {industry} company looking for help with {skill}.",
        "",
        f"- Deliver {skill} to production quality",
    ]
    lines += [f"- Working knowledge of {s} is needed" for s in supporting[:2]]
    lines += [
        "- Hand over a reproducible repository and a short walkthrough",
        f"- Roughly {weeks} weeks of work",
        "",
        f"Budget: ${low:,.0f} - ${high:,.0f} USD (fixed price)",
        f"Skills: {', '.join([skill] + supporting[:2])}",
    ]
    title = f"{skill.title()} for a {industry} team"
    return f"{title}\n\n" + "\n".join(lines)


def stratified_sample(
    pool: list[dict[str, Any]], n: int, rng: random.Random
) -> list[dict[str, Any]]:
    """Equal-sized draws per outcome, topped up from the largest stratum.

    Equal rather than proportional: the point of the sheet is to exercise the
    judge on proposals that did and did not work, and a proportional draw from
    this corpus would be two thirds ignored.
    """
    by_outcome: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in pool:
        by_outcome[item.get("outcome", "unknown")].append(item)

    picked: list[dict[str, Any]] = []
    per = max(1, n // len(OUTCOMES))
    for outcome in OUTCOMES:
        group = by_outcome.get(outcome, [])
        rng.shuffle(group)
        picked.extend(group[:per])

    # Top up from whatever is left if n is not divisible by three, or if one
    # stratum is short. Which stratum fills the gap is recorded in the counts
    # the script prints, so a skewed sheet is visible rather than silent.
    if len(picked) < n:
        chosen = {item["evidence_id"] for item in picked}
        remainder = [item for item in pool if item["evidence_id"] not in chosen]
        rng.shuffle(remainder)
        picked.extend(remainder[: n - len(picked)])

    rng.shuffle(picked)
    return picked[:n]


def carry_over_labels(path: Path, items: list[dict[str, Any]]) -> int:
    """Keep any labels already filled in, so re-running never destroys work.

    With the same seed the sample is identical, so this makes the script safe
    to re-run after the sheet has been partly labelled - which is exactly when
    someone will want to re-run it.
    """
    if not path.exists():
        return 0
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return 0

    by_evidence = {
        row.get("evidence_id"): row for row in existing if isinstance(row, dict)
    }
    kept = 0
    for item in items:
        previous = by_evidence.get(item["evidence_id"])
        if not previous:
            continue
        if previous.get("human_would_reply") is None and previous.get("human_overall") is None:
            continue
        item["human_would_reply"] = previous.get("human_would_reply")
        item["human_overall"] = previous.get("human_overall")
        kept += 1
    return kept


def build(n: int, seed: int) -> list[dict[str, Any]]:
    evidence = json.loads((PROCESSED / "evidence.json").read_text(encoding="utf-8"))
    projects = {
        p["project_id"]: p
        for p in json.loads((PROCESSED / "projects.json").read_text(encoding="utf-8"))
    }

    seen_text: set[str] = set()
    pool: list[dict[str, Any]] = []
    for item in evidence:
        if item.get("type") != "proposal":
            continue
        if item.get("project_id") not in projects:
            continue
        text = item.get("text", "").strip()
        if not text or text in seen_text:
            continue
        seen_text.add(text)
        pool.append(item)

    rng = random.Random(seed)
    sample = stratified_sample(pool, n, rng)

    rows: list[dict[str, Any]] = []
    for index, item in enumerate(sample, start=1):
        project = projects[item["project_id"]]
        rows.append({
            "id": f"judge_{index:02d}",
            "evidence_id": item["evidence_id"],
            "persona_id": item.get("persona_id", ""),
            "project_id": item["project_id"],
            "job_post": job_post_from(project),
            "proposal": item["text"],
            # Fill these in by hand. Nothing in this file may write them.
            "human_would_reply": None,
            "human_overall": None,
            "_hint_actual_outcome": item.get("outcome", ""),
            "_hint_warning": (
                "Read this AFTER deciding. Copying it into the human fields makes the "
                "calibration circular and the resulting kappa meaningless."
            ),
        })
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate the hand-labelling sheet for judge calibration"
    )
    parser.add_argument("--n", type=int, default=30)
    parser.add_argument("--seed", type=int, default=20261115)
    parser.add_argument(
        "--out", type=Path, default=REPO_ROOT / "evals" / "datasets" / "judge_labels.json"
    )
    args = parser.parse_args()

    rows = build(args.n, args.seed)
    kept = carry_over_labels(args.out, rows)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")

    counts = {
        outcome: sum(1 for r in rows if r["_hint_actual_outcome"] == outcome)
        for outcome in OUTCOMES
    }
    unlabelled = sum(1 for r in rows if r["human_would_reply"] is None)

    print(f"wrote {len(rows)} proposals to {args.out}")
    print(f"  outcome mix (hidden from the labelling decision): {counts}")
    if kept:
        print(f"  kept {kept} label(s) already filled in from the previous sheet")
    print(
        f"\nNEXT: open the file and fill in {unlabelled} entr"
        f"{'y' if unlabelled == 1 else 'ies'} yourself.\n"
        "  human_would_reply : true or false  - would a client reply to this proposal?\n"
        "  human_overall     : 1 to 5         - overall quality against the same rubric\n"
        "                                       the judge uses (specificity, grounding,\n"
        "                                       relevance, concision, invitation).\n"
    )
    print(
        "Do NOT copy '_hint_actual_outcome' into those fields. The corpus generated that\n"
        "outcome from phrasing rules, so labelling from it would calibrate the judge\n"
        "against the generator rather than against a person, and the kappa would be\n"
        "circular. Decide first; the hint is there to disagree with afterwards.\n"
    )
    print("Then run: python -m evals.runners.judge_calibration")


if __name__ == "__main__":
    main()
