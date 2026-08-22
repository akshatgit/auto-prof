"""Token throughput and cost, computed from recorded samples.

Three questions the dashboard has to answer: how much has this installation
produced, how fast is it going right now, and what is that costing. The first
two come from `token_samples`, which the job heartbeat writes as cumulative
points; a rate is the difference between two of them.

Prices are NOT built in. A wrong hardcoded rate is worse than no number,
because it looks authoritative. Configure them per model and the cost appears;
leave them unset and the dashboard says so.
"""
from __future__ import annotations

import os
import sqlite3

# Per MILLION tokens, by model, as {model: {"input":, "cached":, "output":}}.
# Read from AUTOPROF_PRICE_<MODEL>_<KIND> (dots and dashes become underscores),
# e.g. AUTOPROF_PRICE_GPT_5_5_OUTPUT=10.0
_KINDS = ("input", "cached", "output")


def _env_key(model: str, kind: str) -> str:
    safe = model.upper().replace("-", "_").replace(".", "_")
    return f"AUTOPROF_PRICE_{safe}_{kind.upper()}"


def price_for(model: str | None, kind: str, env=None) -> float | None:
    """Price per million tokens, or None when it has not been configured."""
    env = env if env is not None else os.environ
    if not model:
        return None
    raw = env.get(_env_key(model, kind))
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def totals(conn: sqlite3.Connection) -> dict:
    """Lifetime token counts across every job that reported usage."""
    row = conn.execute(
        "SELECT COALESCE(SUM(progress_tokens),0) produced, "
        "COALESCE(SUM(progress_input_tokens),0) input, "
        "COALESCE(SUM(progress_cached_tokens),0) cached, "
        "COUNT(*) jobs FROM jobs WHERE progress_at IS NOT NULL"
    ).fetchone()
    return {"produced": row["produced"], "input": row["input"],
            "cached": row["cached"], "jobs": row["jobs"]}


def rate(conn: sqlite3.Connection, minutes: int = 20) -> dict:
    """Tokens produced in the last `minutes`, and the per-hour rate.

    Summed per job as (max - min) within the window, because samples are
    cumulative per job: subtracting a job's own endpoints avoids counting the
    history it accumulated before the window opened.
    """
    rows = conn.execute(
        "SELECT job_id, MAX(produced_tokens) - MIN(produced_tokens) AS produced, "
        "MAX(input_tokens) - MIN(input_tokens) AS input "
        "FROM token_samples WHERE sampled_at >= datetime('now', ?) "
        "GROUP BY job_id",
        (f"-{int(minutes)} minutes",),
    ).fetchall()
    produced = sum(r["produced"] or 0 for r in rows)
    consumed = sum(r["input"] or 0 for r in rows)
    per_hour = produced * 60.0 / minutes if minutes else 0.0
    return {"minutes": minutes, "produced": produced, "input": consumed,
            "per_hour": per_hour, "jobs": len(rows)}


def cost(conn: sqlite3.Connection, env=None) -> dict:
    """Estimated spend by model, and which models have no price configured."""
    rows = conn.execute(
        "SELECT COALESCE(backend_model, backend) AS model, "
        "COALESCE(SUM(progress_tokens),0) produced, "
        "COALESCE(SUM(progress_input_tokens),0) input, "
        "COALESCE(SUM(progress_cached_tokens),0) cached "
        "FROM jobs WHERE progress_at IS NOT NULL GROUP BY model"
    ).fetchall()
    by_model, unpriced, total = [], [], 0.0
    for r in rows:
        # Jobs dispatched before the backend was recorded have no model. They
        # still hold real tokens, so name them rather than dropping them.
        model = r["model"] or "(unrecorded backend)"
        prices = {k: price_for(r["model"], k, env) for k in _KINDS}
        if all(p is None for p in prices.values()):
            unpriced.append(model)
            continue
        # Cached input is billed separately where a price is given; otherwise
        # it is left out rather than guessed at the full input rate.
        uncached = max((r["input"] or 0) - (r["cached"] or 0), 0)
        amount = (
            uncached / 1e6 * (prices["input"] or 0.0)
            + (r["cached"] or 0) / 1e6 * (prices["cached"] or 0.0)
            + (r["produced"] or 0) / 1e6 * (prices["output"] or 0.0)
        )
        total += amount
        by_model.append({"model": model, "amount": amount,
                         "produced": r["produced"], "input": r["input"]})
    return {"total": total, "by_model": by_model, "unpriced": sorted(set(unpriced))}
