"""Pricing Strategist: a bid defensible on what clients actually paid.

The fair band comes from awarded prices in the market statistics, not from the
job's budget field - budgets are frequently aspirational, and anchoring on them
is how freelancers end up bidding below cost on a job the client would have
paid double for.

The band is computed in Python; the model only writes the rationale and may
nudge within the band. Letting a model pick the number outright produces
confident, round, unjustifiable figures.
"""

from __future__ import annotations

from typing import Any

from agent.config import get_config
from agent.handoffs import FitReport, JobSpec, PriceQuote
from agent.llm import RunLedger
from agent.nodes.common import call_tool, llm, node, system_message


@node("pricing")
async def price_job(state: dict[str, Any], ledger: RunLedger) -> dict[str, Any]:
    cfg = get_config()
    job = state.get("job") or {}
    spec: JobSpec | None = state.get("job_spec")
    fit: FitReport | None = state.get("fit_report")
    client = state.get("client") or {}

    skill = (spec.required_skills[0] if spec and spec.required_skills
             else (job.get("required_skills") or ["python"])[0])

    stats: dict[str, Any] = {}
    degraded: list[str] = []
    try:
        stats = await call_tool("pricing", "marketplace", "get_market_rates", {"skill": skill})
    except Exception:  # noqa: BLE001
        degraded.append("marketplace")

    connects_info: dict[str, Any] = {}
    try:
        connects_info = await call_tool("pricing", "marketplace", "get_connects_balance", {})
    except Exception:  # noqa: BLE001
        pass

    budget = job.get("budget") or {}
    is_hourly = (budget.get("type") == "hourly") or bool(spec and spec.is_hourly)

    pricing_cfg = cfg.section("bidding.pricing")
    min_hourly = float(pricing_cfg.get("minimum_effective_hourly_usd", 18.0))

    # --- fair band -------------------------------------------------------
    if is_hourly:
        low = float(stats.get("hourly_p25") or 0) or float(budget.get("min") or 0) or min_hourly
        median = float(stats.get("hourly_median") or 0) or low * 1.25
        high = float(stats.get("hourly_p75") or 0) or median * 1.4
    else:
        low = float(stats.get("awarded_p25") or 0) or float(budget.get("min") or 0)
        median = float(stats.get("awarded_median") or 0) or (low * 1.3 if low else 0.0)
        high = float(stats.get("awarded_p75") or 0) or (median * 1.5 if median else 0.0)

    budget_max = float(budget.get("max") or 0)
    # The client's stated ceiling constrains the band, but does not define it.
    if budget_max and high > budget_max * 1.2:
        high = budget_max * 1.2
    if budget_max and low > budget_max:
        low = budget_max * 0.8

    fit_score = fit.score if fit else 0.5
    # Strong fit bids nearer p75; weak fit nearer p25. Never outside the band.
    position = 0.25 + 0.5 * fit_score
    amount = low + (high - low) * position if high > low else (median or low or budget_max)

    client_generosity = float(client.get("avg_hourly_paid_usd") or 0)
    if client_generosity and is_hourly and client_generosity > median:
        amount = min(high, amount * 1.08)

    amount = round(max(amount, 0.0), 2)

    # --- effective hourly -------------------------------------------------
    hours = float(spec.estimated_hours or 0) if spec else 0.0
    if not hours:
        days = float((spec.timeline_days if spec else None) or job.get("duration_days") or 14)
        complexity_hours = {"low": 0.9, "medium": 1.6, "high": 2.6}.get(
            spec.complexity if spec else "medium", 1.6
        )
        hours = max(4.0, days * complexity_hours)
    effective_hourly = amount if is_hourly else round(amount / max(hours, 1.0), 2)

    below_floor = effective_hourly < min_hourly
    if below_floor and not is_hourly:
        floor_amount = round(min_hourly * hours, 2)
        if budget_max and floor_amount > budget_max * 1.2:
            # The job genuinely cannot clear the floor. Say so rather than bid low.
            amount = floor_amount
        else:
            amount = floor_amount
            effective_hourly = min_hourly
            below_floor = False

    inside_band = (low <= amount <= high) if high > low else True

    # --- rationale --------------------------------------------------------
    context = (
        f"Skill: {skill}\n"
        f"Market (awarded, n={stats.get('sample_size','?')}): p25={low:.0f} median={median:.0f} p75={high:.0f}\n"
        f"Job budget: {budget.get('min')} - {budget.get('max')} ({budget.get('type','fixed')})\n"
        f"Fit score: {fit_score:.2f}\n"
        f"Competing proposals: {job.get('proposals_received','?')}\n"
        f"Client average paid hourly: {client_generosity or 'unknown'}\n"
        f"Computed bid: {amount:.2f} ({'hourly' if is_hourly else 'fixed'}), "
        f"effective hourly {effective_hourly:.2f} over an estimated {hours:.0f} hours\n"
        f"Minimum acceptable effective hourly: {min_hourly:.2f}\n"
        f"{'WARNING: this job cannot clear the floor rate.' if below_floor else ''}"
    )
    rationale = ""
    try:
        result = await llm().complete(
            "pricing",
            [system_message("pricing"), {"role": "user",
             "content": context + "\n\nWrite the two-sentence rationale only."}],
            ledger=ledger,
        )
        rationale = result.text.strip()[:600]
    except Exception:  # noqa: BLE001
        rationale = (
            f"Comparable {skill} work was awarded between ${low:,.0f} and ${high:,.0f} "
            f"(median ${median:,.0f}). At a fit of {fit_score:.0%} this bid sits "
            f"{'above' if amount > median else 'below'} the median, giving an effective "
            f"rate of ${effective_hourly:,.0f}/hour."
        )

    quote = PriceQuote(
        job_id=job.get("job_id", ""),
        amount_usd=amount,
        is_hourly=is_hourly,
        fair_band_low=round(low, 2),
        fair_band_high=round(high, 2),
        market_median=round(median, 2),
        effective_hourly_usd=effective_hourly,
        inside_fair_band=inside_band,
        rationale=rationale,
        comparable_job_ids=[f"market:{skill}:n={stats.get('sample_size', 0)}"],
    )

    update: dict[str, Any] = {"price_quote": quote, "status": "pricing"}
    if degraded:
        update["degraded_services"] = degraded
    if below_floor:
        update["errors"] = [
            f"pricing: job cannot clear the ${min_hourly:.0f}/hour floor "
            f"(effective ${effective_hourly:.2f}); flagged for the user."
        ]
    if connects_info.get("balance") is not None:
        update["node_timings"] = {"pricing_connects_balance": connects_info.get("balance")}
    return update
