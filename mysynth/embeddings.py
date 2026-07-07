from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from .models import CandidateObject, CraftRequest, ObjectId, RouteEdge, SynthObject
from .normalize import normalize_text


EMBEDDING_STATUS_ACTIVE = "active"
EMBEDDING_STATUS_STALE = "stale"
EMBEDDING_STATUS_DELETED = "deleted"


@dataclass(frozen=True)
class EmbeddingText:
    owner_type: str
    owner_id: str
    text: str
    text_hash: str


@dataclass(frozen=True)
class EmbeddingRecord:
    id: int
    owner_type: str
    owner_id: str
    text_hash: str
    model: str
    dimensions: int
    status: str
    vector: list[float]


@dataclass(frozen=True)
class VectorSearchResult:
    owner_type: str
    owner_id: str
    score: float
    embedding_id: int


class EmbeddingProvider(Protocol):
    model: str
    dimensions: int

    def embed(self, text: str) -> list[float]:
        ...


class FakeEmbeddingProvider:
    """用于测试和 dry-run 的确定性本地 embedding provider。"""

    model = "fake-hash-v1"

    def __init__(self, dimensions: int = 16) -> None:
        self.dimensions = dimensions

    def embed(self, text: str) -> list[float]:
        values: list[float] = []
        counter = 0
        # 用 hash 分块生成伪向量，避免调用外部模型。
        while len(values) < self.dimensions:
            digest = hashlib.sha256(f"{counter}:{text}".encode("utf-8")).digest()
            for byte in digest:
                values.append((byte / 127.5) - 1.0)
                if len(values) == self.dimensions:
                    break
            counter += 1
        norm = math.sqrt(sum(value * value for value in values)) or 1.0
        return [round(value / norm, 8) for value in values]


class SQLiteEmbeddingStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def ensure_embedding(self, text: EmbeddingText, provider: EmbeddingProvider) -> EmbeddingRecord:
        existing = self.get_active(text.owner_type, text.owner_id, provider.model)
        if existing and existing.text_hash == text.text_hash:
            return existing
        if existing:
            # 旧向量保留审计，但移出 active 召回。
            self.mark_stale(text.owner_type, text.owner_id, provider.model)
        vector = provider.embed(text.text)
        now = _utc_now()
        with self.conn:
            cursor = self.conn.execute(
                """
                INSERT INTO embeddings(
                    owner_type, owner_id, text_hash, model, dimensions, status, vector_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    text.owner_type,
                    text.owner_id,
                    text.text_hash,
                    provider.model,
                    provider.dimensions,
                    EMBEDDING_STATUS_ACTIVE,
                    json.dumps(vector, separators=(",", ":")),
                    now,
                    now,
                ),
            )
        return EmbeddingRecord(
            id=int(cursor.lastrowid),
            owner_type=text.owner_type,
            owner_id=text.owner_id,
            text_hash=text.text_hash,
            model=provider.model,
            dimensions=provider.dimensions,
            status=EMBEDDING_STATUS_ACTIVE,
            vector=vector,
        )

    def get_active(self, owner_type: str, owner_id: str, model: str) -> EmbeddingRecord | None:
        row = self.conn.execute(
            """
            SELECT id, owner_type, owner_id, text_hash, model, dimensions, status, vector_json
            FROM embeddings
            WHERE owner_type = ? AND owner_id = ? AND model = ? AND status = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (owner_type, owner_id, model, EMBEDDING_STATUS_ACTIVE),
        ).fetchone()
        if row is None:
            return None
        return _record_from_row(row)

    def mark_stale(self, owner_type: str, owner_id: str, model: str) -> None:
        now = _utc_now()
        with self.conn:
            self.conn.execute(
                """
                UPDATE embeddings
                SET status = ?, updated_at = ?
                WHERE owner_type = ? AND owner_id = ? AND model = ? AND status = ?
                """,
                (EMBEDDING_STATUS_STALE, now, owner_type, owner_id, model, EMBEDDING_STATUS_ACTIVE),
            )

    def count_by_status(self, status: str) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS count FROM embeddings WHERE status = ?", (status,)).fetchone()
        return int(row["count"])


class SQLiteVectorIndex:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def search_vector(
        self,
        query_vector: list[float],
        *,
        owner_type: str,
        model: str,
        top_k: int,
    ) -> list[VectorSearchResult]:
        if top_k <= 0 or not query_vector:
            return []
        rows = self.conn.execute(
            """
            SELECT id, owner_type, owner_id, vector_json
            FROM embeddings
            WHERE owner_type = ? AND model = ? AND dimensions = ? AND status = ?
            """,
            (owner_type, model, len(query_vector), EMBEDDING_STATUS_ACTIVE),
        ).fetchall()
        results: list[VectorSearchResult] = []
        for row in rows:
            vector = json.loads(row["vector_json"])
            score = cosine_similarity(query_vector, vector)
            results.append(
                VectorSearchResult(
                    owner_type=row["owner_type"],
                    owner_id=row["owner_id"],
                    score=round(score, 6),
                    embedding_id=int(row["id"]),
                )
            )
        results.sort(key=lambda item: item.score, reverse=True)
        return results[:top_k]

    def search_text(
        self,
        text: EmbeddingText,
        provider: EmbeddingProvider,
        *,
        owner_type: str,
        top_k: int,
    ) -> list[VectorSearchResult]:
        return self.search_vector(provider.embed(text.text), owner_type=owner_type, model=provider.model, top_k=top_k)


def build_object_embedding_text(obj: SynthObject) -> EmbeddingText:
    # 对象向量文本使用稳定字段标签，便于比较模型变化。
    text = normalize_text(
        " ".join(
            [
                f"name:{obj.name}",
                f"type:{obj.type}",
                f"description:{obj.description or ''}",
                f"character:{obj.character_summary or ''}",
                f"background:{obj.background or ''}",
                f"specialty:{obj.specialty or ''}",
                f"weakness:{obj.weakness or ''}",
            ]
        )
    )
    return EmbeddingText(owner_type="object", owner_id=str(obj.id), text=text, text_hash=text_hash(text))


def build_recipe_embedding_text(edge: RouteEdge) -> EmbeddingText:
    # recipe 向量表达合成事件，而不只是结果节点。
    text = normalize_text(
        " ".join(
            [
                f"a:{edge.a_name or edge.a_id}",
                f"operation:{edge.operation}",
                f"b:{edge.b_name or edge.b_id}",
                f"result:{edge.result_name or edge.result_id}",
                f"result_type:{edge.result_type or ''}",
                f"result_description:{edge.result_description or ''}",
            ]
        )
    )
    owner_id = f"{edge.operation}:{edge.a_id}:{edge.b_id}:{edge.result_id}"
    return EmbeddingText(owner_type="recipe", owner_id=owner_id, text=text, text_hash=text_hash(text))


def build_candidate_embedding_text(candidate: CandidateObject) -> EmbeddingText:
    text = normalize_text(
        " ".join(
            [
                f"name:{candidate.name}",
                f"type:{candidate.type}",
                f"description:{candidate.description}",
                f"tags:{' '.join(candidate.core_tags)}",
                f"anchors:{' '.join(candidate.anchors)}",
                f"reason:{candidate.source_reason}",
            ]
        )
    )
    owner_id = text_hash(text)
    return EmbeddingText(owner_type="candidate", owner_id=owner_id, text=text, text_hash=text_hash(text))


def build_request_embedding_text(request: CraftRequest) -> EmbeddingText:
    # request 向量用于查找相似历史合成。
    text = normalize_text(
        " ".join(
            [
                f"a:{request.ingredient_a.name}",
                f"a_type:{request.ingredient_a.type}",
                f"a_description:{request.ingredient_a.description or ''}",
                f"operation:{request.operation}",
                f"b:{request.ingredient_b.name}",
                f"b_type:{request.ingredient_b.type}",
                f"b_description:{request.ingredient_b.description or ''}",
            ]
        )
    )
    owner_id = text_hash(text)
    return EmbeddingText(owner_type="request", owner_id=owner_id, text=text, text_hash=text_hash(text))


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    left_norm = math.sqrt(sum(value * value for value in left)) or 1.0
    right_norm = math.sqrt(sum(value * value for value in right)) or 1.0
    return sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)


def _record_from_row(row: sqlite3.Row) -> EmbeddingRecord:
    return EmbeddingRecord(
        id=int(row["id"]),
        owner_type=row["owner_type"],
        owner_id=row["owner_id"],
        text_hash=row["text_hash"],
        model=row["model"],
        dimensions=int(row["dimensions"]),
        status=row["status"],
        vector=json.loads(row["vector_json"]),
    )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
