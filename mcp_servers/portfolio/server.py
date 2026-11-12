"""Portfolio MCP server (A1) - read-only access to the freelancer's evidence.

Built on the official MCP Python SDK. Good enough that another freelancer could
point Claude Desktop or Cursor at it and use it on their own portfolio - which
is the bar the assignment sets, and the reason the resources and prompts here
are the ones a freelancer would actually reach for.

Everything is read-only: there is no tool on this server that changes
anything, and every tool is annotated ``readOnlyHint=True``.

**Tool granularity (R&D question A).** The same capability is exposed three
ways, selected by ``GIGBID_TOOLSET``:

``fine``   10 narrow tools, one per evidence type and lookup.
``coarse``  3 broad tools with mode/filter arguments.
``best``    4 tools, chosen after reading the failures of the other two, with
            descriptions rewritten to say *when* to call them rather than what
            they do.

``evals/runners/tool_granularity.py`` runs 40 tasks with known-correct calls
against all three and reports tool-selection accuracy, argument accuracy and
tokens per task.

Run:  python -m mcp_servers.portfolio.server            # stdio
      GIGBID_MCP_TRANSPORT=streamable-http python -m mcp_servers.portfolio.server
"""

from __future__ import annotations

import json
import os
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from rag.retriever import get_index, get_variant
from rag.variants.base import RetrievalQuery

TOOLSET = os.getenv("GIGBID_TOOLSET", "best").lower()
DEFAULT_PERSONA = os.getenv("GIGBID_PERSONA", "p_ai_engineer")

mcp = FastMCP(
    "gigbid-portfolio",
    instructions=(
        "Read-only access to a freelancer's portfolio: case studies, repositories, "
        "code, client reviews, and past proposals with their outcomes. Use it to find "
        "concrete evidence that proves a specific capability before writing about it. "
        "Every item has an evidence_id - cite it. If nothing relevant comes back, say "
        "so rather than describing work that is not here."
    ),
)

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)


# ---------------------------------------------------------------------------
# Shared retrieval
# ---------------------------------------------------------------------------
async def _search(
    query: str,
    k: int = 8,
    *,
    types: list[str] | None = None,
    persona_id: str | None = None,
    industry: str | None = None,
    outcome: str | None = None,
    skills: list[str] | None = None,
) -> list[dict[str, Any]]:
    index = get_index()
    variant = get_variant(index=index)
    result = await variant.retrieve(
        RetrievalQuery(
            text=query,
            skills=skills or [],
            persona_id=persona_id or DEFAULT_PERSONA,
            industry=industry,
            k=max(k * 3, 20),
        )
    )
    rows: list[dict[str, Any]] = []
    for item in result.to_evidence_items():
        if types and item.type not in types:
            continue
        if outcome and item.outcome != outcome:
            continue
        if industry and (item.client_industry or "").lower() != industry.lower():
            continue
        rows.append(
            {
                "evidence_id": item.evidence_id,
                "type": item.type,
                "title": item.title,
                "excerpt": item.text[:600],
                "skills": item.skills,
                "project_id": item.project_id,
                "client_industry": item.client_industry,
                "date": item.date,
                "outcome": item.outcome,
                "relevance": round(item.score, 4),
                "source": item.source,
            }
        )
        if len(rows) >= k:
            break
    return rows


def _item(evidence_id: str) -> dict[str, Any]:
    index = get_index()
    chunks = index.by_evidence.get(evidence_id)
    if not chunks:
        return {"error": f"No evidence item with id '{evidence_id}'", "evidence_id": evidence_id}
    meta = index.evidence_meta.get(evidence_id, {})
    return {
        **meta,
        "evidence_id": evidence_id,
        "text": "\n\n".join(c.text for c in sorted(chunks, key=lambda c: c.position)),
        "chunks": len(chunks),
    }


# ---------------------------------------------------------------------------
# Tool set: BEST (shipped)
# ---------------------------------------------------------------------------
if TOOLSET == "best":

    @mcp.tool(annotations=READ_ONLY)
    async def search_evidence(
        requirement: str,
        k: int = 8,
        evidence_types: list[Literal["case_study", "readme", "code", "review", "proposal", "cv"]] | None = None,
        industry: str | None = None,
        outcome: Literal["won", "replied", "ignored", "delivered"] | None = None,
    ) -> str:
        """Find portfolio evidence that proves ONE specific requirement.

        Call this once per requirement, with the requirement phrased as the
        client wrote it - not as a keyword. Passing a whole job post returns a
        blurred average and misses most requirements.

        Filter by `evidence_types` when the kind of proof matters: `code` and
        `readme` prove hands-on implementation, `review` proves the client was
        satisfied, `proposal` with outcome='won' shows phrasing that has worked
        before. Use `industry` only when the client explicitly cares about
        domain experience.

        Returns items with an `evidence_id` you must cite for any claim built
        on them. An empty list means the portfolio does not support this
        requirement - say so, do not substitute something adjacent.
        """
        rows = await _search(requirement, k, types=evidence_types, industry=industry, outcome=outcome)
        return json.dumps({"requirement": requirement, "results": rows, "count": len(rows)}, indent=2)

    @mcp.tool(annotations=READ_ONLY)
    async def get_case_study(evidence_id: str) -> str:
        """Read one portfolio item in full, by its evidence_id.

        Use after `search_evidence` when an excerpt looks like the right proof
        but you need the detail - the metric, the stack, the client context -
        to write about it accurately instead of generically.
        """
        return json.dumps(_item(evidence_id), indent=2)

    @mcp.tool(annotations=READ_ONLY)
    async def list_skills(persona_id: str | None = None) -> str:
        """List every skill the portfolio can actually evidence, with counts.

        Call this before claiming a capability. A skill that is absent here
        cannot be claimed, however well it would fit the job.
        """
        index = get_index()
        persona = persona_id or DEFAULT_PERSONA
        counts: dict[str, int] = {}
        for skill, projects in index.graph.skill_to_projects.items():
            relevant = [
                p for p in projects
                if any(c.persona_id == persona for c in index.by_project.get(p, []))
            ]
            if relevant:
                counts[skill] = len(relevant)
        return json.dumps(
            {
                "persona_id": persona,
                "skills": [
                    {"skill": s, "projects": n}
                    for s, n in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
                ],
                "total": len(counts),
            },
            indent=2,
        )

    @mcp.tool(annotations=READ_ONLY)
    async def find_projects(
        skill: str, industry: str | None = None, persona_id: str | None = None
    ) -> str:
        """Answer 'have I done X, for a client in industry Y?' exactly.

        This is a structured lookup over the skill graph, not a similarity
        search, so it does not return near-misses. Use it for screening
        questions that ask for domain experience; a wrong answer there is
        worse than no answer.
        """
        index = get_index()
        persona = persona_id or DEFAULT_PERSONA
        project_ids = index.graph.projects_for(skill, industry)
        projects = []
        for pid in project_ids:
            chunks = [c for c in index.by_project.get(pid, []) if c.persona_id == persona]
            if not chunks:
                continue
            meta = index.graph.project_meta.get(pid, {})
            projects.append(
                {
                    "project_id": pid,
                    "title": meta.get("title") or chunks[0].title,
                    "industry": meta.get("industry") or chunks[0].client_industry,
                    "year": meta.get("year"),
                    "outcome_metric": meta.get("outcome_metric"),
                    "evidence_ids": sorted({c.evidence_id for c in chunks}),
                }
            )
        return json.dumps(
            {
                "skill": skill,
                "industry": industry,
                "found": len(projects),
                "projects": projects,
                "answer": (
                    f"Yes - {len(projects)} project(s)." if projects
                    else f"No project in this portfolio evidences '{skill}'"
                         + (f" for a {industry} client." if industry else ".")
                ),
            },
            indent=2,
        )


# ---------------------------------------------------------------------------
# Tool set: FINE - many narrow tools
# ---------------------------------------------------------------------------
elif TOOLSET == "fine":

    @mcp.tool(annotations=READ_ONLY)
    async def search_case_studies(query: str, k: int = 5) -> str:
        """Search case studies."""
        return json.dumps(await _search(query, k, types=["case_study"]), indent=2)

    @mcp.tool(annotations=READ_ONLY)
    async def search_repos(query: str, k: int = 5) -> str:
        """Search repository READMEs."""
        return json.dumps(await _search(query, k, types=["readme"]), indent=2)

    @mcp.tool(annotations=READ_ONLY)
    async def search_code(query: str, k: int = 5) -> str:
        """Search code fragments."""
        return json.dumps(await _search(query, k, types=["code"]), indent=2)

    @mcp.tool(annotations=READ_ONLY)
    async def search_reviews(query: str, k: int = 5) -> str:
        """Search client reviews."""
        return json.dumps(await _search(query, k, types=["review"]), indent=2)

    @mcp.tool(annotations=READ_ONLY)
    async def search_proposals(query: str, k: int = 5) -> str:
        """Search past proposals."""
        return json.dumps(await _search(query, k, types=["proposal"]), indent=2)

    @mcp.tool(annotations=READ_ONLY)
    async def search_won_proposals(query: str, k: int = 5) -> str:
        """Search past proposals that were won."""
        return json.dumps(await _search(query, k, types=["proposal"], outcome="won"), indent=2)

    @mcp.tool(annotations=READ_ONLY)
    async def get_case_study(evidence_id: str) -> str:
        """Get one case study by id."""
        return json.dumps(_item(evidence_id), indent=2)

    @mcp.tool(annotations=READ_ONLY)
    async def get_repo(evidence_id: str) -> str:
        """Get one repository README by id."""
        return json.dumps(_item(evidence_id), indent=2)

    @mcp.tool(annotations=READ_ONLY)
    async def get_cv(persona_id: str | None = None) -> str:
        """Get the CV."""
        index = get_index()
        persona = persona_id or DEFAULT_PERSONA
        for eid, meta in index.evidence_meta.items():
            if meta.get("type") == "cv" and meta.get("persona_id") == persona:
                return json.dumps(_item(eid), indent=2)
        return json.dumps({"error": "no CV found"})

    @mcp.tool(annotations=READ_ONLY)
    async def list_skills(persona_id: str | None = None) -> str:
        """List skills."""
        index = get_index()
        return json.dumps(sorted(index.graph.skill_to_projects), indent=2)


# ---------------------------------------------------------------------------
# Tool set: COARSE - few broad tools
# ---------------------------------------------------------------------------
else:

    @mcp.tool(annotations=READ_ONLY)
    async def search_portfolio(
        query: str,
        kind: str = "any",
        k: int = 8,
        industry: str | None = None,
        outcome: str | None = None,
    ) -> str:
        """Search the portfolio.

        kind: any | case_study | readme | code | review | proposal | cv
        outcome: won | replied | ignored | delivered
        """
        types = None if kind == "any" else [kind]
        return json.dumps(await _search(query, k, types=types, industry=industry, outcome=outcome), indent=2)

    @mcp.tool(annotations=READ_ONLY)
    async def get_item(evidence_id: str) -> str:
        """Get any portfolio item by id."""
        return json.dumps(_item(evidence_id), indent=2)

    @mcp.tool(annotations=READ_ONLY)
    async def describe_portfolio(persona_id: str | None = None) -> str:
        """Summarise the portfolio: skills, projects, counts by type."""
        index = get_index()
        persona = persona_id or DEFAULT_PERSONA
        types: dict[str, int] = {}
        for meta in index.evidence_meta.values():
            if meta.get("persona_id") == persona:
                types[meta.get("type", "?")] = types.get(meta.get("type", "?"), 0) + 1
        return json.dumps(
            {
                "persona_id": persona,
                "skills": sorted(index.graph.skill_to_projects),
                "evidence_by_type": types,
                "projects": len(index.by_project),
            },
            indent=2,
        )


# ---------------------------------------------------------------------------
# Resource templates - let a host load whole items by URI
# ---------------------------------------------------------------------------
@mcp.resource("portfolio://case/{evidence_id}")
def case_resource(evidence_id: str) -> str:
    """Full text of one case study, repo README, code fragment or review."""
    return json.dumps(_item(evidence_id), indent=2)


@mcp.resource("portfolio://proposal/{evidence_id}")
def proposal_resource(evidence_id: str) -> str:
    """Full text of one past proposal, including the outcome it achieved."""
    return json.dumps(_item(evidence_id), indent=2)


@mcp.resource("portfolio://project/{project_id}")
def project_resource(project_id: str) -> str:
    """Every evidence item belonging to one project, across all views."""
    index = get_index()
    chunks = index.by_project.get(project_id, [])
    if not chunks:
        return json.dumps({"error": f"No project '{project_id}'"})
    meta = index.graph.project_meta.get(project_id, {})
    by_evidence: dict[str, dict[str, Any]] = {}
    for chunk in chunks:
        entry = by_evidence.setdefault(
            chunk.evidence_id, {"type": chunk.type, "title": chunk.title, "text": []}
        )
        entry["text"].append(chunk.text)
    return json.dumps(
        {
            "project_id": project_id,
            "meta": meta,
            "evidence": [
                {"evidence_id": eid, "type": v["type"], "title": v["title"],
                 "text": "\n\n".join(v["text"])}
                for eid, v in by_evidence.items()
            ],
        },
        indent=2,
    )


@mcp.resource("portfolio://skills")
def skills_resource() -> str:
    """Every skill the portfolio can evidence, with the projects proving it."""
    index = get_index()
    return json.dumps(
        {s: sorted(p) for s, p in sorted(index.graph.skill_to_projects.items())}, indent=2
    )


# ---------------------------------------------------------------------------
# Prompt templates - the ones a freelancer would actually pick
# ---------------------------------------------------------------------------
@mcp.prompt()
def answer_screening_question(question: str, job_title: str = "") -> str:
    """Answer a screening question using only evidence from my portfolio."""
    return (
        f"Answer this screening question{f' for the job: {job_title}' if job_title else ''}.\n\n"
        f"QUESTION: {question}\n\n"
        "Procedure:\n"
        "1. Call search_evidence with the question as the requirement.\n"
        "2. If nothing relevant comes back, answer honestly that you have not done "
        "this specific thing, and say what adjacent work you have done instead.\n"
        "3. Otherwise answer in at most 120 words, naming the concrete project and "
        "its measurable result, and cite each evidence_id you used.\n"
        "4. Claim nothing the retrieved evidence does not state. No 'extensive "
        "experience', no invented numbers, no years-of-experience totals."
    )


@mcp.prompt()
def prove_requirement(requirement: str) -> str:
    """Find the single strongest proof for one requirement."""
    return (
        f"Find the strongest evidence in my portfolio that I can do this:\n\n{requirement}\n\n"
        "Call search_evidence, then get_case_study on the best hit. Report: the "
        "evidence_id, what it actually demonstrates, and - honestly - how far it "
        "falls short of what is being asked. If there is no real proof, say so plainly."
    )


@mcp.prompt()
def draft_opening_line(job_post: str) -> str:
    """Draft a specific opening line, grounded in one real project."""
    return (
        "Write the first two sentences of a proposal for the job below.\n\n"
        f"JOB POST:\n{job_post[:1500]}\n\n"
        "Rules: name the client's actual problem in their words; connect it to ONE "
        "real project from my portfolio with its measurable outcome; cite that "
        "evidence_id. Never open with 'I am an experienced professional' or any "
        "variant. No pleasantries."
    )


@mcp.prompt()
def check_before_claiming(draft: str) -> str:
    """Check a draft for claims the portfolio cannot support."""
    return (
        "Here is a proposal draft. Split it into individual claims about past "
        "experience. For each one, search the portfolio for supporting evidence and "
        "mark it supported, partly supported, or unsupported. Rewrite the partly "
        "supported ones to what the evidence actually shows and delete the "
        "unsupported ones. Return the cleaned draft plus the list of what you "
        f"changed.\n\nDRAFT:\n{draft}"
    )


def main() -> None:
    transport = os.getenv("GIGBID_MCP_TRANSPORT", "stdio")
    if transport == "streamable-http":
        mcp.settings.port = int(os.getenv("GIGBID_PORTFOLIO_PORT", "8101"))
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
