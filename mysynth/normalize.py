from __future__ import annotations

import re
import unicodedata

from .models import ObjectId, Operation, SynthObject


PUNCT_TRANSLATION = str.maketrans(
    {
        "（": "(",
        "）": ")",
        "【": "[",
        "】": "]",
        "，": ",",
        "。": ".",
        "：": ":",
        "；": ";",
        "！": "!",
        "？": "?",
        "　": " ",
    }
)


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKC", value)
    normalized = normalized.translate(PUNCT_TRANSLATION)
    normalized = re.sub(r"\s+", " ", normalized.strip())
    return normalized


def normalize_name(value: str | None) -> str:
    return normalize_text(value).casefold()


def recipe_key(a_id: ObjectId, b_id: ObjectId, operation: Operation) -> str:
    if operation == "add":
        left, right = sorted((a_id, b_id), key=lambda item: (str(type(item)), str(item)))
    else:
        left, right = a_id, b_id
    return f"{operation}:{left}:{right}"


def object_search_text(obj: SynthObject) -> str:
    parts = [
        obj.name,
        obj.type,
        obj.description or "",
        obj.character_summary or "",
        obj.background or "",
        obj.specialty or "",
        obj.weakness or "",
    ]
    return normalize_text(" ".join(parts))
