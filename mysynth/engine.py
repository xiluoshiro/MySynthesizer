from __future__ import annotations

from datetime import UTC, datetime

from .candidate_generators import CandidateGenerator, CompositeCandidateGenerator, LLMCandidateGenerator, RuleCandidateGenerator
from .candidates import EMOJI_BY_TYPE
from .embeddings import (
    EmbeddingProvider,
    SQLiteVectorIndex,
    build_candidate_embedding_text,
    build_request_embedding_text,
)
from .features import extract_features
from .intent import plan_intent
from .models import CandidateObject, CraftRequest, CraftResult, MatchScore, SynthObject
from .normalize import recipe_key
from .ranking import score_match
from .store import ObjectStore


class RuleSynthesizerEngine:
    def __init__(
        self,
        store: ObjectStore,
        *,
        vector_index: SQLiteVectorIndex | None = None,
        embedding_provider: EmbeddingProvider | None = None,
        candidate_generator: CandidateGenerator | None = None,
        llm_candidate_generator: CandidateGenerator | None = None,
    ) -> None:
        self.store = store
        self.vector_index = vector_index
        self.embedding_provider = embedding_provider
        self.candidate_generator = candidate_generator or RuleCandidateGenerator()
        self.llm_candidate_generator = llm_candidate_generator

    def craft(self, request: CraftRequest) -> CraftResult:
        # 先处理失败和缓存路径，避免无谓生成候选。
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
            direct_result = self._direct_route_result(request)
            if direct_result is not None:
                result = CraftResult(
                    success=True,
                    result=self._result_object(direct_result),
                    decision="matched_existing",
                    cached=False,
                    matched_object_id=direct_result.id,
                    explanation=(
                        f"direct route 命中：{request.ingredient_a.name} {request.operation} "
                        f"{request.ingredient_b.name} -> {direct_result.name}"
                    ),
                )
                if request.options.persist:
                    self.store.save_craft_result(request, result)
                return result

        a_features = extract_features(request.ingredient_a)
        b_features = extract_features(request.ingredient_b)
        intent = plan_intent(request.ingredient_a, request.ingredient_b, a_features, b_features, request.operation)
        # 候选生成和匹配分离；LLM 默认进入候选层，失败后由规则兜底。
        candidate_generator = self._candidate_generator(request)
        candidates = candidate_generator.generate(
            request.ingredient_a,
            request.ingredient_b,
            a_features,
            b_features,
            intent,
            request.options.max_candidates,
        )
        candidate_errors = _candidate_errors(candidate_generator)
        if not candidates:
            result = CraftResult(
                success=False,
                failure_reason="no_candidates",
                decision="failed",
                explanation="候选生成失败",
                candidate_errors=candidate_errors,
            )
            if request.options.persist:
                self.store.save_craft_result(request, result)
            return result

        best_candidate, top_matches = self._rank_candidates(candidates, request)
        best_match = top_matches[0] if top_matches else None

        # 只有强匹配才复用已有对象，弱匹配走本地新候选。
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
                    candidate_errors=candidate_errors,
                    explanation=self._match_explanation(request, best_candidate, best_match),
                )
                if request.options.persist:
                    self.store.save_craft_result(request, result)
                return result

        duplicate = self._duplicate_by_name(best_candidate)
        if duplicate is not None:
            result = CraftResult(
                success=True,
                result=self._result_object(duplicate),
                decision="merged_existing",
                cached=False,
                candidate=best_candidate,
                matched_object_id=duplicate.id,
                score_breakdown=best_match.score if best_match else None,
                top_matches=top_matches[:5],
                candidate_errors=candidate_errors,
                explanation=self._merge_explanation(best_candidate, duplicate),
            )
            if request.options.persist:
                self.store.save_craft_result(request, result)
            return result

        created = self._construct_new_object(best_candidate)
        result = CraftResult(
            success=True,
            result=created,
            decision="created_pending",
            cached=False,
            candidate=best_candidate,
            score_breakdown=best_match.score if best_match else None,
            top_matches=top_matches[:5],
            candidate_errors=candidate_errors,
            explanation=self._create_explanation(request, best_candidate, best_match),
        )
        if request.options.persist:
            self.store.save_craft_result(request, result)
        return result

    def _candidate_generator(self, request: CraftRequest) -> CandidateGenerator:
        if not request.options.use_llm:
            return self.candidate_generator
        llm_generator = self.llm_candidate_generator or LLMCandidateGenerator()
        return CompositeCandidateGenerator([llm_generator, self.candidate_generator])

    def _validate(self, request: CraftRequest) -> str | None:
        if request.ingredient_a.name.strip() == "" or request.ingredient_b.name.strip() == "":
            return "ingredient name cannot be empty"
        if not request.options.allow_banned and (request.ingredient_a.is_banned or request.ingredient_b.is_banned):
            return "banned ingredients are not allowed"
        if request.ingredient_a.status != "active" or request.ingredient_b.status != "active":
            return "inactive ingredients are not allowed"
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
                # 同一已有对象只保留最高分候选解释。
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

        # 混合召回：候选文本和 type/token 兜底；不做单边邻接扩散。
        add_many(self.store.search_candidates(candidate, request.options.max_retrieved))
        add_many(self._vector_objects_for_candidate(candidate, request))
        add_many(self._vector_recipes_for_request(request))
        add_many(self.store.search_by_type_and_tokens(candidate.type, candidate.core_tags, limit=20))
        return objects[: request.options.max_retrieved]

    def _route_prior(self, request: CraftRequest, object_id: int | None) -> float:
        return 0.0

    def _direct_route_result(self, request: CraftRequest) -> SynthObject | None:
        a_id = request.ingredient_a.id
        b_id = request.ingredient_b.id
        if a_id is None or b_id is None:
            return None
        edges = self.store.find_edges_by_inputs(a_id, b_id, request.operation)
        for edge in edges:
            result = self.store.get_object(edge.result_id)
            if result is not None:
                return result
        return None

    def _vector_objects_for_candidate(self, candidate: CandidateObject, request: CraftRequest) -> list[SynthObject]:
        if not request.options.use_vectors or self.vector_index is None or self.embedding_provider is None:
            return []
        results = self.vector_index.search_text(
            build_candidate_embedding_text(candidate),
            self.embedding_provider,
            owner_type="object",
            top_k=request.options.vector_top_k,
        )
        objects: list[SynthObject] = []
        for result in results:
            try:
                object_id = int(result.owner_id)
            except ValueError:
                continue
            obj = self.store.get_object(object_id)
            if obj is not None:
                objects.append(obj)
        return objects

    def _vector_recipes_for_request(self, request: CraftRequest) -> list[SynthObject]:
        if not request.options.use_vectors or self.vector_index is None or self.embedding_provider is None:
            return []
        results = self.vector_index.search_text(
            build_request_embedding_text(request),
            self.embedding_provider,
            owner_type="recipe",
            top_k=request.options.vector_top_k,
        )
        objects: list[SynthObject] = []
        for result in results:
            result_id = _recipe_result_id(result.owner_id)
            if result_id is None:
                continue
            obj = self.store.get_object(result_id)
            if obj is not None:
                objects.append(obj)
        return objects

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
            status="pending",
            quality_flags=["pending_review"],
        )

    def _result_object(self, obj: SynthObject) -> SynthObject:
        # 完整对象可能带大量 craft_sources，响应只保留稳定字段。
        payload = obj.model_dump(mode="json", exclude={"craft_sources"})
        return SynthObject.model_validate(payload)

    def _duplicate_by_name(self, candidate: CandidateObject) -> SynthObject | None:
        matches = self.store.get_by_name(candidate.name)
        for match in matches:
            if match.type == candidate.type:
                return match
        return None

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

    def _merge_explanation(self, candidate: CandidateObject, duplicate: SynthObject) -> str:
        return f"候选「{candidate.name}」与已有 active 对象「{duplicate.name}」名称归一化一致，因此归并到 canonical 对象。"

    def _create_explanation(
        self,
        request: CraftRequest,
        candidate: CandidateObject,
        match: MatchScore | None,
    ) -> str:
        if match is None:
            return f"未召回足够相近的已有对象，按候选「{candidate.name}」创建 pending 对象，等待质量确认。"
        return (
            f"最佳已有对象「{match.name}」评分 {match.score.total:.2f}，未达到命中阈值 "
            f"{request.options.match_threshold:.2f}。因此按候选「{candidate.name}」创建 pending 对象，等待质量确认。"
        )


def _recipe_result_id(owner_id: str) -> int | None:
    parts = owner_id.split(":")
    if len(parts) != 4:
        return None
    try:
        return int(parts[3])
    except ValueError:
        return None


def _candidate_errors(generator: CandidateGenerator) -> list[str]:
    errors = getattr(generator, "last_errors", None)
    if isinstance(errors, list):
        return [str(error) for error in errors]
    error = getattr(generator, "last_error", None)
    return [str(error)] if error else []
