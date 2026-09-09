"""The judge interface required by the brief.

    judge(trajectory, tool_schemas, case) -> {verdict, confidence, category}

Consumes the trajectory only: messages, tool calls, tool returns, tool schemas
and the case text. It never touches backend state, gold actions or
``goal_state``, and it neither imports nor executes Harbour.
"""

from __future__ import annotations

import os
from typing import Any, Iterable

import numpy as np

from .features import load_tool_schemas
from .llm import LLMAdjudicator, Ledger
from .model import StructuralJudge, confidence_from_probability
from .taxonomy import categories_for, primary_category

DEFAULT_MODEL_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "artifacts", "structural.pkl"
)

# Rows whose calibrated probability lands inside this band are the ones an LLM
# call can still change. Chosen on dev: ~17% of rows, ~48% of residual errors.
DEFAULT_BAND = (0.20, 0.80)


def _as_row(trajectory: dict, case: dict | None, row_id: str) -> dict:
    traj = dict(trajectory or {})
    if case is not None and "case" not in traj:
        traj["case"] = case
    return {"id": row_id, "trajectory": traj}


class Judge:
    """Loaded-once judge, reusable across many trajectories."""

    def __init__(
        self,
        model_path: str = DEFAULT_MODEL_PATH,
        schemas: dict[str, dict] | str | None = None,
        use_llm: bool = False,
        band: tuple[float, float] = DEFAULT_BAND,
        llm: LLMAdjudicator | None = None,
        stacker: Any | None = None,
    ) -> None:
        self.model = StructuralJudge.load(model_path)
        if isinstance(schemas, str):
            self.schemas = load_tool_schemas(schemas)
        elif isinstance(schemas, dict):
            self.schemas = schemas
        else:
            # artifacts/tool_schemas.json is copied there by reproduce.sh, so a
            # loaded model stays usable without the fixture directory present.
            self.schemas = load_tool_schemas(
                os.path.join(os.path.dirname(DEFAULT_MODEL_PATH), "tool_schemas.json")
            )
        self.use_llm = use_llm
        self.band = band
        self.llm = llm or (LLMAdjudicator(ledger=Ledger()) if use_llm else None)
        self.stacker = stacker

    # -- batch -----------------------------------------------------------

    def judge_rows(self, rows: list[dict]) -> list[dict]:
        if not rows:
            return []
        probs, gates = self.model.probabilities(rows, self.schemas)
        out: list[dict] = []
        for i, row in enumerate(rows):
            prob = float(probs[i])
            escalated_to_llm = False
            llm_reply: dict[str, Any] | None = None

            if (
                self.use_llm
                and self.llm is not None
                and not bool(gates[i])
                and self.band[0] < prob < self.band[1]
            ):
                llm_reply = self.llm.adjudicate(row)
                escalated_to_llm = True
                if llm_reply.get("ok"):
                    prob = _blend(prob, llm_reply, self.stacker)

            verdict = int(prob >= self.model.threshold)
            confidence = float(confidence_from_probability(np.asarray([prob]))[0])
            cats = categories_for(row, verdict)
            out.append(
                {
                    "id": row.get("id", ""),
                    "verdict": verdict,
                    "confidence": round(confidence, 4),
                    "category": cats[0],
                    "all_categories": cats,
                    "probability": round(prob, 6),
                    "gate_fired": bool(gates[i]),
                    "llm_consulted": escalated_to_llm,
                    "llm_why": (llm_reply or {}).get("why", "") if llm_reply else "",
                }
            )
        return out

    # -- single ----------------------------------------------------------

    def judge_one(
        self, trajectory: dict, tool_schemas: Iterable[dict] | None = None,
        case: dict | None = None, row_id: str = "",
    ) -> dict:
        if tool_schemas is not None:
            self.schemas = {e["name"]: e for e in tool_schemas}
        result = self.judge_rows([_as_row(trajectory, case, row_id)])[0]
        return {
            "verdict": result["verdict"],
            "confidence": result["confidence"],
            "category": result["category"],
        }


def _blend(structural_prob: float, llm_reply: dict, stacker: Any | None) -> float:
    """Combine the structural probability with the LLM's opinion.

    Without a fitted stacker, fall back to a bounded logit nudge: the LLM can
    move a borderline row across the line but cannot overturn a confident
    structural call, which is the behaviour we want from an advisory layer.
    """
    import math

    verdict = llm_reply.get("verdict")
    conf = float(llm_reply.get("confidence") or 0.6)
    if verdict not in (0, 1):
        return structural_prob

    if stacker is not None:
        p = min(max(structural_prob, 1e-6), 1 - 1e-6)
        z = math.log(p / (1 - p))
        return float(stacker.predict_proba([[z, float(verdict), conf]])[0][1])

    p = min(max(structural_prob, 1e-6), 1 - 1e-6)
    z = math.log(p / (1 - p))
    # Strength scales with how sure the model says it is, capped so a single
    # call cannot dominate.
    strength = 1.4 * max(0.0, (conf - 0.5) / 0.5)
    z += strength if verdict == 1 else -strength
    return 1.0 / (1.0 + math.exp(-z))


def judge(trajectory: dict, tool_schemas: Iterable[dict], case: dict | None = None) -> dict:
    """One-shot convenience entry point matching the brief's signature.

    Loads the model on every call, so prefer :class:`Judge` for batches.
    """
    return Judge(schemas={e["name"]: e for e in tool_schemas}).judge_one(
        trajectory, None, case
    )


__all__ = ["Judge", "judge", "primary_category", "DEFAULT_BAND"]
