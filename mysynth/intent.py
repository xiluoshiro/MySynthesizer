from __future__ import annotations

from dataclasses import dataclass, field

from .features import ObjectFeatures
from .models import Operation, SynthObject, SynthType


@dataclass(frozen=True)
class CraftIntent:
    operation: Operation
    expected_type: SynthType
    preserve_terms: set[str] = field(default_factory=set)
    remove_terms: set[str] = field(default_factory=set)
    anchors: set[str] = field(default_factory=set)
    summary: str = ""


def plan_intent(
    a: SynthObject,
    b: SynthObject,
    a_features: ObjectFeatures,
    b_features: ObjectFeatures,
    operation: Operation,
) -> CraftIntent:
    if operation == "subtract":
        expected_type = a.type
        summary = f"从「{a.name}」中剥离「{b.name}」代表的属性后，保留最自然的剩余对象。"
        return CraftIntent(
            operation=operation,
            expected_type=expected_type,
            preserve_terms=set(a_features.name_tokens),
            remove_terms=set(b_features.name_tokens),
            anchors=set(a_features.anchors) - set(b_features.anchors),
            summary=summary,
        )

    expected_type = _expected_add_type(a.type, b.type)
    anchors = set(a_features.anchors) | set(b_features.anchors)
    summary = f"融合「{a.name}」与「{b.name}」的核心物质、功能、身份或题材锚点。"
    return CraftIntent(
        operation=operation,
        expected_type=expected_type,
        preserve_terms=set(a_features.name_tokens) | set(b_features.name_tokens),
        anchors=anchors,
        summary=summary,
    )


def _expected_add_type(a_type: SynthType, b_type: SynthType) -> SynthType:
    pair = {a_type, b_type}
    if pair == {"element"}:
        return "element"
    if pair == {"equipment"}:
        return "equipment"
    if pair == {"creature"}:
        return "creature"
    if "concept" in pair:
        return "concept"
    if "equipment" in pair:
        return "equipment"
    return "concept"
