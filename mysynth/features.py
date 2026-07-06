from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import SynthObject
from .normalize import normalize_text, object_search_text


DOMAIN_KEYWORDS = {
    "自然": ["水", "火", "风", "土", "冰", "雷", "光", "暗", "云", "雨", "海", "山", "星", "太阳", "月"],
    "能源": ["能", "电", "核", "聚变", "裂变", "燃料", "反应堆", "发动机"],
    "武器": ["武器", "炮", "导弹", "舰", "战", "攻击", "防御", "打击", "盾"],
    "舞台": ["舞台", "歌剧", "音乐剧", "演唱", "偶像", "声优", "乐队", "主唱", "星光"],
    "角色": ["少女", "主角", "角色", "人", "士兵", "飞行员", "指挥", "声优"],
    "组织": ["系统", "网络", "体系", "组织", "平台", "舰队", "司令部"],
    "科技": ["量子", "电子", "通信", "网络", "卫星", "轨道", "雷达", "隐身"],
}

ANCHOR_PATTERNS = [
    r"BanG Dream!?",
    r"MyGO!!!!!",
    r"Roselia",
    r"Poppin'?Party",
    r"RAISE A SUILEN",
    r"Revue Starlight",
    r"Starlight",
]


@dataclass(frozen=True)
class ObjectFeatures:
    name_tokens: set[str] = field(default_factory=set)
    semantic_tags: set[str] = field(default_factory=set)
    anchors: set[str] = field(default_factory=set)
    raw_text: str = ""


TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9!']+|[\u4e00-\u9fff]{1,4}|\d+")


def tokenize(text: str) -> set[str]:
    normalized = normalize_text(text)
    tokens = {match.group(0) for match in TOKEN_RE.finditer(normalized)}
    for char in normalized:
        if "\u4e00" <= char <= "\u9fff":
            tokens.add(char)
    return {token for token in tokens if token.strip()}


def extract_features(obj: SynthObject) -> ObjectFeatures:
    text = object_search_text(obj)
    tokens = tokenize(text)
    tags: set[str] = set()
    anchors: set[str] = set()

    for tag, words in DOMAIN_KEYWORDS.items():
        if any(word in text for word in words):
            tags.add(tag)

    for pattern in ANCHOR_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            anchors.add(re.search(pattern, text, flags=re.IGNORECASE).group(0))  # type: ignore[union-attr]

    if obj.name:
        anchors.update(
            token for token in tokenize(obj.name) if re.search(r"[A-Za-z!]{3,}", token)
        )

    return ObjectFeatures(
        name_tokens=tokenize(obj.name) | tokens,
        semantic_tags=tags,
        anchors=anchors,
        raw_text=text,
    )
