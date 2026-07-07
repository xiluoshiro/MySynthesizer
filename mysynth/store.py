from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable, Protocol

from .features import tokenize
from .models import CandidateObject, CraftRequest, CraftResult, ObjectId, Operation, RouteEdge, SynthObject
from .normalize import normalize_name, normalize_text, object_search_text, recipe_key


RecipeKey = str


class ObjectStore(Protocol):
    def get_object(self, object_id: ObjectId) -> SynthObject | None:
        ...

    def get_by_name(self, name: str) -> list[SynthObject]:
        ...

    def list_objects(self) -> Iterable[SynthObject]:
        ...

    def get_neighbors(self, object_id: ObjectId, limit: int | None = None) -> list[SynthObject]:
        ...

    def find_edges_by_inputs(self, a_id: ObjectId, b_id: ObjectId, operation: Operation) -> list[RouteEdge]:
        ...

    def search_by_type_and_tokens(self, synth_type: str, tokens: Iterable[str], limit: int) -> list[SynthObject]:
        ...

    def find_recipe(self, key: RecipeKey) -> SynthObject | None:
        ...

    def search_candidates(self, candidate: CandidateObject, limit: int) -> list[SynthObject]:
        ...

    def save_craft_result(self, request: CraftRequest, result: CraftResult) -> None:
        ...


class SQLiteObjectStore:
    def __init__(
        self,
        db_path: str | Path = "data/engine/mysynth.db",
        source_path: str | Path = "outputs/data/current/mysynthesizer_mine_full_routes_latest.json",
    ) -> None:
        self.db_path = Path(db_path)
        self.source_path = Path(source_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.objects_by_id: dict[ObjectId, SynthObject] = {}
        self.name_index: dict[str, set[ObjectId]] = defaultdict(set)
        self.token_index: dict[str, set[ObjectId]] = defaultdict(set)
        self.type_index: dict[str, set[ObjectId]] = defaultdict(set)
        self.recipe_index: dict[RecipeKey, ObjectId] = {}
        self.neighbors_by_object: dict[ObjectId, set[ObjectId]] = defaultdict(set)

    def initialize(self, force_import: bool = False) -> None:
        self._create_schema()
        if force_import or self._object_count() == 0:
            if not self.source_path.exists():
                raise FileNotFoundError(f"Source graph not found: {self.source_path}")
            self.import_graph(self.source_path, clear=force_import)
        self.rebuild_indexes()

    def close(self) -> None:
        self.conn.close()

    def _create_schema(self) -> None:
        # SQLite 统一承载元数据、路线、事件和 embedding sidecar。
        self.conn.executescript(
            """
            PRAGMA journal_mode=WAL;

            CREATE TABLE IF NOT EXISTS objects (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                normalized_name TEXT NOT NULL,
                type TEXT NOT NULL,
                description TEXT,
                source TEXT,
                is_banned INTEGER NOT NULL DEFAULT 0,
                created_at TEXT,
                discovered_at TEXT
            );

            CREATE TABLE IF NOT EXISTS object_payloads (
                object_id INTEGER PRIMARY KEY REFERENCES objects(id) ON DELETE CASCADE,
                payload_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS route_edges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                a_id INTEGER NOT NULL,
                b_id INTEGER NOT NULL,
                operation TEXT NOT NULL,
                result_id INTEGER NOT NULL,
                source TEXT,
                UNIQUE(a_id, b_id, operation, result_id)
            );

            CREATE TABLE IF NOT EXISTS recipe_cache (
                recipe_key TEXT PRIMARY KEY,
                a_id INTEGER NOT NULL,
                b_id INTEGER NOT NULL,
                operation TEXT NOT NULL,
                result_id INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS craft_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_json TEXT NOT NULL,
                result_json TEXT NOT NULL,
                score_json TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS failures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_json TEXT,
                reason TEXT NOT NULL,
                context_json TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS embeddings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_type TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                text_hash TEXT NOT NULL,
                model TEXT NOT NULL,
                dimensions INTEGER NOT NULL,
                status TEXT NOT NULL,
                vector_backend_id TEXT,
                vector_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS embedding_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_type TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_objects_normalized_name ON objects(normalized_name);
            CREATE INDEX IF NOT EXISTS idx_objects_type ON objects(type);
            CREATE INDEX IF NOT EXISTS idx_route_edges_inputs ON route_edges(a_id, b_id, operation);
            CREATE INDEX IF NOT EXISTS idx_route_edges_result ON route_edges(result_id);
            CREATE INDEX IF NOT EXISTS idx_embeddings_owner ON embeddings(owner_type, owner_id, model, status);
            CREATE INDEX IF NOT EXISTS idx_embeddings_hash ON embeddings(text_hash, model, status);
            """
        )
        self.conn.commit()

    def _object_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS count FROM objects").fetchone()
        return int(row["count"])

    def import_graph(self, path: str | Path, clear: bool = False) -> None:
        graph = json.loads(Path(path).read_text(encoding="utf-8"))
        objects = graph.get("objects", [])
        edges = graph.get("route_edges", [])
        with self.conn:
            # 导入用事务保证对象、边和 recipe cache 一致。
            if clear:
                self.conn.execute("DELETE FROM failures")
                self.conn.execute("DELETE FROM craft_events")
                self.conn.execute("DELETE FROM recipe_cache")
                self.conn.execute("DELETE FROM route_edges")
                self.conn.execute("DELETE FROM object_payloads")
                self.conn.execute("DELETE FROM objects")
            for payload in objects:
                obj = SynthObject.model_validate(payload)
                if obj.id is None:
                    continue
                self._upsert_object(obj, payload)
            for payload in edges:
                edge = RouteEdge.model_validate(payload)
                self._insert_route_edge(edge, source="import")

    def rebuild_indexes(self) -> None:
        # 从 SQLite 重建在线索引，后续 status 过滤也应放在这里。
        self.objects_by_id.clear()
        self.name_index.clear()
        self.token_index.clear()
        self.type_index.clear()
        self.recipe_index.clear()
        self.neighbors_by_object.clear()

        rows = self.conn.execute(
            """
            SELECT object_payloads.payload_json
            FROM objects
            JOIN object_payloads ON object_payloads.object_id = objects.id
            """
        ).fetchall()
        for row in rows:
            obj = SynthObject.model_validate_json(row["payload_json"])
            if obj.id is None:
                continue
            self.objects_by_id[obj.id] = obj
            self.name_index[normalize_name(obj.name)].add(obj.id)
            self.type_index[obj.type].add(obj.id)
            for token in tokenize(object_search_text(obj)):
                self.token_index[token.casefold()].add(obj.id)

        recipe_rows = self.conn.execute("SELECT recipe_key, result_id FROM recipe_cache").fetchall()
        for row in recipe_rows:
            self.recipe_index[row["recipe_key"]] = row["result_id"]

        edge_rows = self.conn.execute("SELECT a_id, b_id, result_id FROM route_edges").fetchall()
        for row in edge_rows:
            # 邻接只用于诊断和受限路线证据，不做宽泛候选扩散。
            a_id = int(row["a_id"])
            b_id = int(row["b_id"])
            result_id = int(row["result_id"])
            self.neighbors_by_object[a_id].update({b_id, result_id})
            self.neighbors_by_object[b_id].update({a_id, result_id})
            self.neighbors_by_object[result_id].update({a_id, b_id})

    def get_object(self, object_id: ObjectId) -> SynthObject | None:
        return self.objects_by_id.get(object_id)

    def get_by_name(self, name: str) -> list[SynthObject]:
        ids = self.name_index.get(normalize_name(name), set())
        return [self.objects_by_id[item] for item in ids if item in self.objects_by_id]

    def list_objects(self) -> Iterable[SynthObject]:
        return self.objects_by_id.values()

    def get_neighbors(self, object_id: ObjectId, limit: int | None = None) -> list[SynthObject]:
        neighbor_ids = sorted(self.neighbors_by_object.get(object_id, set()), key=lambda item: str(item))
        if limit is not None:
            neighbor_ids = neighbor_ids[:limit]
        return [self.objects_by_id[item] for item in neighbor_ids if item in self.objects_by_id]

    def find_edges_by_inputs(self, a_id: ObjectId, b_id: ObjectId, operation: Operation) -> list[RouteEdge]:
        # add 查询按交换律处理，subtract 保留 A/B 方向。
        pairs = [(a_id, b_id)]
        if operation == "add" and a_id != b_id:
            pairs.append((b_id, a_id))
        rows: list[sqlite3.Row] = []
        for left, right in pairs:
            rows.extend(
                self.conn.execute(
                    """
                    SELECT result_id, operation, a_id, b_id
                    FROM route_edges
                    WHERE a_id = ? AND b_id = ? AND operation = ?
                    ORDER BY id
                    """,
                    (left, right, operation),
                ).fetchall()
            )
        return [edge for row in rows if (edge := self._route_edge_from_row(row)) is not None]

    def search_by_type_and_tokens(self, synth_type: str, tokens: Iterable[str], limit: int) -> list[SynthObject]:
        ids: list[ObjectId] = []
        seen: set[ObjectId] = set()
        allowed = self.type_index.get(synth_type, set())
        normalized_tokens: list[str] = []
        # 同时支持已分词 token 和候选里的短语。
        for token in tokens:
            normalized_tokens.extend(item.casefold() for item in tokenize(token))
        for token in normalized_tokens:
            for object_id in self.token_index.get(token, set()):
                if object_id in allowed and object_id not in seen and object_id in self.objects_by_id:
                    seen.add(object_id)
                    ids.append(object_id)
                    if len(ids) >= limit:
                        return [self.objects_by_id[item] for item in ids]
        return [self.objects_by_id[item] for item in ids]

    def find_recipe(self, key: RecipeKey) -> SynthObject | None:
        result_id = self.recipe_index.get(key)
        if result_id is None:
            return None
        return self.objects_by_id.get(result_id)

    def search_candidates(self, candidate: CandidateObject, limit: int) -> list[SynthObject]:
        ids: list[ObjectId] = []
        seen: set[ObjectId] = set()

        def add_many(values: Iterable[ObjectId]) -> None:
            for value in values:
                if value not in seen and value in self.objects_by_id:
                    seen.add(value)
                    ids.append(value)

        # 先召回精确/文本匹配，再用同 type 对象低置信兜底。
        add_many(self.name_index.get(normalize_name(candidate.name), set()))
        for token in tokenize(" ".join([candidate.name, candidate.description, *candidate.core_tags, *candidate.anchors])):
            add_many(self.token_index.get(token.casefold(), set()))
            if len(ids) >= limit * 3:
                break
        add_many(self.type_index.get(candidate.type, set()))
        return [self.objects_by_id[item] for item in ids[:limit]]

    def save_craft_result(self, request: CraftRequest, result: CraftResult) -> None:
        now = _utc_now()
        request_json = request.model_dump_json()
        result_json = result.model_dump_json()
        score_json = result.score_breakdown.model_dump_json() if result.score_breakdown else None

        with self.conn:
            # 失败也作为调试证据持久化，避免静默丢失。
            if not result.success:
                self.conn.execute(
                    """
                    INSERT INTO failures(request_json, reason, context_json, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (request_json, result.failure_reason or "unknown", result_json, now),
                )
                return

            if result.result is None:
                return
            saved_result = result.result
            if saved_result.id is None:
                saved_result.id = self.next_local_id()
                result.result = saved_result
                result_json = result.model_dump_json()

            if result.decision == "created_new":
                self._upsert_object(saved_result, saved_result.model_dump(mode="json"))

            a_id = request.ingredient_a.id
            b_id = request.ingredient_b.id
            if a_id is not None and b_id is not None and saved_result.id is not None:
                # 成功合成同时写入图证据和精确 recipe cache。
                self._insert_route_edge(
                    RouteEdge(
                        result_id=saved_result.id,
                        result_name=saved_result.name,
                        result_type=saved_result.type,
                        result_description=saved_result.description,
                        operation=request.operation,
                        a_id=a_id,
                        a_name=request.ingredient_a.name,
                        a_type=request.ingredient_a.type,
                        a_description=request.ingredient_a.description,
                        b_id=b_id,
                        b_name=request.ingredient_b.name,
                        b_type=request.ingredient_b.type,
                        b_description=request.ingredient_b.description,
                    ),
                    source="local_engine",
                )

            self.conn.execute(
                """
                INSERT INTO craft_events(request_json, result_json, score_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (request_json, result_json, score_json, now),
            )
        self.rebuild_indexes()

    def next_local_id(self) -> int:
        row = self.conn.execute("SELECT MIN(id) AS min_id FROM objects").fetchone()
        min_id = row["min_id"]
        if min_id is None or int(min_id) >= 0:
            return -1
        return int(min_id) - 1

    def list_route_edges(self, limit: int | None = None) -> list[RouteEdge]:
        sql = """
            SELECT result_id, operation, a_id, b_id
            FROM route_edges
            ORDER BY id
        """
        if limit is not None:
            sql += " LIMIT ?"
            rows = self.conn.execute(sql, (limit,)).fetchall()
        else:
            rows = self.conn.execute(sql).fetchall()
        return [edge for row in rows if (edge := self._route_edge_from_row(row)) is not None]

    def _route_edge_from_row(self, row: sqlite3.Row) -> RouteEdge | None:
        result_obj = self.get_object(int(row["result_id"]))
        a_obj = self.get_object(int(row["a_id"]))
        b_obj = self.get_object(int(row["b_id"]))
        if result_obj is None or a_obj is None or b_obj is None:
            return None
        return RouteEdge(
            result_id=result_obj.id or int(row["result_id"]),
            result_name=result_obj.name,
            result_type=result_obj.type,
            result_description=result_obj.description,
            operation=row["operation"],
            a_id=a_obj.id or int(row["a_id"]),
            a_name=a_obj.name,
            a_type=a_obj.type,
            a_description=a_obj.description,
            b_id=b_obj.id or int(row["b_id"]),
            b_name=b_obj.name,
            b_type=b_obj.type,
            b_description=b_obj.description,
        )

    def _upsert_object(self, obj: SynthObject, payload: dict) -> None:
        if obj.id is None:
            raise ValueError("Cannot save object without id")
        self.conn.execute(
            """
            INSERT INTO objects(id, name, normalized_name, type, description, source, is_banned, created_at, discovered_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                normalized_name=excluded.normalized_name,
                type=excluded.type,
                description=excluded.description,
                source=excluded.source,
                is_banned=excluded.is_banned,
                created_at=excluded.created_at,
                discovered_at=excluded.discovered_at
            """,
            (
                obj.id,
                obj.name,
                normalize_name(obj.name),
                obj.type,
                obj.description,
                obj.source,
                1 if obj.is_banned else 0,
                obj.created_at,
                obj.discovered_at,
            ),
        )
        self.conn.execute(
            """
            INSERT INTO object_payloads(object_id, payload_json)
            VALUES (?, ?)
            ON CONFLICT(object_id) DO UPDATE SET payload_json=excluded.payload_json
            """,
            (obj.id, json.dumps(payload, ensure_ascii=False, separators=(",", ":"))),
        )

    def _insert_route_edge(self, edge: RouteEdge, source: str) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO route_edges(a_id, b_id, operation, result_id, source)
            VALUES (?, ?, ?, ?, ?)
            """,
            (edge.a_id, edge.b_id, edge.operation, edge.result_id, source),
        )
        key = recipe_key(edge.a_id, edge.b_id, edge.operation)
        self.conn.execute(
            """
            INSERT OR REPLACE INTO recipe_cache(recipe_key, a_id, b_id, operation, result_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (key, edge.a_id, edge.b_id, edge.operation, edge.result_id, _utc_now()),
        )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
