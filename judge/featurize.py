"""Combined feature vector: transcript structure + deterministic policy checks.

Kept separate from :mod:`judge.features` because :mod:`judge.policy` imports
``ParsedTrajectory`` from there; merging in either would be circular.
"""

from __future__ import annotations

from .features import ParsedTrajectory, extract, load_tool_schemas  # noqa: F401
from .intent import intent_features
from .policy import policy_features


def extract_all(row: dict, schemas: dict[str, dict]) -> dict[str, float]:
    parsed = ParsedTrajectory(row)
    f = extract(row, schemas)
    f.update(policy_features(parsed))
    f.update(intent_features(parsed.message, parsed.audited))
    return f


def feature_names(schemas: dict[str, dict]) -> tuple[str, ...]:
    probe = {
        "id": "m::c_0#r0",
        "trajectory": {
            "case": {"case_id": "c_0", "customer_id": "cu_0", "message": ""},
            "turns": [],
            "audited_tool_calls": [],
        },
    }
    return tuple(extract_all(probe, schemas).keys())


def vectorise(rows: list[dict], schemas: dict[str, dict]) -> tuple[list[list[float]], tuple[str, ...]]:
    names = feature_names(schemas)
    matrix = [[extract_all(row, schemas).get(name, 0.0) for name in names] for row in rows]
    return matrix, names
