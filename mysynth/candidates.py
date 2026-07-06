from __future__ import annotations

from .features import ObjectFeatures
from .intent import CraftIntent
from .models import CandidateObject, SynthObject


EMOJI_BY_TYPE = {
    "element": "✨",
    "item": "📦",
    "equipment": "🛠️",
    "creature": "👤",
    "concept": "💡",
}


def generate_candidates(
    a: SynthObject,
    b: SynthObject,
    a_features: ObjectFeatures,
    b_features: ObjectFeatures,
    intent: CraftIntent,
    max_candidates: int,
) -> list[CandidateObject]:
    candidates: list[CandidateObject] = []
    if intent.operation == "add":
        candidates.extend(_add_candidates(a, b, a_features, b_features, intent))
    else:
        candidates.extend(_subtract_candidates(a, b, a_features, b_features, intent))

    unique: dict[tuple[str, str], CandidateObject] = {}
    for candidate in candidates:
        key = (candidate.name, candidate.type)
        if key not in unique:
            unique[key] = candidate
    return list(unique.values())[:max_candidates]


def _add_candidates(
    a: SynthObject,
    b: SynthObject,
    a_features: ObjectFeatures,
    b_features: ObjectFeatures,
    intent: CraftIntent,
) -> list[CandidateObject]:
    tags = sorted(a_features.semantic_tags | b_features.semantic_tags)
    anchors = sorted(intent.anchors)
    common_description = (
        f"由「{a.name}」与「{b.name}」融合形成，结合了"
        f"{a.description or a.name}与{b.description or b.name}的核心特征。"
    )
    result = [
        CandidateObject(
            name=f"{a.name}{b.name}",
            type=intent.expected_type,
            description=common_description,
            emoji=EMOJI_BY_TYPE[intent.expected_type],
            core_tags=tags,
            anchors=anchors,
            source_reason="直接组合两个输入名称，作为最朴素的融合候选。",
        ),
        CandidateObject(
            name=f"{b.name}{a.name}",
            type=intent.expected_type,
            description=common_description,
            emoji=EMOJI_BY_TYPE[intent.expected_type],
            core_tags=tags,
            anchors=anchors,
            source_reason="反向组合两个输入名称，用于捕捉更自然的中文命名顺序。",
        ),
    ]

    if a.type == "element" and b.type == "element":
        result.append(
            CandidateObject(
                name=f"{a.name}{b.name}现象",
                type="element",
                description=f"「{a.name}」与「{b.name}」共同作用形成的自然现象。",
                emoji="🌌",
                core_tags=sorted(set(tags + ["自然"])),
                anchors=anchors,
                source_reason="两个 element 相加常形成自然物、物质或自然现象。",
            )
        )
    if "舞台" in tags or "角色" in tags:
        result.append(
            CandidateObject(
                name=f"{a.name}{b.name}企划",
                type="concept",
                description=f"围绕「{a.name}」与「{b.name}」形成的角色、舞台或演出企划概念。",
                emoji="🎭",
                core_tags=tags,
                anchors=anchors,
                source_reason="输入含舞台、角色或演出语义，优先考虑企划/作品型概念。",
            )
        )
    if anchors:
        result.append(
            CandidateObject(
                name=anchors[0],
                type="concept",
                description=f"由输入中的排他锚点「{anchors[0]}」主导收束形成的专名概念。",
                emoji="⭐",
                core_tags=tags,
                anchors=anchors,
                source_reason="输入含强专名锚点，候选保留专名而非泛化。",
            )
        )
    return result


def _subtract_candidates(
    a: SynthObject,
    b: SynthObject,
    a_features: ObjectFeatures,
    b_features: ObjectFeatures,
    intent: CraftIntent,
) -> list[CandidateObject]:
    tags = sorted(a_features.semantic_tags - b_features.semantic_tags)
    anchors = sorted(intent.anchors)
    return [
        CandidateObject(
            name=f"无{b.name}{a.name}",
            type=a.type,
            description=f"从「{a.name}」中移除「{b.name}」代表的属性、组成或用途后剩下的对象。",
            emoji=EMOJI_BY_TYPE[a.type],
            core_tags=tags,
            anchors=anchors,
            source_reason="减法候选保留 A 的对象类型，并显式移除 B 的语义。",
        ),
        CandidateObject(
            name=f"{a.name}残余",
            type=a.type,
            description=f"「{a.name}」剥离「{b.name}」后保留的残余结构或抽象本质。",
            emoji=EMOJI_BY_TYPE[a.type],
            core_tags=tags,
            anchors=anchors,
            source_reason="当减法无法稳定命名时，保留 A 的残余本质作为候选。",
        ),
    ]
