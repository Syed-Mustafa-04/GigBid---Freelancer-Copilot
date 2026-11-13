"""Generate the GigBid synthetic corpus.

Why synthetic, and what it is for
---------------------------------
The public Upwork datasets on Kaggle give us *job posts* (title, description,
budget, skills) but no freelancer portfolios, no client reply/hire outcomes
linked to specific proposals, and no gold "which evidence proves this
requirement" labels. Those three things are exactly what GigBid is evaluated
on, so they are generated here and clearly marked ``synthetic: true``.

``scripts/ingest_kaggle.py`` layers the *real* job postings on top for pricing
statistics and the live job stream. Provenance of every record is recorded in
``docs/DATA_CARD.md``.

The corpus is built to defeat naive RAG on purpose:

* **Three views of one project.** Every project appears as a prose case study,
  a repo README, and a code fragment. Naive top-k over mixed chunks returns
  three near-duplicates of the same project and starves the rest. V5
  (multi-vector, one project per hit) is the variant that fixes this.
* **Implied skills.** A project that says "matrix factorisation over an
  implicit-feedback matrix" never writes the words "collaborative filtering" or
  "recommender system", which is what the job post asks for. Dense-only
  retrieval has to bridge that; exact-name jobs ("Celery", "dbt") punish dense
  and reward BM25. That is the V2 hybrid story.
* **Outcome-bearing proposals.** Past proposals carry won/replied/ignored
  labels that correlate with phrasing features, not with topical similarity.
  V4 is the only variant that can see this signal.

Run:  python -m scripts.generate_corpus --out data/processed --seed 20261115
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
CORPUS_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Skill taxonomy. `implies` is the trap: a project that demonstrates the key
# skill describes it using `surface` wording and never names the key itself.
# ---------------------------------------------------------------------------
SKILL_GRAPH: dict[str, dict[str, Any]] = {
    "collaborative filtering": {
        "surface": ["matrix factorisation over an implicit-feedback matrix", "ALS on user-item interactions"],
        "implies": ["recommender systems", "numpy", "scipy sparse"],
    },
    "churn prediction": {
        "surface": ["survival-style retention modelling", "binary outcome model on subscription data"],
        "implies": ["classification", "feature engineering", "scikit-learn"],
    },
    "retrieval augmented generation": {
        "surface": ["grounded answering over a private document set", "citation-backed question answering"],
        "implies": ["embeddings", "vector search", "LLM orchestration"],
    },
    "time series forecasting": {
        "surface": ["weekly demand projection with seasonality terms", "autoregressive demand model"],
        "implies": ["pandas", "statsmodels", "feature engineering"],
    },
    "computer vision": {
        "surface": ["defect detection on production line imagery", "bounding-box model for shelf audits"],
        "implies": ["pytorch", "image augmentation", "transfer learning"],
    },
    "nlp classification": {
        "surface": ["routing inbound tickets to the right queue", "intent tagging on support transcripts"],
        "implies": ["transformers", "text preprocessing", "evaluation metrics"],
    },
    "dbt": {
        "surface": ["modelled the warehouse in layered SQL with tests", "staging and mart layers with lineage"],
        "implies": ["sql", "data modelling", "snowflake"],
    },
    "celery": {
        "surface": ["moved long jobs onto a Redis-backed worker queue", "asynchronous task fan-out with retries"],
        "implies": ["redis", "python", "background jobs"],
    },
    "fastapi": {
        "surface": ["typed Python HTTP service with generated OpenAPI", "async Python API with dependency injection"],
        "implies": ["python", "rest api", "pydantic"],
    },
    "react": {
        "surface": ["component-driven dashboard with client-side routing", "hook-based UI with optimistic updates"],
        "implies": ["typescript", "frontend", "state management"],
    },
    "next.js": {
        "surface": ["server-rendered storefront with incremental revalidation", "app-router site with streaming"],
        "implies": ["react", "typescript", "ssr"],
    },
    "postgres": {
        "surface": ["normalised schema with partial indexes", "relational store tuned with EXPLAIN"],
        "implies": ["sql", "database design", "query optimisation"],
    },
    "stripe": {
        "surface": ["subscription billing with webhook reconciliation", "metered usage billing integration"],
        "implies": ["payments", "webhooks", "api integration"],
    },
    "power bi": {
        "surface": ["executive refreshable reporting with row-level security", "self-service model with DAX measures"],
        "implies": ["dax", "data modelling", "business intelligence"],
    },
    "ab testing": {
        "surface": ["sequential experiment readout with guardrail metrics", "two-sided test on conversion lift"],
        "implies": ["statistics", "experiment design", "hypothesis testing"],
    },
    "etl pipelines": {
        "surface": ["nightly ingestion with idempotent loads", "incremental extract with watermarking"],
        "implies": ["airflow", "python", "data engineering"],
    },
    "kubernetes": {
        "surface": ["rolled services onto a managed cluster with health gates", "declarative deploys with autoscaling"],
        "implies": ["docker", "devops", "ci/cd"],
    },
    "langgraph": {
        "surface": ["stateful multi-step agent with checkpointing", "graph-structured tool-using workflow"],
        "implies": ["llm orchestration", "python", "agents"],
    },
}

INDUSTRIES = [
    "e-commerce", "fintech", "healthcare", "logistics", "edtech", "SaaS",
    "real estate", "travel", "manufacturing", "media", "non-profit", "gaming",
]

CLIENT_FIRST = ["Nadia", "Tomas", "Priya", "Marcus", "Elena", "Yusuf", "Hana", "Diego",
                "Aisha", "Lars", "Mei", "Omar", "Sofia", "Kwame", "Ingrid", "Rafael"]
CLIENT_LAST = ["Okonkwo", "Berg", "Raman", "Silva", "Novak", "Haddad", "Tanaka", "Moreau",
               "Fitzgerald", "Andersson", "Chen", "Farouk", "Rossi", "Mensah", "Lindqvist", "Costa"]
COMPANY_SUFFIX = ["Labs", "Group", "Collective", "Systems", "Partners", "Studio", "Works", "Digital"]


# ---------------------------------------------------------------------------
# Personas
# ---------------------------------------------------------------------------
@dataclass
class PersonaSpec:
    persona_id: str
    display_name: str
    headline: str
    core_skills: list[str]
    domains: list[str]
    hourly_rate_usd: float
    min_project_usd: float
    years_experience: int
    tone: str
    n_projects: int


PERSONAS: list[PersonaSpec] = [
    PersonaSpec(
        persona_id="p_ai_engineer",
        display_name="Amara Osei",
        headline="AI/ML engineer - retrieval systems, forecasting and production ML",
        core_skills=["retrieval augmented generation", "churn prediction", "time series forecasting",
                     "nlp classification", "collaborative filtering", "langgraph", "fastapi"],
        domains=["SaaS", "e-commerce", "fintech", "healthcare"],
        hourly_rate_usd=68.0,
        min_project_usd=900.0,
        years_experience=6,
        tone="precise, evidence-led, slightly formal",
        n_projects=22,
    ),
    PersonaSpec(
        persona_id="p_fullstack_dev",
        display_name="Rohan Mehta",
        headline="Full-stack web developer - Next.js, FastAPI, Postgres, payments",
        core_skills=["next.js", "react", "fastapi", "postgres", "stripe", "celery", "kubernetes"],
        domains=["e-commerce", "SaaS", "travel", "media"],
        hourly_rate_usd=55.0,
        min_project_usd=700.0,
        years_experience=7,
        tone="direct, practical, ships-first",
        n_projects=25,
    ),
    PersonaSpec(
        persona_id="p_data_analyst",
        display_name="Clara Bauer",
        headline="Data analyst - warehouse modelling, BI and experimentation",
        core_skills=["dbt", "power bi", "ab testing", "etl pipelines", "postgres", "time series forecasting"],
        domains=["retail", "logistics", "fintech", "non-profit", "e-commerce"],
        hourly_rate_usd=47.0,
        min_project_usd=500.0,
        years_experience=5,
        tone="plain, numbers-first, cautious about claims",
        n_projects=18,
    ),
]


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------
@dataclass
class Project:
    project_id: str
    persona_id: str
    title: str
    primary_skill: str
    supporting_skills: list[str]
    industry: str
    year: int
    outcome_metric: str
    duration_weeks: int
    budget_usd: float
    stack: list[str] = field(default_factory=list)


@dataclass
class EvidenceRecord:
    evidence_id: str
    persona_id: str
    project_id: str | None
    type: str
    title: str
    text: str
    skills: list[str]
    client_industry: str | None
    date: str
    outcome: str
    source: str
    synthetic: bool = True
    view: str = ""


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------
class CorpusGenerator:
    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)
        self.seed = seed
        self.now = datetime(2026, 11, 1, tzinfo=UTC)

    # -- helpers ----------------------------------------------------------
    def _id(self, prefix: str, *parts: Any) -> str:
        raw = "::".join(str(p) for p in parts)
        digest = hashlib.sha1(f"{self.seed}{raw}".encode()).hexdigest()[:10]
        return f"{prefix}_{digest}"

    def _date_within(self, years_back: int) -> str:
        days = self.rng.randint(30, years_back * 365)
        return (self.now - timedelta(days=days)).date().isoformat()

    def _surface(self, skill: str) -> str:
        entry = SKILL_GRAPH.get(skill)
        if not entry:
            return skill
        return self.rng.choice(entry["surface"])

    def _implied(self, skill: str) -> list[str]:
        return list(SKILL_GRAPH.get(skill, {}).get("implies", []))

    # -- projects ---------------------------------------------------------
    def projects_for(self, persona: PersonaSpec) -> list[Project]:
        projects: list[Project] = []
        for i in range(persona.n_projects):
            primary = persona.core_skills[i % len(persona.core_skills)]
            others = [s for s in persona.core_skills if s != primary]
            self.rng.shuffle(others)
            supporting = others[: self.rng.randint(1, 3)]
            industry = self.rng.choice(persona.domains)
            year = self.rng.randint(2021, 2026)
            metric = self.rng.choice([
                f"cut manual review time by {self.rng.randint(25, 70)}%",
                f"raised offline precision from {self.rng.randint(55,70)}% to {self.rng.randint(74,91)}%",
                f"reduced p95 latency from {self.rng.randint(800,2400)}ms to {self.rng.randint(90,400)}ms",
                f"brought weekly reporting from {self.rng.randint(2,5)} days to under {self.rng.randint(2,8)} hours",
                f"lifted qualified conversions by {self.rng.randint(4,19)}% in a controlled test",
                f"handled {self.rng.randint(3,40)}x peak traffic without added infrastructure",
            ])
            projects.append(
                Project(
                    project_id=self._id("proj", persona.persona_id, i),
                    persona_id=persona.persona_id,
                    title=self._project_title(primary, industry),
                    primary_skill=primary,
                    supporting_skills=supporting,
                    industry=industry,
                    year=year,
                    outcome_metric=metric,
                    duration_weeks=self.rng.randint(2, 16),
                    budget_usd=float(self.rng.randrange(600, 14000, 50)),
                    stack=self._implied(primary) + [s for s in supporting],
                )
            )
        return projects

    def _project_title(self, skill: str, industry: str) -> str:
        templates = [
            "{s} for a {i} operator",
            "{i} {s} rebuild",
            "Production {s} system in {i}",
            "{s} engagement with a {i} client",
        ]
        return self.rng.choice(templates).format(s=skill.title(), i=industry)

    # -- the three views of a project -------------------------------------
    def case_study(self, persona: PersonaSpec, project: Project) -> EvidenceRecord:
        surface = self._surface(project.primary_skill)
        support = ", ".join(self._surface(s) for s in project.supporting_skills)
        text = (
            f"## {project.title}\n\n"
            f"**Client context.** A {project.industry} company engaged me for {project.duration_weeks} weeks "
            f"in {project.year}. The brief was broad: they knew the symptom, not the cause.\n\n"
            f"**What I built.** I implemented {surface}. Alongside that I handled {support}. "
            f"The work was delivered as a reproducible repository with tests and a written handover.\n\n"
            f"**Result.** We {project.outcome_metric}. The client took ownership of the system at the end "
            f"of the engagement and has run it since without me.\n\n"
            f"**What I would do differently.** The first two weeks were spent on data access that could have "
            f"been resolved in a single call with their platform team."
        )
        return EvidenceRecord(
            evidence_id=self._id("ev", project.project_id, "case"),
            persona_id=persona.persona_id,
            project_id=project.project_id,
            type="case_study",
            title=project.title,
            text=text,
            # NOTE: the primary skill is deliberately NOT listed by name here.
            skills=sorted(set(project.supporting_skills + self._implied(project.primary_skill))),
            client_industry=project.industry,
            date=f"{project.year}-{self.rng.randint(1,12):02d}-15",
            outcome="delivered",
            source="synthetic:case_study",
            view="summary",
        )

    def readme(self, persona: PersonaSpec, project: Project) -> EvidenceRecord:
        stack = sorted(set(project.stack))[:5]
        text = (
            f"# {project.title.lower().replace(' ', '-')}\n\n"
            f"Internal tooling delivered for a {project.industry} client.\n\n"
            f"## Stack\n" + "\n".join(f"- {s}" for s in stack) + "\n\n"
            "## Running locally\n```bash\nmake install\nmake test\nmake run\n```\n\n"
            "## Layout\n"
            "- `src/pipeline/` extraction and transformation steps\n"
            "- `src/models/` model definitions and training entrypoints\n"
            "- `tests/` unit and integration coverage\n\n"
            "## Notes\n"
            "Runs on a schedule. Failures page the on-call channel. "
            "Configuration is environment-driven; no credentials in the repo."
        )
        return EvidenceRecord(
            evidence_id=self._id("ev", project.project_id, "readme"),
            persona_id=persona.persona_id,
            project_id=project.project_id,
            type="readme",
            title=f"README - {project.title}",
            text=text,
            skills=stack,
            client_industry=project.industry,
            date=f"{project.year}-{self.rng.randint(1,12):02d}-20",
            outcome="delivered",
            source="synthetic:repo_readme",
            view="readme",
        )

    def code(self, persona: PersonaSpec, project: Project) -> EvidenceRecord:
        skill = project.primary_skill
        snippet = CODE_SNIPPETS.get(skill, CODE_SNIPPETS["_default"])
        text = (
            f"```python\n# {project.project_id} - extracted from the delivered repository\n"
            f"{snippet}\n```"
        )
        return EvidenceRecord(
            evidence_id=self._id("ev", project.project_id, "code"),
            persona_id=persona.persona_id,
            project_id=project.project_id,
            type="code",
            title=f"Code - {project.title}",
            text=text,
            skills=self._implied(skill),
            client_industry=project.industry,
            date=f"{project.year}-{self.rng.randint(1,12):02d}-22",
            outcome="delivered",
            source="synthetic:repo_code",
            view="code",
        )

    def review(self, persona: PersonaSpec, project: Project) -> EvidenceRecord:
        rating = self.rng.choice([5.0, 5.0, 5.0, 4.0, 4.5, 3.5])
        body = self.rng.choice([
            "Clear communicator. Delivered what was agreed and flagged a scope risk early rather than after the fact.",
            "Good work. Documentation was better than we expected and the handover meant our team could take over.",
            "Solid delivery. Timeline slipped by a few days but we were told in advance and the quality held up.",
            "Knew the domain. Pushed back on a request that would have cost us money, which we appreciated.",
        ])
        return EvidenceRecord(
            evidence_id=self._id("ev", project.project_id, "review"),
            persona_id=persona.persona_id,
            project_id=project.project_id,
            type="review",
            title=f"Client review ({rating}/5)",
            text=f"{body} Rating: {rating}/5. Project: {project.title}.",
            skills=[],
            client_industry=project.industry,
            date=f"{project.year}-{self.rng.randint(1,12):02d}-28",
            outcome="delivered",
            source="synthetic:client_review",
            view="review",
        )

    def cv(self, persona: PersonaSpec, projects: list[Project]) -> EvidenceRecord:
        lines = [
            f"# {persona.display_name}",
            f"{persona.headline}",
            "",
            f"{persona.years_experience} years of independent contract work. "
            f"Typical engagement {persona.min_project_usd:.0f} USD and up; "
            f"indicative rate {persona.hourly_rate_usd:.0f} USD/hour.",
            "",
            "## Selected engagements",
        ]
        for proj in projects[:8]:
            lines.append(f"- **{proj.title}** ({proj.year}, {proj.industry}) - {proj.outcome_metric}.")
        lines += ["", "## Working style", persona.tone.capitalize() + "."]
        return EvidenceRecord(
            evidence_id=self._id("ev", persona.persona_id, "cv"),
            persona_id=persona.persona_id,
            project_id=None,
            type="cv",
            title=f"CV - {persona.display_name}",
            text="\n".join(lines),
            skills=sorted(set(persona.core_skills)),
            client_industry=None,
            date=self._date_within(1),
            outcome="unknown",
            source="synthetic:cv",
            view="cv",
        )

    # -- past proposals, with outcomes ------------------------------------
    def past_proposals(self, persona: PersonaSpec, projects: list[Project], n: int) -> list[EvidenceRecord]:
        """Outcome correlates with *phrasing features*, not topical similarity.

        Specific-opening + named metric + a question -> much more likely to be
        won/replied. This is the only signal V4 can exploit, and it is
        deliberately orthogonal to cosine similarity.
        """
        out: list[EvidenceRecord] = []
        for i in range(n):
            project = self.rng.choice(projects)
            specific_opening = self.rng.random() < 0.45
            names_metric = self.rng.random() < 0.5
            asks_question = self.rng.random() < 0.5

            score = 0.12
            score += 0.30 if specific_opening else 0.0
            score += 0.22 if names_metric else 0.0
            score += 0.16 if asks_question else 0.0
            roll = self.rng.random()
            if roll < score * 0.55:
                outcome = "won"
            elif roll < score:
                outcome = "replied"
            else:
                outcome = "ignored"

            opening = (
                f"Your post mentions {self._surface(project.primary_skill)} - that is the exact problem "
                f"I handled for a {project.industry} client in {project.year}."
                if specific_opening
                else "I am an experienced professional with a passion for delivering high-quality results."
            )
            metric = f" On that engagement we {project.outcome_metric}." if names_metric else ""
            question = (
                " One question before I scope this: is the existing data already in a warehouse, "
                "or does ingestion sit inside this engagement?"
                if asks_question
                else ""
            )
            body = (
                f"{opening}{metric}\n\n"
                f"How I would approach yours: a short discovery pass to confirm the data and constraints, "
                f"then an initial working version you can review inside the first week, then hardening "
                f"and handover.{question}"
            )
            out.append(
                EvidenceRecord(
                    evidence_id=self._id("ev", persona.persona_id, "prop", i),
                    persona_id=persona.persona_id,
                    project_id=project.project_id,
                    type="proposal",
                    title=f"Past proposal - {project.title} ({outcome})",
                    text=body,
                    skills=self._implied(project.primary_skill),
                    client_industry=project.industry,
                    date=self._date_within(3),
                    outcome=outcome,
                    source="synthetic:past_proposal",
                    view="proposal",
                )
            )
        return out


CODE_SNIPPETS: dict[str, str] = {
    "collaborative filtering": (
        "def fit_als(interactions, factors=64, reg=0.02, iters=15):\n"
        "    users, items = interactions.shape\n"
        "    U = np.random.normal(scale=0.01, size=(users, factors))\n"
        "    V = np.random.normal(scale=0.01, size=(items, factors))\n"
        "    for _ in range(iters):\n"
        "        U = _solve_side(interactions, V, reg)\n"
        "        V = _solve_side(interactions.T, U, reg)\n"
        "    return U, V"
    ),
    "churn prediction": (
        "def build_features(events: pd.DataFrame) -> pd.DataFrame:\n"
        "    windowed = events.groupby('account_id').agg(\n"
        "        sessions_28d=('session_id', 'nunique'),\n"
        "        last_seen_days=('ts', lambda s: (NOW - s.max()).days),\n"
        "        support_tickets=('ticket_id', 'count'),\n"
        "    )\n"
        "    return windowed.fillna({'support_tickets': 0})"
    ),
    "retrieval augmented generation": (
        "async def answer(question: str, k: int = 8) -> Answer:\n"
        "    dense = await store.search(embed(question), k=40)\n"
        "    lexical = bm25.search(question, k=40)\n"
        "    fused = reciprocal_rank_fusion([dense, lexical], k=60)\n"
        "    top = reranker.rank(question, fused)[:k]\n"
        "    return await synthesise(question, top)"
    ),
    "dbt": (
        "-- models/marts/fct_orders.sql\n"
        "with payments as (select * from {{ ref('stg_payments') }}),\n"
        "     orders   as (select * from {{ ref('stg_orders') }})\n"
        "select o.order_id, o.customer_id, sum(p.amount) as amount\n"
        "from orders o left join payments p using (order_id)\n"
        "group by 1, 2"
    ),
    "celery": (
        "@app.task(bind=True, max_retries=5, acks_late=True)\n"
        "def reconcile_invoice(self, invoice_id: str) -> None:\n"
        "    try:\n"
        "        gateway.reconcile(invoice_id)\n"
        "    except TransientGatewayError as exc:\n"
        "        raise self.retry(exc=exc, countdown=2 ** self.request.retries)"
    ),
    "fastapi": (
        "@router.post('/proposals', response_model=ProposalOut, status_code=201)\n"
        "async def create_proposal(\n"
        "    payload: ProposalIn,\n"
        "    user: User = Depends(current_user),\n"
        "    svc: ProposalService = Depends(get_service),\n"
        ") -> ProposalOut:\n"
        "    return await svc.create(user.id, payload)"
    ),
    "next.js": (
        "export default async function JobPage({ params }: { params: { id: string } }) {\n"
        "  const job = await getJob(params.id, { next: { revalidate: 60 } });\n"
        "  return <JobDetail job={job} />;\n"
        "}"
    ),
    "stripe": (
        "@router.post('/webhooks/stripe')\n"
        "async def stripe_webhook(request: Request) -> Response:\n"
        "    event = stripe.Webhook.construct_event(\n"
        "        await request.body(), request.headers['stripe-signature'], SECRET\n"
        "    )\n"
        "    if event.type == 'invoice.payment_failed':\n"
        "        await dunning.schedule(event.data.object.customer)\n"
        "    return Response(status_code=204)"
    ),
    "ab testing": (
        "def sequential_readout(control, treatment, alpha=0.05):\n"
        "    lift = treatment.mean() - control.mean()\n"
        "    se = np.sqrt(control.var()/len(control) + treatment.var()/len(treatment))\n"
        "    z = lift / se\n"
        "    return {'lift': lift, 'z': z, 'significant': abs(z) > norm.ppf(1 - alpha/2)}"
    ),
    "time series forecasting": (
        "def weekly_forecast(history: pd.Series, horizon: int = 13) -> pd.Series:\n"
        "    seasonal = history.rolling(52, min_periods=8).mean()\n"
        "    trend = history.diff().rolling(8).mean().iloc[-1]\n"
        "    base = history.iloc[-1]\n"
        "    return pd.Series([base + trend * h for h in range(1, horizon + 1)])"
    ),
    "_default": (
        "def main(config: Config) -> int:\n"
        "    pipeline = build_pipeline(config)\n"
        "    result = pipeline.run()\n"
        "    report(result)\n"
        "    return 0 if result.ok else 1"
    ),
}


def build(seed: int, out_dir: Path) -> dict[str, Any]:
    gen = CorpusGenerator(seed)
    personas_out: list[dict[str, Any]] = []
    projects_out: list[dict[str, Any]] = []
    evidence_out: list[dict[str, Any]] = []

    for persona in PERSONAS:
        projects = gen.projects_for(persona)
        projects_out.extend(asdict(p) for p in projects)
        personas_out.append({**asdict(persona), "synthetic": True})

        evidence_out.append(asdict(gen.cv(persona, projects)))
        for project in projects:
            evidence_out.append(asdict(gen.case_study(persona, project)))
            evidence_out.append(asdict(gen.readme(persona, project)))
            evidence_out.append(asdict(gen.code(persona, project)))
            if gen.rng.random() < 0.75:
                evidence_out.append(asdict(gen.review(persona, project)))
        evidence_out.extend(asdict(r) for r in gen.past_proposals(persona, projects, 150))

    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "corpus_version": CORPUS_VERSION,
        "seed": seed,
        "generated_at": datetime.now(UTC).isoformat(),
        "synthetic": True,
        "counts": {
            "personas": len(personas_out),
            "projects": len(projects_out),
            "evidence": len(evidence_out),
        },
        "evidence_by_type": {
            t: sum(1 for e in evidence_out if e["type"] == t)
            for t in sorted({e["type"] for e in evidence_out})
        },
    }
    _write(out_dir / "personas.json", personas_out)
    _write(out_dir / "projects.json", projects_out)
    _write(out_dir / "evidence.json", evidence_out)
    _write(out_dir / "corpus_manifest.json", manifest)
    return manifest


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the GigBid synthetic corpus")
    parser.add_argument("--seed", type=int, default=20261115)
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "data" / "processed")
    args = parser.parse_args()
    manifest = build(args.seed, args.out)
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
