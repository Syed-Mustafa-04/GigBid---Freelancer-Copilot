# ADR 0004 — Which retrieval variant ships

**Status:** accepted · **Date:** 2026-11-15
**Evidence:** [`evals/results/rag_variants.json`](../../evals/results/rag_variants.json)
— 90 gold-labelled dev jobs, bge-small embeddings, 954 chunks.

## Context

Ten variants were implemented and measured. R6 requires the shipped design to
be chosen by evaluation, not by default.

## The measurements

Sorted by project hit@5, then item recall@5.

| Variant | Recall@5 | nDCG@10 | MRR | **project hit@5** | project MRR | distinct projects@5 | p95 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| V7 HyDE | 0.527 | **0.538** | 0.805 | **1.000** | 0.832 | 3.70 | 864 ms |
| **V4 outcome-aware (ships)** | **0.513** | 0.531 | 0.855 | **1.000** | **0.857** | 3.97 | **484 ms** |
| V2 hybrid | 0.464 | 0.532 | **0.857** | **1.000** | **0.857** | 3.90 | 933 ms |
| V6 contextual | 0.464 | 0.532 | 0.857 | **1.000** | 0.857 | 3.90 | 544 ms |
| V3 cross-encoder rerank | 0.384 | 0.491 | 0.751 | **1.000** | 0.752 | 3.90 | 549 ms |
| V5 multi-vector | 0.200 | 0.210 | 0.850 | **1.000** | 0.850 | 4.93 | 567 ms |
| V0 naive dense | 0.656 | 0.587 | 0.838 | 0.989 | 0.848 | 2.08 | 357 ms |
| V1 requirement decomposition | 0.271 | 0.264 | 0.620 | 0.911 | 0.636 | 4.53 | 891 ms |
| V9 skill graph | 0.144 | 0.137 | 0.471 | 0.722 | 0.471 | 5.00 | 604 ms |
| V8 corrective RAG | 0.127 | 0.123 | 0.322 | 0.411 | 0.322 | 2.18 | 1,698 ms |

## Decision

Ship **V4: hybrid retrieval with an outcome-aware prior**.
`agent/config.yaml: rag.shipped_variant` is the one line that encodes it.

## Why not the top nDCG row

The naive baseline posts the highest Recall@5 (0.656) and nDCG@10 (0.587), and
**that is a metric artifact, not a win**.

Gold evidence for a job is the evidence of the single project the job was
generated from, so item-level recall rewards *concentration*. V0 scores highly
because it returns near-identical views of one project — 2.08 distinct projects
in its top 5, against 3.90–4.93 for the hybrid family. It is not finding more;
it is finding the same thing repeatedly, which is exactly the failure the
corpus was built to expose.

`project_hit@5` asks the question the Proposal Writer depends on: did the right
project reach the top k? V0 misses it on 1.1% of jobs. Six variants never miss
it. Both metrics are in the results file and the caveat is recorded in the
artefact itself, so the recall column cannot be read in isolation later.

## Why V4 within the perfect-recall group

Six variants achieve `project_hit@5 = 1.000`. Among them V4:

- has the **highest item recall** bar HyDE (0.513 vs 0.527, a 0.014 gap);
- has the **joint-best project MRR** (0.857) — it ranks the right project top;
- is the **fastest** (484 ms p95, 44% quicker than HyDE);
- costs **no LLM call per retrieval**, where HyDE spends tokens on every query
  to generate its hypothetical document.

It is also the only variant that reads the outcome signal, which is a product
advantage rather than a metric one: it surfaces past proposals that actually
won, which is what the Writer should be imitating.

Paying 864 ms and a token spend per query for 0.014 recall is not a trade worth
making on a free-tier budget.

## Rejected, with reasons

**V3 cross-encoder reranking — a deployment-constraint rejection, not a quality
one.** Measured at **~1.3 s per query-document pair on CPU**; reranking the full
40-candidate set costs **53.6 s per job**, against a 25 s p95 budget for the
entire job before any LLM call. Capped to 10 candidates it drops to 549 ms but
also to 0.384 recall — the cap removes most of what reranking was for. On a GPU
this is likely the right answer, and the code path is kept and measured so that
can be revisited rather than re-derived.

**V1 requirement decomposition — a genuine negative result.** It halved
Recall@5 (0.271) and had the second-worst project hit rate (0.911). It spreads
retrieval across 4.53 projects, correct for a job spanning several projects and
wrong for this corpus, where every job derives from exactly one. The hypothesis
as stated — that decomposition raises evidence recall — is **not supported**.
The corpus limitation that makes it unfair is recorded in the data card.

**V8 corrective RAG — the worst result in the table, and the most instructive.**
`project_hit@5 = 0.411` means it discards the right project on nearly 60% of
jobs. The grading threshold (0.5) is tuned far too strictly for this corpus, so
the loop declares the evidence insufficient and returns almost nothing. The
instinct it encodes — refusing to claim when evidence is weak — is right, and
the shipped Fit Matcher still honours `insufficient_evidence` when V8 is used.
As a retrieval variant it is badly calibrated, and it is reported as such rather
than quietly dropped.

**V9 skill graph (0.722).** Depends on clean skill extraction to seed the
traversal; when extraction is thin it falls back to dense and loses. Promising
for "have I done X in industry Y" screening questions specifically, which is
not what this benchmark measures.

**V6 contextual retrieval is identical to V2 on this corpus** — same numbers to
three decimals. `Chunk.embed_text()` applies the context prefix at *ingestion*
for every variant, so V6 is V2 over an already-contextual index rather than an
independent arm. That is a flaw in the comparison, recorded in the code and in
the diagnostics. The contribution of contextual prefixes is measured properly
by the `no_context` row of the ablation, which re-embeds the corpus without
them.

## What the ablation says — and it contradicts this decision

Run after this ADR was accepted, on 60 jobs
([`evals/results/ablation.json`](../../evals/results/ablation.json)):

| Ablation | Recall@5 | nDCG@10 | project hit@5 | p95 | Δ recall |
| --- | --- | --- | --- | --- | --- |
| shipped (V4) | 0.487 | 0.504 | 1.000 | 394 ms | — |
| **no_lexical** | **0.570** | **0.564** | 1.000 | **341 ms** | **+0.083** |
| no_rrf | 0.500 | 0.453 | 0.983 | 482 ms | +0.013 |
| no_rerank | 0.487 | 0.504 | 1.000 | 460 ms | 0.000 |
| no_context | 0.393 | 0.452 | 1.000 | 480 ms | −0.094 |
| no_persona | 0.393 | 0.434 | 1.000 | 477 ms | −0.094 |
| no_dense | 0.370 | 0.360 | 1.000 | 105 ms | −0.117 |

**Removing the BM25 channel makes retrieval better.** Dropping it raises
Recall@5 by 8.3 points and nDCG by 6.0, and is 53 ms faster. The lexical
channel is not neutral weight — it is actively degrading the shipped design.

That reading is consistent with the variant table, which should have prompted
this question earlier: V0 naive *dense* posts the highest raw recall of all ten
variants. The corpus was built on the premise that exact tool names ("Celery",
"dbt") would reward lexical matching. On these 90 jobs that premise does not
hold: the dense encoder already handles the vocabulary, and BM25 mostly
promotes chunks sharing common English with the post.

Three other results worth recording:

- **Contextual prefixes are load-bearing** (−0.094 without them). This settles
  the V6 ≡ V2 puzzle: the prefixes matter, they are simply present in every
  variant, so the variant comparison could never see them. Only the ablation,
  which re-embeds without them, can.
- **The persona filter is worth 0.094 recall**, so tenancy isolation is also a
  quality feature, not only a correctness one.
- **`no_rerank` changes nothing at all** (identical to three decimals), which
  is the correct result — V4 does not rerank — and serves as a sanity check
  that the harness is ablating what it claims to.

### Why V4 still ships today

Not because the ablation is wrong. Because the indicated change — dense plus
the outcome prior, no lexical channel — **has not been measured end to end**.
The ablation moves one component inside a fixed harness; it does not run the
decision path, the Writer, claim verification, or GigSim reply rate. Shipping a
retrieval change on a deadline, on the strength of one 60-job run, against a
metric the data card already flags as biased toward concentration, would repeat
exactly the mistake this ADR criticises in the baseline.

**The next change to this file is to add `v10_dense_outcome` and measure it
properly.** That is written down here rather than left as an intention.

## Consequences

Good: 484 ms p95, no GPU, no external vector service, perfect project-level
recall on the dev set, and the outcome prior aligns retrieval with what has
actually won work.

Bad: the outcome prior can trade evidence relevance for stylistic familiarity.
If a future run shows reply rate rising while Recall@5 falls, that is the
trade-off appearing, and it should be reported rather than tuned away.

The choice is one config line, so a re-measurement that changes it is a
one-line diff with the results file as justification.
