from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


SynthType = Literal["element", "item", "equipment", "creature", "concept"]
Operation = Literal["add", "subtract"]
Decision = Literal["matched_existing", "created_new", "failed", "merged_existing", "created_pending", "rejected"]
ObjectStatus = Literal["active", "pending", "rejected", "merged", "banned", "archived"]
RouteEdgeStatus = Literal["active", "disabled"]
ObjectId = int


class SynthObject(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: ObjectId | None = None
    name: str
    emoji: str | None = None
    type: SynthType
    description: str | None = None
    character_summary: str | None = None
    background: str | None = None
    specialty: str | None = None
    weakness: str | None = None
    source: str | None = None
    is_banned: bool = False
    ban_reason: str | None = None
    banned_at: str | None = None
    banned_by_user_id: int | None = None
    first_discoverer_id: int | None = None
    first_discoverer_nickname: str | None = None
    created_at: str | None = None
    discovered_at: str | None = None
    discovery_method: str | None = None
    is_first_discoverer: bool = False
    category_ids: list[int] = Field(default_factory=list)
    status: ObjectStatus = "active"
    canonical_id: ObjectId | None = None
    quality_flags: list[str] = Field(default_factory=list)


class CraftOptions(BaseModel):
    allow_banned: bool = False
    match_threshold: float = 0.78
    review_threshold: float = 0.62
    max_candidates: int = 10
    max_retrieved: int = 40
    vector_top_k: int = 10
    use_vectors: bool = True
    use_llm: bool = True
    explain: bool = True
    persist: bool = True


class CraftRequest(BaseModel):
    operation: Operation
    ingredient_a: SynthObject
    ingredient_b: SynthObject
    options: CraftOptions = Field(default_factory=CraftOptions)


class CandidateObject(BaseModel):
    name: str
    type: SynthType
    description: str
    emoji: str | None = None
    core_tags: list[str] = Field(default_factory=list)
    anchors: list[str] = Field(default_factory=list)
    source_reason: str


class ScoreBreakdown(BaseModel):
    semantic_similarity: float = 0.0
    name_similarity: float = 0.0
    anchor_bonus: float = 0.0
    type_compatibility_bonus: float = 0.0
    route_prior_bonus: float = 0.0
    contradiction_penalty: float = 0.0
    over_generic_penalty: float = 0.0
    total: float = 0.0


class MatchScore(BaseModel):
    object_id: ObjectId
    name: str
    type: SynthType
    score: ScoreBreakdown
    reasons: list[str] = Field(default_factory=list)


class CraftResult(BaseModel):
    success: bool
    result: SynthObject | None = None
    failure_reason: str | None = None
    decision: Decision
    cached: bool = False
    candidate: CandidateObject | None = None
    matched_object_id: ObjectId | None = None
    score_breakdown: ScoreBreakdown | None = None
    top_matches: list[MatchScore] = Field(default_factory=list)
    candidate_errors: list[str] = Field(default_factory=list)
    explanation: str


class RouteEdge(BaseModel):
    result_id: ObjectId
    result_name: str | None = None
    result_type: SynthType | None = None
    result_description: str | None = None
    operation: Operation
    a_id: ObjectId
    a_name: str | None = None
    a_type: SynthType | None = None
    a_description: str | None = None
    b_id: ObjectId
    b_name: str | None = None
    b_type: SynthType | None = None
    b_description: str | None = None
    status: RouteEdgeStatus = "active"


JsonDict = dict[str, Any]
