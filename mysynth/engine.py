from __future__ import annotations

from datetime import UTC, datetime

from .candidates import EMOJI_BY_TYPE, generate_candidates
from .features import extract_features
from .intent import plan_intent
from .models import CandidateObject, CraftRequest, CraftResult, MatchScore, SynthObject
from .normalize import recipe_key
from .ranking import score_match
from .store import ObjectStore


class RuleSynthesizerEngine:
    def __init__(self, store: ObjectStore) -> None:
        self.store = store

    def craft(self, request: CraftRequest) -> CraftResult:
        failure = self._validate(request)
        if failure is not None:
            result = CraftResult(success=False, failure_reason=failure, decision="failed", explanation=failure)
            if request.options.persist:
                self.store.save_craft_result(request, result)
            return result

        a_id = request.ingredient_a.id
        b_id = request.ingredient_b.id
        if a_id is not None and b_id is not None:
            cached = self.store.find_recipe(recipe_key(a_id, b_id, request.operation))
            if cached is not None:
                result = CraftResult(
                    success=True,
                    result=self._result_object(cached),
                    decision="matched_existing",
                    cached=True,
                    matched_object_id=cached.id,
                    explanation=f"recipe cache 命中：{request.ingredient_a.name} {request.operation} {request.ingredient_b.name} -> {cached.name}",
                )
                return result

        a_features = extract_features(request.ingredient_a)
        b_features = extract_features(request.ingredient_b)
        intent = plan_intent(request.ingredient_a, request.ingredient_b, a_features, b_features, request.operation)
        candidates = generate_candidates(
            request.ingredient_a,
            request.ingredient_b,
            a_features,
            b_features,
            intent,
            request.options.max_candidates,
        )
        if not candidates:
            result = CraftResult(success=False, failure_reason="no_candidates", decision="failed", explanation="候选生成失败")
            if request.options.persist:
                self.store.save_craft_result(request, result)
            return result

        best_candidate, top_matches = self._rank_candidates(candidates, request)
        best_match = top_matches[0] if top_matches else None

        if best_match and best_match.score.total >= request.options.match_threshold:
            matched = self.store.get_object(best_match.object_id)
            if matched is not None:
                result = CraftResult(
                    success=True,
                    result=self._result_object(matched),
                    decision="matched_existing",
                    cached=False,
                    candidate=best_candidate,
                    matched_object_id=matched.id,
                    score_breakdown=best_match.score,
                    top_matches=top_matches[:5],
                    explanation=self._match_explanation(request, best_candidate, best_match),
                )
                if request.options.persist:
                    self.store.save_craft_result(request, result)
                return result

        created = self._construct_new_object(best_candidate)
        result = CraftResult(
            success=True,
            result=created,
            decision="created_new",
            cached=False,
            candidate=best_candidate,
            score_breakdown=best_match.score if best_match else None,
            top_matches=top_matches[:5],
            explanation=self._create_explanation(request, best_candidate, best_match),
        )
        if request.options.persist:
            self.store.save_craft_result(request, result)
        return result

    def _validate(self, request: CraftRequest) -> str | None:
        if request.ingredient_a.name.strip() == "" or request.ingredient_b.name.strip() == "":
            return "ingredient name cannot be empty"
        if not request.options.allow_banned and (request.ingredient_a.is_banned or request.ingredient_b.is_banned):
            return "banned ingredients are not allowed"
        return None

    def _rank_candidates(
        self,
        candidates: list[CandidateObject],
        request: CraftRequest,
    ) -> tuple[CandidateObject, list[MatchScore]]:
        scored: list[tuple[CandidateObject, MatchScore]] = []
        best_by_object: dict[int, tuple[CandidateObject, MatchScore]] = {}
        for candidate in candidates:
            retrieved = self._retrieve_for_candidate(candidate, request)
            for obj in retrieved:
                route_prior = self._route_prior(request, obj.id)
                match = score_match(candidate, obj, route_prior=route_prior)
                current = best_by_object.get(match.object_id)
                if current is None or match.score.total > current[1].score.total:
                    best_by_object[match.object_id] = (candidate, match)
        scored = list(best_by_object.values())
        scored.sort(key=lambda item: item[1].score.total, reverse=True)
        if scored:
            return scored[0][0], [item[1] for item in scored]
        return candidates[0], []

    def _retrieve_for_candidate(self, candidate: CandidateObject, request: CraftRequest) -> list[SynthObject]:
        objects: list[SynthObject] = []
        seen: set[int] = set()

        def add_many(values: list[SynthObject]) -> None:
            for value in values:
                if value.id is not None and value.id not in seen:
                    seen.add(value.id)
                    objects.append(value)

        add_many(self.store.search_candidates(candidate, request.options.max_retrieved))
        if request.ingredient_a.id is not None:
            add_many(self.store.get_neighbors(request.ingredient_a.id, limit=20))
        if request.ingredient_b.id is not None:
            add_many(self.store.get_neighbors(request.ingredient_b.id, limit=20))
        if request.ingredient_a.id is not None and request.ingredient_b.id is not None:
            direct_edges = self.store.find_edges_by_inputs(
                request.ingredient_a.id,
                request.ingredient_b.id,
                request.operation,
            )
            add_many([obj for edge in direct_edges if (obj := self.store.get_object(edge.result_id)) is not None])
        add_many(self.store.search_by_type_and_tokens(candidate.type, candidate.core_tags, limit=20))
        return objects[: request.options.max_retrieved]

    def _route_prior(self, request: CraftRequest, object_id: int | None) -> float:
        if object_id is None:
            return 0.0
        a_neighbors = getattr(self.store, "neighbors_by_object", {}).get(request.ingredient_a.id, set())
        b_neighbors = getattr(self.store, "neighbors_by_object", {}).get(request.ingredient_b.id, set())
        if object_id in a_neighbors and object_id in b_neighbors:
            return 0.10
        if object_id in a_neighbors or object_id in b_neighbors:
            return 0.05
        return 0.0

    def _construct_new_object(self, candidate: CandidateObject) -> SynthObject:
        now = datetime.now(UTC).isoformat()
        return SynthObject(
            id=None,
            name=candidate.name,
            emoji=candidate.emoji or EMOJI_BY_TYPE[candidate.type],
            type=candidate.type,
            description=candidate.description,
            source="local_engine",
            is_banned=False,
            created_at=now,
            discovered_at=now,
            discovery_method="local_engine",
            is_first_discoverer=True,
            category_ids=[],
        )

    def _result_object(self, obj: SynthObject) -> SynthObject:
        payload = obj.model_dump(mode="json", exclude={"craft_sources"})
        return SynthObject.model_validate(payload)

    def _match_explanation(
        self,
        request: CraftRequest,
        candidate: CandidateObject,
        match: MatchScore,
    ) -> str:
        reasons = "；".join(match.reasons) if match.reasons else "综合评分最高"
        return (
            f"候选「{candidate.name}」来自 {candidate.source_reason}。"
            f"已有对象「{match.name}」评分 {match.score.total:.2f}，达到命中阈值；{reasons}。"
            f"因此返回已有对象。"
        )

    def _create_explanation(
        self,
        request: CraftRequest,
        candidate: CandidateObject,
        match: MatchScore | None,
    ) -> str:
        if match is None:
            return f"未召回足够相近的已有对象，按候选「{candidate.name}」创建新对象。"
        return (
            f"最佳已有对象「{match.name}」评分 {match.score.total:.2f}，未达到命中阈值 "
            f"{request.options.match_threshold:.2f}。因此按候选「{candidate.name}」创建新对象。"
        )
