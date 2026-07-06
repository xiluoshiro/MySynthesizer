from __future__ import annotations

from .features import ObjectFeatures, extract_features, tokenize
from .models import CandidateObject, MatchScore, ScoreBreakdown, SynthObject
from .normalize import normalize_name, object_search_text


GENERIC_SUFFIXES = ("概念", "现象", "系统", "体系", "平台", "对象", "残余")


def score_match(
    candidate: CandidateObject,
    existing: SynthObject,
    route_prior: float = 0.0,
) -> MatchScore:
    existing_features = extract_features(existing)
    candidate_tokens = tokenize(" ".join([candidate.name, candidate.description, *candidate.core_tags, *candidate.anchors]))
    existing_tokens = existing_features.name_tokens

    name_similarity = _name_similarity(candidate.name, existing.name)
    semantic_similarity = _jaccard(candidate_tokens, existing_tokens)
    anchor_bonus = _anchor_bonus(candidate, existing_features)
    type_bonus = 0.12 if candidate.type == existing.type else 0.0
    contradiction_penalty = _contradiction_penalty(candidate, existing)
    over_generic_penalty = _over_generic_penalty(candidate, existing)

    total = (
        semantic_similarity * 0.30
        + name_similarity * 0.25
        + anchor_bonus
        + type_bonus
        + route_prior
        - contradiction_penalty
        - over_generic_penalty
    )
    score = ScoreBreakdown(
        semantic_similarity=round(semantic_similarity * 0.30, 4),
        name_similarity=round(name_similarity * 0.25, 4),
        anchor_bonus=round(anchor_bonus, 4),
        type_compatibility_bonus=round(type_bonus, 4),
        route_prior_bonus=round(route_prior, 4),
        contradiction_penalty=round(contradiction_penalty, 4),
        over_generic_penalty=round(over_generic_penalty, 4),
        total=round(max(0.0, min(1.0, total)), 4),
    )
    return MatchScore(
        object_id=existing.id or 0,
        name=existing.name,
        type=existing.type,
        score=score,
        reasons=_reasons(candidate, existing, score),
    )


def _name_similarity(left: str, right: str) -> float:
    left_name = normalize_name(left)
    right_name = normalize_name(right)
    if not left_name or not right_name:
        return 0.0
    if left_name == right_name:
        return 1.0
    if left_name in right_name or right_name in left_name:
        return 0.72
    return _jaccard(tokenize(left_name), tokenize(right_name))


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _anchor_bonus(candidate: CandidateObject, existing_features: ObjectFeatures) -> float:
    if not candidate.anchors:
        return 0.0
    shared = set(candidate.anchors) & existing_features.anchors
    if shared:
        return 0.15
    existing_text = existing_features.raw_text.casefold()
    if any(anchor.casefold() in existing_text for anchor in candidate.anchors):
        return 0.12
    return 0.0


def _contradiction_penalty(candidate: CandidateObject, existing: SynthObject) -> float:
    text = object_search_text(existing)
    if "不包含" in text and any(tag in text for tag in candidate.core_tags):
        return 0.2
    return 0.0


def _over_generic_penalty(candidate: CandidateObject, existing: SynthObject) -> float:
    if existing.name.endswith(GENERIC_SUFFIXES) and len(candidate.anchors) > 0:
        return 0.08
    if existing.name in {"概念", "系统", "现象", "对象"}:
        return 0.1
    return 0.0


def _reasons(candidate: CandidateObject, existing: SynthObject, score: ScoreBreakdown) -> list[str]:
    reasons: list[str] = []
    if normalize_name(candidate.name) == normalize_name(existing.name):
        reasons.append("名称精确匹配")
    elif score.name_similarity > 0:
        reasons.append("名称存在部分重叠")
    if candidate.type == existing.type:
        reasons.append("type 相同")
    if score.anchor_bonus > 0:
        reasons.append("共享专名锚点")
    if score.semantic_similarity > 0:
        reasons.append("描述或标签语义重叠")
    return reasons
