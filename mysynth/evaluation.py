from __future__ import annotations

from dataclasses import dataclass

from .engine import RuleSynthesizerEngine
from .models import CraftOptions, CraftRequest
from .store import SQLiteObjectStore


@dataclass
class EvaluationCounters:
    total: int
    exact_id_match: int
    name_match: int
    type_match: int
    top5_match: int

    def as_dict(self) -> dict[str, float | int]:
        if self.total == 0:
            return {
                "total": 0,
                "exact_id_match": 0,
                "name_match": 0,
                "type_match": 0,
                "top5_match": 0,
            }
        return {
            "total": self.total,
            "exact_id_match": self.exact_id_match / self.total,
            "name_match": self.name_match / self.total,
            "type_match": self.type_match / self.total,
            "top5_match": self.top5_match / self.total,
        }


@dataclass
class EvaluationFailure:
    operation: str
    input_types: str
    a_id: int
    b_id: int
    expected_id: int
    expected_name: str
    actual_id: int | None
    actual_name: str | None
    decision: str
    candidate_name: str | None
    top_matches: list[dict[str, object]]


@dataclass
class EvaluationSummary(EvaluationCounters):
    buckets: dict[str, EvaluationCounters]
    failures: list[EvaluationFailure]

    def as_dict(self) -> dict[str, object]:
        result = super().as_dict()
        result["buckets"] = {key: value.as_dict() for key, value in sorted(self.buckets.items())}
        result["failures"] = [failure.__dict__ for failure in self.failures]
        return result


def evaluate_routes(
    store: SQLiteObjectStore,
    limit: int | None = None,
    max_failures: int = 20,
) -> EvaluationSummary:
    engine = RuleSynthesizerEngine(store)
    edges = store.list_route_edges(limit=limit)
    total = exact = name = type_match = top5 = 0
    buckets: dict[str, EvaluationCounters] = {}
    failures: list[EvaluationFailure] = []
    for edge in edges:
        a = store.get_object(edge.a_id)
        b = store.get_object(edge.b_id)
        expected = store.get_object(edge.result_id)
        if a is None or b is None or expected is None:
            continue
        bucket_key = f"{edge.operation}:{a.type}+{b.type}->{expected.type}"
        bucket = buckets.setdefault(bucket_key, EvaluationCounters(0, 0, 0, 0, 0))
        request = CraftRequest(
            operation=edge.operation,
            ingredient_a=a,
            ingredient_b=b,
            options=CraftOptions(persist=False),
        )
        result = engine.craft(request)
        total += 1
        bucket.total += 1
        if result.result is None:
            _append_failure(failures, max_failures, edge, expected, result, bucket_key)
            continue
        exact_hit = result.result.id == expected.id
        name_hit = result.result.name == expected.name
        type_hit = result.result.type == expected.type
        top5_hit = exact_hit or any(match.object_id == expected.id for match in result.top_matches[:5])

        if exact_hit:
            exact += 1
            bucket.exact_id_match += 1
        if name_hit:
            name += 1
            bucket.name_match += 1
        if type_hit:
            type_match += 1
            bucket.type_match += 1
        if top5_hit:
            top5 += 1
            bucket.top5_match += 1
        if not exact_hit:
            _append_failure(failures, max_failures, edge, expected, result, bucket_key)
    return EvaluationSummary(
        total=total,
        exact_id_match=exact,
        name_match=name,
        type_match=type_match,
        top5_match=top5,
        buckets=buckets,
        failures=failures,
    )


def _append_failure(
    failures: list[EvaluationFailure],
    max_failures: int,
    edge,
    expected,
    result,
    bucket_key: str,
) -> None:
    if len(failures) >= max_failures:
        return
    failures.append(
        EvaluationFailure(
            operation=edge.operation,
            input_types=bucket_key,
            a_id=edge.a_id,
            b_id=edge.b_id,
            expected_id=edge.result_id,
            expected_name=expected.name,
            actual_id=result.result.id if result.result else None,
            actual_name=result.result.name if result.result else None,
            decision=result.decision,
            candidate_name=result.candidate.name if result.candidate else None,
            top_matches=[
                {
                    "object_id": match.object_id,
                    "name": match.name,
                    "score": match.score.total,
                }
                for match in result.top_matches[:5]
            ],
        )
    )
