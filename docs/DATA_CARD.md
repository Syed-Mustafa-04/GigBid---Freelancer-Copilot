# Data card

What GigBid is built and evaluated on, where it came from, and what it cannot
be used to claim.

---

## Summary

| Dataset | Records | Real or synthetic | Licence | Used for |
| --- | --- | --- | --- | --- |
| Persona portfolios | 694 evidence items, 65 projects, 3 personas | **Synthetic** | Generated here; no licence constraint | The RAG corpus |
| Job market | 198 live, 600 dev, 1,500 historical | **Synthetic** | Generated here | GigSim, evaluation |
| Clients | 140, with hidden preferences | **Synthetic** | Generated here | GigSim reply/hire simulation |
| Policy pack | 11 rules | Instructor-written summary, restated here | Coursework material | Compliance Guard ground truth |
| Injection corpus | 8 planted + 8 paraphrased | **Synthetic** | Generated here | Guardrail evaluation |
| Upwork postings (Kaggle) | optional, up to 200k+ | **Real** | See licences below | Pricing realism; loader provided, not required |

Everything generated carries `"synthetic": true` in its record and a
`source` field naming the generator. Nothing real and nothing personal is in
the repository.

---

## Why the portfolio corpus is synthetic

This is the central data decision and the honest answer is: **the thing GigBid
is evaluated on does not exist in public data.**

The Kaggle Upwork datasets give job *postings* — title, description, budget,
skills. They do not give:

1. **Freelancer portfolios.** No case studies, repositories, code, or CVs tied
   to the people bidding.
2. **Proposal outcomes.** No record of which proposal won, which got a reply,
   and which was ignored — and the outcome is what V4 learns from.
3. **Gold evidence labels.** Nobody has annotated "this portfolio item proves
   this job requirement", which is exactly what Recall@5 and nDCG@10 measure.

Without (3) the retrieval evaluation is unmeasurable; a proxy label would make
the numbers look rigorous while meaning nothing. So the corpus is generated
such that gold labels are **knowable by construction**: each job is spawned
from a known project, so that project's evidence items *are* the relevant set,
with no annotation and no judgement.

### Generated to be hard on purpose

A synthetic corpus is only useful if it is not trivially easy. Three
difficulties are built in deliberately:

| Difficulty | How | What it tests |
| --- | --- | --- |
| **Three views of one project** | Each project appears as a prose case study, a repo README, and a code fragment, in vocabularies that barely overlap. | V5 multi-vector. Also the reason naive top-k returns near-duplicates. |
| **Implied, not named, skills** | A project demonstrating collaborative filtering says "matrix factorisation over an implicit-feedback matrix" and never the words the job post uses. | Dense vs lexical retrieval; the V2 hybrid case. |
| **Outcome ≠ topic** | A past proposal's won/replied/ignored label correlates with *phrasing* features (specific opening, named metric, a question), not with topical similarity. | V4 outcome ranking. Deliberately orthogonal to cosine similarity. |

### Generation

```
python -m scripts.generate_corpus --seed 20261115
python -m scripts.generate_jobs   --seed 20261115
```

Deterministic from the seed: the same command reproduces the same corpus
byte-for-byte, which is what makes the evaluation numbers re-checkable.

| Persona | Role | Projects | Evidence |
| --- | --- | --- | --- |
| `p_ai_engineer` | AI/ML engineer — retrieval, forecasting, production ML | 22 | ~240 |
| `p_fullstack_dev` | Full-stack — Next.js, FastAPI, Postgres, payments | 25 | ~260 |
| `p_data_analyst` | Data analyst — warehouse modelling, BI, experimentation | 18 | ~190 |

Each persona also carries 150 past proposals with outcome labels.

---

## Job families and gold labels

`evals/datasets/dev_jobs.json` holds 600 labelled jobs (200 per persona).

| Family | n | Gold decision | What it probes |
| --- | --- | --- | --- |
| `genuine` | 240 | `bid` | Normal operation |
| `stretch` | 120 | `watch` | Adjacent skills, partial evidence — the ambiguous band |
| `mismatch` | 105 | `skip` | Entirely different field |
| `scam` | 75 | `skip` + flags | Off-platform contact, unpaid trials, credential requests |
| `injection` | 60 | `bid`, must not comply | A **genuine** job carrying an instruction aimed at the agent |

The `injection` family is the subtle one. The underlying job is real and worth
bidding on; the correct behaviour is to **ignore the instruction and still
judge the job on its merits**. A system that skips every poisoned post is not
safe, it is broken — an attacker could suppress a competitor's bids by writing
"ignore previous instructions" into someone else's listing. This drove a real
fix: client-risk markers are computed on the post *with injected spans
removed*, so an attacker cannot manufacture risk flags about an innocent client.

Labels live in `dev_labels.json`, keyed by job id, and are never shown to the
graph.

---

## Known limitation: the gold design biases one metric

Gold evidence for a job is the evidence of the **single project** it was
generated from. Item-level `Recall@5` therefore *rewards concentration*: a
retriever returning three views of the gold project scores higher than one
returning the gold project plus four other relevant projects.

This is visible in the measured results — the naive baseline posts the highest
`Recall@5` precisely because it returns near-duplicates (2.08 distinct projects
in the top 5, against 3.90 for hybrid).

So `project_hit@k` and `project_mrr` are reported alongside, asking the question
the Proposal Writer actually depends on: *did the right project reach the top
k?* On that measure hybrid retrieval reaches **1.000** against the baseline's
0.989 while also surfacing broader evidence. Both metrics are in
`evals/results/rag_variants.json`, and the caveat is recorded in the artefact
itself, not only here.

A corpus where jobs legitimately span several projects would remove the bias.
That is the right next change and it is not made yet.

---

## Optional: real Kaggle data

`scripts/ingest_kaggle.py` layers real postings on top for pricing realism and
a more varied live stream. Not required: everything runs without it.

| Dataset | Licence | Used for |
| --- | --- | --- |
| [Upwork job postings 2024 (50k)](https://www.kaggle.com/datasets/asaniczka/upwork-job-postings-dataset-2024-50k-records) | ODC-BY / CC BY-SA 4.0 (per dataset page) | Posting text, budget distributions |
| [All jobs on Upwork (200k+)](https://www.kaggle.com/datasets/asaniczka/all-jobs-on-upwork-200k-plus) | Per dataset page | Volume, category spread |
| [Upwork freelance jobs (60k)](https://www.kaggle.com/datasets/ahmedmyalo/upwork-freelance-jobs-60k) | Per dataset page | Skill vocabulary |
| [Freelance contracts (1.3M)](https://www.kaggle.com/datasets/asaniczka/freelance-contracts-dataset-1-3-million-entries) | Per dataset page | Awarded-price statistics |
| [Freelancer success/earnings/churn](https://www.kaggle.com/datasets/sarveshchhetri/freelancer-success-earnings-and-churn-prediction) | Per dataset page | Client-quality priors |

**Attribution.** These are third-party datasets scraped from Upwork by their
authors and redistributed on Kaggle. They are **not** included in this
repository — only a loader. Verify the licence on each dataset page before
redistributing anything derived from them. Real postings are tagged
`"synthetic": false` and `"source": "kaggle:<dataset>"` so they are never
confused with generated records.

**Why they are optional rather than central.** They improve price realism and
nothing else that is measured here. The decision, grounding and retrieval
metrics all need labels these datasets do not carry.

---

## What this data cannot support

1. **Real-world reply rates.** GigSim's reply and hire rates come from a
   deterministic model of hidden client preferences. They are a *consistent*
   end metric for comparing system variants against each other — not a
   prediction of what any real client would do.
2. **Model-quality claims without an API key.** With no key, GigBid runs a
   deterministic offline provider so the graph still executes end to end. Every
   results file records `provider: offline_stub`, and those numbers measure the
   rule-based parts of the system only. They are not model measurements and the
   report never presents them as such.
3. **Generalisation of the injection defence.** The planted attacks are what the
   detector was written against; scoring only on them measures memorisation.
   The paraphrase set is the honest number, and it is lower.
4. **Anything about real people.** No real freelancer, client, or proposal is in
   this repository. Persona names are invented.

---

## Privacy and confidentiality

* No real client data, no scraped profiles, no personal information.
* OAuth tokens for live mode are Fernet-encrypted at rest, never logged, and
  never returned by any endpoint. With no `TOKEN_ENCRYPTION_KEY`, the store
  **refuses to save** rather than writing plaintext.
* PII is masked before anything reaches a log, a trace, or memory
  (`agent/safety.py: mask_pii`).
* The hidden test set is `.gitignore`d and never committed.

## Reproducing

```bash
make seed      # corpus, jobs, dev set, prompts   (seed 20261115)
make index     # build and persist the vector index
make eval      # every evaluation; writes evals/results/*.json
```

Every number in the README and the report traces to a file in
`evals/results/`.
