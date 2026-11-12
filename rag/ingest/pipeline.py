"""Ingestion: loaders, chunkers, metadata, contextual prefixes.

Handles the four source shapes in the corpus - prose case studies, repo
READMEs, code, and short items (reviews, past proposals, CV) - because they
chunk differently. Splitting code on blank lines destroys it; splitting prose
on function boundaries does nothing.

Every chunk carries: persona, type, project, skills, date, client industry
and, for proposals, outcome. Those are what V4 (outcome ranking), V5
(multi-vector) and V9 (graph) filter and re-rank on, so they are not
decoration.

Incremental re-indexing: ``ingest_evidence`` is keyed on evidence_id, so
adding one item in the UI re-chunks that item only.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from rag.index.store import Chunk

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

CODE_FENCE = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)
MD_HEADING = re.compile(r"^(#{1,4})\s+(.+)$", re.MULTILINE)


def approx_tokens(text: str) -> int:
    """Word count scaled to approximate tokens. Deliberately tokenizer-free."""
    return int(len(text.split()) * 1.3) + 1


# ---------------------------------------------------------------------------
# Chunkers
# ---------------------------------------------------------------------------
def chunk_prose(text: str, max_tokens: int, overlap_tokens: int) -> list[str]:
    """Split on paragraphs, packing to the budget, with sentence overlap."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    current: list[str] = []
    budget = 0

    for para in paragraphs:
        cost = approx_tokens(para)
        if cost > max_tokens:
            # A single oversized paragraph: fall back to sentences.
            for sentence in re.split(r"(?<=[.!?])\s+", para):
                s_cost = approx_tokens(sentence)
                if budget + s_cost > max_tokens and current:
                    chunks.append("\n\n".join(current))
                    current, budget = _carry_overlap(current, overlap_tokens)
                current.append(sentence)
                budget += s_cost
            continue
        if budget + cost > max_tokens and current:
            chunks.append("\n\n".join(current))
            current, budget = _carry_overlap(current, overlap_tokens)
        current.append(para)
        budget += cost

    if current:
        chunks.append("\n\n".join(current))
    return [c for c in chunks if c.strip()]


def _carry_overlap(current: list[str], overlap_tokens: int) -> tuple[list[str], int]:
    carried: list[str] = []
    budget = 0
    for part in reversed(current):
        cost = approx_tokens(part)
        if budget + cost > overlap_tokens:
            break
        carried.insert(0, part)
        budget += cost
    return carried, budget


def chunk_markdown(text: str, max_tokens: int, overlap_tokens: int) -> list[str]:
    """Split a README on headings, keeping each heading with its body."""
    matches = list(MD_HEADING.finditer(text))
    if not matches:
        return chunk_prose(text, max_tokens, overlap_tokens)
    sections: list[str] = []
    for i, match in enumerate(matches):
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        section = text[start:end].strip()
        if approx_tokens(section) > max_tokens:
            heading = match.group(0)
            for part in chunk_prose(section, max_tokens, overlap_tokens):
                sections.append(part if part.startswith(heading) else f"{heading}\n{part}")
        elif section:
            sections.append(section)
    preamble = text[: matches[0].start()].strip()
    if preamble:
        sections.insert(0, preamble)
    return sections


def chunk_code(text: str, max_tokens: int) -> list[str]:
    """Keep fenced blocks whole; split only on top-level definitions."""
    blocks = CODE_FENCE.findall(text)
    if not blocks:
        return [text] if text.strip() else []
    out: list[str] = []
    for language, body in blocks:
        fenced = f"```{language}\n{body}```"
        if approx_tokens(fenced) <= max_tokens:
            out.append(fenced)
            continue
        units = re.split(r"\n(?=(?:def |class |async def |-- |@app|@router))", body)
        current: list[str] = []
        budget = 0
        for unit in units:
            cost = approx_tokens(unit)
            if budget + cost > max_tokens and current:
                out.append(f"```{language}\n" + "\n".join(current) + "```")
                current, budget = [], 0
            current.append(unit)
            budget += cost
        if current:
            out.append(f"```{language}\n" + "\n".join(current) + "```")
    return out


# ---------------------------------------------------------------------------
# Contextual prefixes (V6)
# ---------------------------------------------------------------------------
def contextual_prefix(record: dict[str, Any], chunk_text: str, position: int, total: int) -> str:
    """A generated line situating a fragment inside its document.

    Anthropic's contextual-retrieval result in miniature: a fragment that
    says only "we used ALS with 64 factors" is unfindable from a job post
    asking for a recommender. Prefixing the project, type and industry
    restores what the split removed. Generated deterministically from
    metadata rather than by an LLM call per chunk - same effect on recall at
    a fraction of the ingestion cost, and reproducible.
    """
    bits: list[str] = []
    if record.get("title"):
        bits.append(f"From '{record['title']}'")
    kind = record.get("type", "")
    if kind:
        bits.append(f"a {kind.replace('_', ' ')}")
    if record.get("client_industry"):
        bits.append(f"for a {record['client_industry']} client")
    if record.get("date"):
        bits.append(f"({record['date'][:4]})")
    if total > 1:
        bits.append(f"[part {position + 1} of {total}]")
    outcome = record.get("outcome", "unknown")
    if kind == "proposal" and outcome != "unknown":
        bits.append(f"- this proposal was {outcome}")
    return " ".join(bits) + "."


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------
def ingest_evidence(
    records: list[dict[str, Any]],
    *,
    max_tokens: int = 420,
    overlap_tokens: int = 60,
    add_context: bool = True,
) -> list[Chunk]:
    """Turn evidence records into indexed chunks."""
    chunks: list[Chunk] = []

    for record in records:
        text = record.get("text", "") or ""
        kind = record.get("type", "case_study")
        if not text.strip():
            continue

        if kind == "code":
            pieces = chunk_code(text, max_tokens)
        elif kind == "readme":
            pieces = chunk_markdown(text, max_tokens, overlap_tokens)
        elif kind in {"review", "proposal"}:
            pieces = [text] if approx_tokens(text) <= max_tokens else chunk_prose(text, max_tokens, overlap_tokens)
        else:
            pieces = chunk_prose(text, max_tokens, overlap_tokens)

        total = len(pieces)
        for position, piece in enumerate(pieces):
            prefix = contextual_prefix(record, piece, position, total) if add_context else ""
            chunks.append(
                Chunk(
                    chunk_id=f"{record['evidence_id']}::{position}",
                    evidence_id=record["evidence_id"],
                    persona_id=record.get("persona_id", ""),
                    project_id=record.get("project_id"),
                    type=kind,
                    view=record.get("view", kind),
                    title=record.get("title", ""),
                    text=piece,
                    skills=record.get("skills", []) or [],
                    client_industry=record.get("client_industry"),
                    date=record.get("date", "") or "",
                    outcome=record.get("outcome", "unknown") or "unknown",
                    source=record.get("source", ""),
                    position=position,
                    context_prefix=prefix,
                )
            )
    return chunks


def load_corpus(processed_dir: Path | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    directory = processed_dir or (REPO_ROOT / "data" / "processed")
    evidence_path = directory / "evidence.json"
    if not evidence_path.exists():
        raise FileNotFoundError(
            f"{evidence_path} not found. Run: python -m scripts.generate_corpus"
        )
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    projects_path = directory / "projects.json"
    projects = json.loads(projects_path.read_text(encoding="utf-8")) if projects_path.exists() else []
    return evidence, projects


def build_index(
    processed_dir: Path | None = None,
    *,
    config: Any | None = None,
    persist: Path | None = None,
):
    """Build (and optionally persist) the full corpus index."""
    from agent.config import get_config
    from rag.embeddings import get_embedder
    from rag.index.store import CorpusIndex

    cfg = config or get_config()
    evidence, projects = load_corpus(processed_dir)
    chunking = cfg.section("rag.chunking")
    chunks = ingest_evidence(
        evidence,
        max_tokens=int(chunking.get("max_tokens", 420)),
        overlap_tokens=int(chunking.get("overlap_tokens", 60)),
    )
    index = CorpusIndex(get_embedder(cfg))
    index.build(chunks, projects)
    if persist:
        index.save(persist)
    return index
