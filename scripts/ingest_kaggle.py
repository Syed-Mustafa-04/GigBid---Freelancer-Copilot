"""Optional loader: layer real Upwork postings on top of the synthetic corpus.

Why this exists. The synthetic corpus carries the labels GigBid is evaluated
on - gold evidence, bid/skip decisions, reply outcomes - which no public
dataset has. What the public Kaggle scrapes do have is real market behaviour:
how clients actually word a post, what they actually budget, and what work
actually sold for. The Pricing Strategist bids against awarded-price
percentiles, and percentiles taken from generated numbers are only as
defensible as the generator. This script lets real ones replace them.

Three rules it follows, in order of importance:

1. **Real and generated data never mix in one file.** Every record written
   here carries ``"synthetic": false`` and ``"source": "kaggle:<dataset>"``,
   and real postings land in ``jobs_real.json``, beside the synthetic
   ``jobs_live.json`` rather than over it. A single file holding both would
   make every later claim about provenance unverifiable.
2. **Tolerate the data as it is.** Kaggle publishers rename and drop columns
   between versions, so a dataset is recognised by how well a CSV's headers
   overlap a known signature, not by exact match, and every field is optional.
   A row missing a budget is still a usable posting.
3. **Never invent a value.** A posting with one budget figure is recorded as a
   point, not widened into a plausible-looking range; a posting with no skills
   column gets an empty skill list and is counted in the summary. Gaps are
   reported rather than filled.

Nothing here is required. Everything in the repository runs without it, and
the datasets themselves are not redistributed - only this loader. Check the
licence on each dataset page before republishing anything derived from them.

Run:
    python -m scripts.ingest_kaggle --input-dir data/raw/kaggle
    python -m scripts.ingest_kaggle --input-dir data/raw/kaggle --limit 0
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# The same percentile definition the evaluation uses. Pricing bands computed
# with a different one would not be comparable with anything in evals/results.
from evals.metrics import percentile

REPO_ROOT = Path(__file__).resolve().parent.parent

# Long job descriptions overflow the stdlib default on some of these dumps.
csv.field_size_limit(10_000_000)

DOWNLOAD_HINTS: list[tuple[str, str]] = [
    ("asaniczka/upwork-job-postings-dataset-2024-50k-records", "postings, 2024, 50k"),
    ("asaniczka/all-jobs-on-upwork-200k-plus", "postings, 200k+"),
    ("ahmedmyalo/upwork-freelance-jobs-60k", "postings with skills, 60k"),
    ("asaniczka/freelance-contracts-dataset-1-3-million-entries", "awarded prices, 1.3M"),
]

# Known datasets, recognised by header overlap rather than by filename -
# people rename downloads, and the publishers reshuffle columns. ``kind``
# decides what a file is used for: postings become jobs, contracts become
# price statistics, and anything else is recognised and then left alone.
DATASETS: dict[str, dict[str, Any]] = {
    "upwork-postings-2024-50k": {
        "kind": "postings",
        "signature": {
            "title", "link", "published_date", "is_hourly",
            "hourly_low", "hourly_high", "budget", "country",
        },
    },
    "upwork-all-jobs-200k": {
        "kind": "postings",
        "signature": {
            "title", "description", "published_date", "is_hourly",
            "hourly_low", "hourly_high", "budget", "category",
        },
    },
    "upwork-freelance-jobs-60k": {
        "kind": "postings",
        "signature": {
            "job_title", "description", "ex_level_demand", "published_date",
            "payment_type", "duration", "budget", "technical_tool",
        },
    },
    "freelance-contracts-1.3m": {
        "kind": "contracts",
        "signature": {
            "title", "skills", "amount", "hourly_rate", "contract_type",
            "client_country", "feedback", "start_date",
        },
    },
    "freelancer-success-earnings-churn": {
        "kind": "freelancers",
        "signature": {
            "freelancer_id", "earnings", "churn", "job_success_score",
            "hourly_rate", "country",
        },
    },
}

# One concept, many spellings. Order matters: the first alias present wins.
ALIASES: dict[str, list[str]] = {
    "title": ["title", "job_title", "jobtitle", "name", "job_name", "project_title"],
    "description": [
        "description", "job_description", "details", "summary", "snippet", "job_details",
    ],
    "skills": [
        "skills", "required_skills", "technical_tool", "technologies", "tags",
        "skill", "category", "subcategory",
    ],
    "budget": ["budget", "budget_usd", "fixed_price", "price", "total_amount"],
    "budget_min": ["budget_min", "min_budget", "hourly_low", "rate_min", "min_price"],
    "budget_max": ["budget_max", "max_budget", "hourly_high", "rate_max", "max_price"],
    "hourly_flag": ["is_hourly", "hourly", "payment_type", "contract_type", "job_type", "type"],
    "posted_at": [
        "published_date", "posted_on", "posted_at", "date_posted", "created_at",
        "start_date", "date",
    ],
    "client": ["client_id", "client", "employer_id", "buyer_id", "company", "client_country"],
    "country": ["country", "client_country"],
    "proposals": [
        "applicants_num", "applicants", "proposals", "num_proposals", "total_applicants", "bids",
    ],
    "duration": ["duration", "project_length", "estimated_duration", "duration_days"],
    "experience": ["ex_level_demand", "experience_level", "experience", "level"],
    "awarded_amount": [
        "awarded_amount_usd", "awarded_amount", "amount", "contract_amount",
        "total_charge", "total_paid", "earnings",
    ],
    "hourly_rate": ["hourly_rate", "hourly_rate_usd", "rate", "avg_hourly_rate"],
}

SKILL_SPLIT = re.compile(r"[,;|/]+")
NUMBER = re.compile(r"-?\d+(?:[.,]\d+)?")
DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d", "%b %d, %Y", "%d %b %Y")


# ---------------------------------------------------------------------------
# Reading, whatever is installed
# ---------------------------------------------------------------------------
def normalise(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")


def read_header(path: Path) -> list[str]:
    """Headers come from the stdlib either way - it is one line and always works."""
    with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        reader = csv.reader(handle)
        for row in reader:
            return [normalise(cell) for cell in row]
    return []


def iter_rows(path: Path, limit: int) -> Iterator[dict[str, str]]:
    """Stream rows, with pandas if it is installed and the csv module if not.

    Both paths stream in chunks rather than loading the file: the contracts
    dump is 1.3M rows and a ``--limit 200`` run should not pay for the other
    1,299,800. pandas is preferred only because it is more forgiving of the
    quoting and encoding oddities in these scrapes - the fallback produces the
    same records, so nothing about the output depends on which one ran.
    """
    try:
        import pandas as pd
    except ImportError:
        pd = None

    yielded = 0
    if pd is not None:
        frames = pd.read_csv(
            path, dtype=str, keep_default_na=False, chunksize=5_000,
            encoding="utf-8", encoding_errors="replace", on_bad_lines="skip",
        )
        for frame in frames:
            frame.columns = [normalise(c) for c in frame.columns]
            for record in frame.to_dict("records"):
                yield {k: ("" if v is None else str(v)) for k, v in record.items()}
                yielded += 1
                if limit and yielded >= limit:
                    return
        return

    with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle)
        reader.fieldnames = [normalise(c) for c in (reader.fieldnames or [])]
        for record in reader:
            yield {k: (v or "") for k, v in record.items() if k}
            yielded += 1
            if limit and yielded >= limit:
                return


def detect_dataset(headers: list[str]) -> tuple[str, str, float]:
    """Best signature overlap wins; below half, the file is treated as unknown."""
    present = set(headers)
    best_name, best_kind, best_score = "unknown", "unknown", 0.0
    for name, spec in DATASETS.items():
        signature: set[str] = spec["signature"]
        score = len(present & signature) / len(signature)
        if score > best_score:
            best_name, best_kind, best_score = name, spec["kind"], score
    if best_score < 0.5:
        # Not a dataset we know, but if it has a title it is still a posting
        # file and worth reading. Being wrong here costs a tagged record, not
        # a corrupted corpus: the source field says where it came from.
        if "title" in present or "job_title" in present:
            return "unrecognised-postings", "postings", best_score
        return "unknown", "unknown", best_score
    return best_name, best_kind, best_score


# ---------------------------------------------------------------------------
# Field parsing - every one of these tolerates absence
# ---------------------------------------------------------------------------
def pick(row: dict[str, str], field: str) -> str:
    for alias in ALIASES.get(field, []):
        value = row.get(alias)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def to_float(text: str) -> float | None:
    if not text:
        return None
    match = NUMBER.search(text.replace(",", ""))
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def to_int(text: str) -> int:
    value = to_float(text)
    return int(value) if value is not None else 0


def split_skills(text: str) -> list[str]:
    if not text:
        return []
    parts = [p.strip(" '\"[]").lower() for p in SKILL_SPLIT.split(text)]
    return [p for p in parts if p and len(p) < 60][:12]


def parse_date(text: str) -> str:
    """ISO-8601 with a timezone, or empty. A guessed date is worse than none.

    A date with no time or offset is read as UTC rather than as local time.
    Converting it instead would shift every posting by the ingesting machine's
    offset, which silently moves a date-only value onto the previous day.
    """
    if not text:
        return ""
    cleaned = text.strip().replace("Z", "+00:00")
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        for fmt in DATE_FORMATS:
            try:
                parsed = datetime.strptime(text.strip(), fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return ""
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC).isoformat()
    return parsed.astimezone(UTC).isoformat()


def is_hourly(row: dict[str, str]) -> bool:
    flag = pick(row, "hourly_flag").lower()
    return flag in {"true", "1", "yes", "y", "hourly"} or "hour" in flag


def duration_days(text: str) -> int:
    """Map the free-text durations these dumps carry onto days.

    30 is the fallback because the median fixed-price engagement in the
    synthetic market is a month, so an unparseable duration does not shift the
    distribution in either direction.
    """
    lowered = text.lower()
    number = to_float(lowered)
    if "month" in lowered:
        return int((number or 1) * 30)
    if "week" in lowered:
        return int((number or 1) * 7)
    if "day" in lowered:
        return int(number or 1)
    if number and number > 0:
        return int(number)
    return 30


def connects_for(amount: float, hourly: bool) -> int:
    """Upwork's published Connects bands, applied to a figure the data has.

    This is a rule, not data: no posting in these scrapes records what it cost
    to apply. It is here so a real posting can be priced through the same
    expected-value-per-Connect arithmetic as a simulated one.
    """
    if hourly:
        return 8
    if amount <= 0:
        return 8
    if amount < 100:
        return 4
    if amount < 500:
        return 6
    if amount < 1_000:
        return 8
    if amount < 5_000:
        return 12
    return 16


def digest(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:10]


# ---------------------------------------------------------------------------
# Normalisation into the shape the rest of the system uses
# ---------------------------------------------------------------------------
def row_to_job(row: dict[str, str], dataset: str) -> tuple[dict[str, Any] | None, str]:
    """Return the normalised job, or ``None`` and the reason it was skipped."""
    title = pick(row, "title")
    if not title:
        return None, "no title column or empty title"

    hourly = is_hourly(row)
    low = to_float(pick(row, "budget_min"))
    high = to_float(pick(row, "budget_max"))
    single = to_float(pick(row, "budget"))
    if low is None and high is None and single is not None:
        # One figure stays one figure. Inventing a +/-20% band around it would
        # feed the pricing percentiles a spread that is not in the data.
        low = high = single
    if low is not None and high is not None and low > high:
        low, high = high, low

    client_key = pick(row, "client") or pick(row, "country")
    posted_at = parse_date(pick(row, "posted_at"))
    skills = split_skills(pick(row, "skills"))
    description = pick(row, "description") or title

    job: dict[str, Any] = {
        "job_id": f"kjob_{digest(dataset, title, posted_at, client_key)}",
        "title": title[:300],
        "description": description[:6_000],
        "required_skills": skills,
        "budget": {
            "min": round(low, 2) if low is not None else None,
            "max": round(high, 2) if high is not None else None,
            "currency": "USD",
            "type": "hourly" if hourly else "fixed",
        },
        "duration_days": duration_days(pick(row, "duration")),
        # Real postings carry no screening questions; an empty list is the
        # honest representation and the graph already handles it.
        "screening_questions": [],
        "connects_cost": connects_for(high or single or 0.0, hourly),
        "client_id": f"kcl_{digest(client_key)}" if client_key else "kcl_unknown",
        "posted_at": posted_at,
        "proposals_received": to_int(pick(row, "proposals")),
        "experience_level": pick(row, "experience").lower(),
        # The two fields that keep real and generated records apart everywhere
        # downstream. Nothing in this file writes synthetic: true.
        "synthetic": False,
        "source": f"kaggle:{dataset}",
    }
    return job, ""


def row_to_contract(row: dict[str, str]) -> dict[str, Any] | None:
    """A priced row: what the work actually sold for, which is the useful part."""
    amount = to_float(pick(row, "awarded_amount"))
    hourly_rate = to_float(pick(row, "hourly_rate"))
    if amount is None and hourly_rate is None:
        return None
    return {
        "skills": split_skills(pick(row, "skills")) or split_skills(pick(row, "title")),
        "awarded_amount_usd": amount,
        "awarded_hourly_usd": hourly_rate,
        "proposals_received": to_int(pick(row, "proposals")),
    }


def market_stats(contracts: list[dict[str, Any]], dataset: str) -> dict[str, Any]:
    """Awarded-price percentiles per skill, in the shape /market/stats returns.

    Deliberately the same keys as ``gigsim.world.market_stats``, so the
    Pricing Strategist can be pointed at real numbers without touching the
    node: it already reads ``awarded_p25``, ``awarded_median``,
    ``awarded_p75`` and the hourly equivalents.
    """
    amounts: dict[str, list[float]] = defaultdict(list)
    hourly: dict[str, list[float]] = defaultdict(list)
    proposals: dict[str, list[int]] = defaultdict(list)

    for contract in contracts:
        # "_all" is the fallback band for a job whose skill is not in the data,
        # which is the common case for a long-tail skill.
        keys = contract["skills"] + ["_all"]
        for key in keys:
            if contract["awarded_amount_usd"] is not None:
                amounts[key].append(contract["awarded_amount_usd"])
            if contract["awarded_hourly_usd"] is not None:
                hourly[key].append(contract["awarded_hourly_usd"])
            proposals[key].append(contract["proposals_received"])

    by_skill: dict[str, Any] = {}
    for skill in sorted(set(amounts) | set(hourly)):
        sample = amounts.get(skill, [])
        rates = hourly.get(skill, [])
        # Percentiles from a handful of rows are noise with decimal places.
        if len(sample) + len(rates) < 5:
            continue
        seen = proposals.get(skill, [])
        by_skill[skill] = {
            "skill": skill,
            "sample_size": max(len(sample), len(rates)),
            "awarded_p25": percentile(sample, 25),
            "awarded_median": percentile(sample, 50),
            "awarded_p75": percentile(sample, 75),
            "hourly_p25": percentile(rates, 25),
            "hourly_median": percentile(rates, 50),
            "hourly_p75": percentile(rates, 75),
            "avg_proposals_received": round(sum(seen) / len(seen), 1) if seen else 0.0,
        }

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "synthetic": False,
        "source": f"kaggle:{dataset}",
        "contracts_read": len(contracts),
        "skills_with_enough_data": len(by_skill),
        "minimum_rows_per_skill": 5,
        "note": (
            "Awarded prices, not asking budgets. GigSim still serves its synthetic "
            "statistics from jobs_historical.json; point the marketplace adapter or the "
            "Pricing Strategist at this file to bid against real ones."
        ),
        "by_skill": by_skill,
    }


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
def print_instructions(input_dir: Path) -> None:
    print(f"No CSV files found in {input_dir}.\n")
    print("This loader is optional - everything in GigBid runs without it. To use it:\n")
    print("  1. Install the data extras (pandas is optional, the Kaggle CLI is not):")
    print('       .venv/Scripts/python.exe -m pip install -e ".[data]"\n')
    print("  2. Authenticate the Kaggle CLI. It reads ~/.kaggle/kaggle.json, which you")
    print("     download from https://www.kaggle.com/settings -> API -> Create New Token.")
    print("     Without that file every `kaggle` command fails with a 401.\n")
    print("  3. Download one or more of these into the input directory:\n")
    for slug, what in DOWNLOAD_HINTS:
        print(f"       kaggle datasets download -d {slug} \\")
        print(f"         -p {input_dir} --unzip      # {what}")
    print("\n  4. Re-run:")
    print(f"       .venv/Scripts/python.exe -m scripts.ingest_kaggle --input-dir {input_dir}\n")
    print("The datasets are third-party scrapes of Upwork, redistributed on Kaggle and")
    print("NOT included in this repository. Check the licence on each dataset page before")
    print("republishing anything derived from them; see docs/DATA_CARD.md.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Load real Kaggle Upwork data alongside the synthetic corpus"
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=REPO_ROOT / "data" / "raw" / "kaggle",
        help="Directory holding the downloaded CSVs (default: data/raw/kaggle)",
    )
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "data" / "processed")
    parser.add_argument(
        "--limit",
        type=int,
        default=2_000,
        help="Maximum rows to read per file; 0 reads the whole file (default: 2000)",
    )
    args = parser.parse_args()

    input_dir: Path = args.input_dir
    files = sorted(input_dir.glob("*.csv")) if input_dir.is_dir() else []
    if not files:
        # Not an error: the loader is optional and a missing directory is the
        # normal state of a fresh clone.
        print_instructions(input_dir)
        return

    jobs: list[dict[str, Any]] = []
    contracts: list[dict[str, Any]] = []
    recognised: list[dict[str, Any]] = []
    skipped: dict[str, int] = defaultdict(int)
    seen_ids: set[str] = set()
    contracts_dataset = ""
    without_skills = 0

    for path in files:
        headers = read_header(path)
        dataset, kind, score = detect_dataset(headers)
        rows_in = rows_out = 0

        for row in iter_rows(path, args.limit):
            rows_in += 1
            if kind == "postings":
                job, reason = row_to_job(row, dataset)
                if job is None:
                    skipped[reason] += 1
                    continue
                if job["job_id"] in seen_ids:
                    skipped["duplicate of a row already ingested"] += 1
                    continue
                seen_ids.add(job["job_id"])
                if not job["required_skills"]:
                    without_skills += 1
                jobs.append(job)
                rows_out += 1
            elif kind == "contracts":
                contract = row_to_contract(row)
                if contract is None:
                    skipped["contract row with no amount or hourly rate"] += 1
                    continue
                contracts.append(contract)
                contracts_dataset = dataset
                rows_out += 1
            else:
                skipped[f"{path.name}: no handler for this dataset"] += 1

        recognised.append({
            "file": path.name,
            "dataset": dataset,
            "kind": kind,
            "header_match": round(score, 2),
            "rows_read": rows_in,
            "rows_ingested": rows_out,
        })

    out_dir: Path = args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    jobs_path = out_dir / "jobs_real.json"
    stats_path = out_dir / "market_stats_real.json"

    if jobs:
        jobs_path.write_text(json.dumps(jobs, indent=2, ensure_ascii=False), encoding="utf-8")
    stats: dict[str, Any] | None = None
    if contracts:
        stats = market_stats(contracts, contracts_dataset or "unknown")
        stats_path.write_text(json.dumps(stats, indent=2, ensure_ascii=False), encoding="utf-8")

    # ---- summary ---------------------------------------------------------
    rule = "-" * 72
    print(f"\n{rule}")
    print(f"{'file':34s} {'dataset':28s} {'read':>6s} {'kept':>6s}")
    for entry in recognised:
        label = entry["dataset"] if entry["header_match"] >= 0.5 else f"{entry['dataset']}?"
        print(
            f"{entry['file'][:34]:34s} {label[:28]:28s} "
            f"{entry['rows_read']:6d} {entry['rows_ingested']:6d}"
        )
    print(rule)
    print(f"postings ingested : {len(jobs)}")
    print(f"contracts ingested: {len(contracts)}")
    if without_skills:
        print(
            f"  {without_skills} posting(s) had no skills column - skill lists are empty "
            "rather than guessed"
        )
    if skipped:
        print("skipped:")
        for reason, count in sorted(skipped.items(), key=lambda kv: -kv[1]):
            print(f"  {count:6d}  {reason}")
    print(rule)
    if jobs:
        print(f"written: {jobs_path}  (synthetic: false, source: kaggle:<dataset>)")
        print("         jobs_live.json is untouched - the synthetic market is still what")
        print("         GigSim serves, and these are a separate, clearly tagged file.")
    else:
        print("no postings written: no file in the directory parsed as a postings dataset")
    if stats:
        print(
            f"written: {stats_path}  "
            f"({stats['skills_with_enough_data']} skills with 5+ priced rows)"
        )
    else:
        print(
            "no market statistics written: nothing in the directory carried awarded "
            "prices. The contracts dataset is the one that does."
        )


if __name__ == "__main__":
    main()
