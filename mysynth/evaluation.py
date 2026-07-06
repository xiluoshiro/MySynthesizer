from __future__ import annotations

from dataclasses import dataclass

from .engine import RuleSynthesizerEngine
from .models import CraftOptions, CraftRequest
from .store import SQLiteObjectStore


@dataclass
class EvaluationSummary:
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


def evaluate_routes(store: SQLiteObjectStore, limit: int | None = None) -> EvaluationSummary:
    engine = RuleSynthesizerEngine(store)
    edges = store.list_route_edges(limit=limit)
    total = exact = name = type_match = top5 = 0
    for edge in edges:
        a = store.get_object(edge.a_id)
        b = store.get_object(edge.b_id)
        expected = store.get_object(edge.result_id)
        if a is None or b is None or expected is None:
            continue
        request = CraftRequest(
            operation=edge.operation,
            ingredient_a=a,
            ingredient_b=b,
            options=CraftOptions(persist=False),
        )
        result = engine.craft(request)
        total += 1
        if result.result is None:
            continue
        if result.result.id == expected.id:
            exact += 1
            top5 += 1
        if result.result.name == expected.name:
            name += 1
        if result.result.type == expected.type:
            type_match += 1
        elif any(match.object_id == expected.id for match in result.top_matches[:5]):
            top5 += 1
    return EvaluationSummary(total=total, exact_id_match=exact, name_match=name, type_match=type_match, top5_match=top5)
