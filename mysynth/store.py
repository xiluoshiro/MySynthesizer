from __future__ import annotations

import base64
import ctypes
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable, Protocol

from .features import tokenize
from .models import CandidateObject, CraftRequest, CraftResult, ObjectId, Operation, RouteEdge, SynthObject
from .normalize import normalize_name, normalize_text, object_search_text, recipe_key


RecipeKey = str
INITIAL_OBJECT_IDS: tuple[ObjectId, ...] = (1, 2, 3, 4)
LLM_SETTING_KEYS = {"base_url", "api_key", "model", "timeout", "temperature", "top_p"}


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

    def search_text(self, query: str, limit: int) -> list[SynthObject]:
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
        self.all_objects_by_id: dict[ObjectId, SynthObject] = {}
        self.objects_by_id: dict[ObjectId, SynthObject] = {}
        self.name_index: dict[str, set[ObjectId]] = defaultdict(set)
        self.token_index: dict[str, set[ObjectId]] = defaultdict(set)
        self.type_index: dict[str, set[ObjectId]] = defaultdict(set)
        self.recipe_index: dict[RecipeKey, ObjectId] = {}
        self.neighbors_by_object: dict[ObjectId, set[ObjectId]] = defaultdict(set)
        self.fts_enabled = False

    def initialize(self, force_import: bool = False, full_import: bool = False) -> None:
        self._create_schema()
        if force_import or self._object_count() == 0:
            if not self.source_path.exists():
                raise FileNotFoundError(f"Source graph not found: {self.source_path}")
            self.import_graph(self.source_path, clear=force_import)
            if not full_import:
                self.reset_to_initial_state()
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
                status TEXT NOT NULL DEFAULT 'active',
                canonical_id INTEGER,
                quality_flags TEXT NOT NULL DEFAULT '[]',
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
                status TEXT NOT NULL DEFAULT 'active',
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

            CREATE TABLE IF NOT EXISTS object_aliases (
                alias TEXT PRIMARY KEY,
                normalized_alias TEXT NOT NULL,
                object_id INTEGER NOT NULL,
                source TEXT,
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

            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_objects_normalized_name ON objects(normalized_name);
            CREATE INDEX IF NOT EXISTS idx_objects_type ON objects(type);
            CREATE INDEX IF NOT EXISTS idx_object_aliases_normalized ON object_aliases(normalized_alias);
            CREATE INDEX IF NOT EXISTS idx_route_edges_inputs ON route_edges(a_id, b_id, operation);
            CREATE INDEX IF NOT EXISTS idx_route_edges_result ON route_edges(result_id);
            CREATE INDEX IF NOT EXISTS idx_embeddings_owner ON embeddings(owner_type, owner_id, model, status);
            CREATE INDEX IF NOT EXISTS idx_embeddings_hash ON embeddings(text_hash, model, status);
            """
        )
        self._ensure_schema_columns()
        self._ensure_fts_table()
        self.conn.commit()

    def _ensure_schema_columns(self) -> None:
        self._ensure_column("objects", "status", "ALTER TABLE objects ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
        self._ensure_column("objects", "canonical_id", "ALTER TABLE objects ADD COLUMN canonical_id INTEGER")
        self._ensure_column("objects", "quality_flags", "ALTER TABLE objects ADD COLUMN quality_flags TEXT NOT NULL DEFAULT '[]'")
        self._ensure_column("route_edges", "status", "ALTER TABLE route_edges ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")

    def _ensure_column(self, table: str, column: str, sql: str) -> None:
        columns = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            self.conn.execute(sql)

    def _ensure_fts_table(self) -> None:
        try:
            self.conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS object_fts
                USING fts5(object_id UNINDEXED, name, type, search_text)
                """
            )
        except sqlite3.OperationalError:
            self.fts_enabled = False
            return
        self.fts_enabled = True

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
                if self.fts_enabled:
                    self.conn.execute("DELETE FROM object_fts")
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
        self.all_objects_by_id.clear()
        self.objects_by_id.clear()
        self.name_index.clear()
        self.token_index.clear()
        self.type_index.clear()
        self.recipe_index.clear()
        self.neighbors_by_object.clear()

        rows = self.conn.execute(
            """
            SELECT object_payloads.payload_json, objects.status, objects.canonical_id, objects.quality_flags
            FROM objects
            JOIN object_payloads ON object_payloads.object_id = objects.id
            """
        ).fetchall()
        for row in rows:
            obj = self._object_from_row(row)
            if obj.id is None:
                continue
            self.all_objects_by_id[obj.id] = obj
            if not self._is_online_object(obj):
                continue
            self.objects_by_id[obj.id] = obj
            self.name_index[normalize_name(obj.name)].add(obj.id)
            self.type_index[obj.type].add(obj.id)
            for token in tokenize(object_search_text(obj)):
                self.token_index[token.casefold()].add(obj.id)

        recipe_rows = self.conn.execute("SELECT recipe_key, result_id FROM recipe_cache").fetchall()
        for row in recipe_rows:
            if self.get_object(int(row["result_id"])) is not None:
                self.recipe_index[row["recipe_key"]] = row["result_id"]

        edge_rows = self.conn.execute("SELECT result_id, operation, a_id, b_id FROM route_edges WHERE status = 'active'").fetchall()
        for row in edge_rows:
            # 邻接只用于诊断和受限路线证据，不做宽泛候选扩散。
            if self._route_edge_from_row(row) is None:
                continue
            a_id = int(row["a_id"])
            b_id = int(row["b_id"])
            result_id = int(row["result_id"])
            self.neighbors_by_object[a_id].update({b_id, result_id})
            self.neighbors_by_object[b_id].update({a_id, result_id})
            self.neighbors_by_object[result_id].update({a_id, b_id})

    def get_object(self, object_id: ObjectId) -> SynthObject | None:
        obj = self.all_objects_by_id.get(object_id)
        if obj is None:
            return None
        if obj.status == "merged" and obj.canonical_id is not None:
            return self.get_object(obj.canonical_id)
        if self._is_online_object(obj):
            return obj
        return None

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
                    WHERE a_id = ? AND b_id = ? AND operation = ? AND status = 'active'
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

    def search_text(self, query: str, limit: int) -> list[SynthObject]:
        if limit <= 0:
            return []
        if self.fts_enabled:
            fts_query = _fts_query(query)
            if fts_query:
                rows = self.conn.execute(
                    """
                    SELECT object_id
                    FROM object_fts
                    WHERE object_fts MATCH ?
                    LIMIT ?
                    """,
                    (fts_query, limit),
                ).fetchall()
                results = [self.get_object(int(row["object_id"])) for row in rows]
                objects = [obj for obj in results if obj is not None]
                if objects:
                    return objects

        ids: list[ObjectId] = []
        seen: set[ObjectId] = set()
        for token in tokenize(query):
            for object_id in self.token_index.get(token.casefold(), set()):
                if object_id not in seen and object_id in self.objects_by_id:
                    seen.add(object_id)
                    ids.append(object_id)
                    if len(ids) >= limit:
                        return [self.objects_by_id[item] for item in ids]
        return [self.objects_by_id[item] for item in ids]

    def find_recipe(self, key: RecipeKey) -> SynthObject | None:
        result_id = self.recipe_index.get(key)
        if result_id is None:
            return None
        return self.get_object(result_id)

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
        add_many(obj.id for obj in self.search_text(" ".join([candidate.name, candidate.description, *candidate.core_tags, *candidate.anchors]), limit * 2) if obj.id is not None)
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

            if result.decision in {"created_new", "created_pending"}:
                self._upsert_object(saved_result, saved_result.model_dump(mode="json"))
            if result.decision == "merged_existing" and result.candidate is not None:
                self._insert_object_alias(result.candidate.name, saved_result.id, source="local_engine")

            a_id = request.ingredient_a.id
            b_id = request.ingredient_b.id
            if a_id is not None and b_id is not None and saved_result.id is not None:
                route_status = "disabled" if result.decision == "created_pending" or saved_result.status != "active" else "active"
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
                        status=route_status,
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

    def get_llm_settings(self, *, include_secret: bool = False) -> dict[str, object]:
        rows = self.conn.execute(
            "SELECT key, value_json FROM app_settings WHERE key LIKE 'llm.%'"
        ).fetchall()
        settings: dict[str, object] = {}
        for row in rows:
            key = str(row["key"]).removeprefix("llm.")
            if key in LLM_SETTING_KEYS:
                value = json.loads(row["value_json"])
                if key == "api_key" and isinstance(value, str):
                    value = _unprotect_secret(value)
                settings[key] = value
        if not include_secret and settings.get("api_key"):
            settings["api_key"] = _mask_secret(str(settings["api_key"]))
        return settings

    def save_llm_settings(self, payload: dict[str, object]) -> dict[str, object]:
        now = _utc_now()
        next_values: dict[str, object] = {}
        for key in ["base_url", "model"]:
            value = _optional_text(payload.get(key))
            if value is not None:
                next_values[key] = value
        for key, default, min_value, max_value in [
            ("timeout", 30.0, 1.0, 300.0),
            ("temperature", 0.7, 0.0, 2.0),
            ("top_p", 1.0, 0.0, 1.0),
        ]:
            if key in payload:
                next_values[key] = _bounded_float(payload.get(key), default, min_value, max_value)
        if bool(payload.get("clear_api_key", False)):
            next_values["api_key"] = ""
        else:
            api_key = _optional_text(payload.get("api_key"))
            if api_key is not None:
                next_values["api_key"] = api_key

        with self.conn:
            for key, value in next_values.items():
                if key == "api_key" and value == "":
                    self.conn.execute("DELETE FROM app_settings WHERE key = ?", ("llm.api_key",))
                    continue
                stored_value = _protect_secret(str(value)) if key == "api_key" else value
                self.conn.execute(
                    """
                    INSERT INTO app_settings(key, value_json, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value_json=excluded.value_json,
                        updated_at=excluded.updated_at
                    """,
                    (f"llm.{key}", json.dumps(stored_value, ensure_ascii=False, separators=(",", ":")), now),
                )
        return self.get_llm_settings()

    def promote_pending_object(self, object_id: ObjectId) -> SynthObject:
        obj = self.all_objects_by_id.get(object_id)
        if obj is None:
            raise ValueError(f"object not found: {object_id}")
        if obj.status != "pending":
            raise ValueError(f"object is not pending: {object_id}")

        promoted = obj.model_copy(update={"status": "active", "quality_flags": []})
        now = _utc_now()
        with self.conn:
            self._upsert_object(promoted, promoted.model_dump(mode="json"))
            rows = self.conn.execute(
                """
                SELECT a_id, b_id, operation
                FROM route_edges
                WHERE result_id = ? AND status = 'disabled'
                """,
                (object_id,),
            ).fetchall()
            self.conn.execute(
                """
                UPDATE route_edges
                SET status = 'active'
                WHERE result_id = ? AND status = 'disabled'
                """,
                (object_id,),
            )
            for row in rows:
                key = recipe_key(int(row["a_id"]), int(row["b_id"]), row["operation"])
                self.conn.execute(
                    """
                    INSERT OR REPLACE INTO recipe_cache(recipe_key, a_id, b_id, operation, result_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (key, row["a_id"], row["b_id"], row["operation"], object_id, now),
                )
        self.rebuild_indexes()
        result = self.get_object(object_id)
        if result is None:
            raise RuntimeError(f"promoted object is not visible online: {object_id}")
        return result

    def reject_pending_object(self, object_id: ObjectId, reason: str | None = None) -> SynthObject:
        obj = self.all_objects_by_id.get(object_id)
        if obj is None:
            raise ValueError(f"object not found: {object_id}")
        if obj.status != "pending":
            raise ValueError(f"object is not pending: {object_id}")

        flags = list(dict.fromkeys([*obj.quality_flags, "rejected", *(["review_reason:" + reason] if reason else [])]))
        rejected = obj.model_copy(update={"status": "rejected", "quality_flags": flags})
        with self.conn:
            self._upsert_object(rejected, rejected.model_dump(mode="json"))
            self.conn.execute("UPDATE route_edges SET status = 'disabled' WHERE result_id = ?", (object_id,))
            self.conn.execute("DELETE FROM recipe_cache WHERE result_id = ?", (object_id,))
        self.rebuild_indexes()
        return self.all_objects_by_id[object_id]

    def merge_pending_object(self, object_id: ObjectId, canonical_id: ObjectId) -> SynthObject:
        obj = self.all_objects_by_id.get(object_id)
        canonical = self.get_object(canonical_id)
        if obj is None:
            raise ValueError(f"object not found: {object_id}")
        if obj.status != "pending":
            raise ValueError(f"object is not pending: {object_id}")
        if canonical is None:
            raise ValueError(f"canonical object is not active: {canonical_id}")

        merged = obj.model_copy(update={"status": "merged", "canonical_id": canonical.id, "quality_flags": ["merged"]})
        now = _utc_now()
        with self.conn:
            self._upsert_object(merged, merged.model_dump(mode="json"))
            self._insert_object_alias(obj.name, canonical.id, source="review")
            rows = self.conn.execute(
                """
                SELECT a_id, b_id, operation
                FROM route_edges
                WHERE result_id = ?
                """,
                (object_id,),
            ).fetchall()
            self.conn.execute(
                """
                UPDATE route_edges
                SET result_id = ?, status = 'active'
                WHERE result_id = ?
                """,
                (canonical.id, object_id),
            )
            for row in rows:
                key = recipe_key(int(row["a_id"]), int(row["b_id"]), row["operation"])
                self.conn.execute(
                    """
                    INSERT OR REPLACE INTO recipe_cache(recipe_key, a_id, b_id, operation, result_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (key, row["a_id"], row["b_id"], row["operation"], canonical.id, now),
                )
        self.rebuild_indexes()
        return self.all_objects_by_id[object_id]

    def reset_to_initial_state(self) -> dict[str, object]:
        existing = {
            int(row["id"])
            for row in self.conn.execute(
                f"SELECT id FROM objects WHERE id IN ({','.join('?' for _ in INITIAL_OBJECT_IDS)})",
                INITIAL_OBJECT_IDS,
            ).fetchall()
        }
        missing = [object_id for object_id in INITIAL_OBJECT_IDS if object_id not in existing]
        if missing:
            raise ValueError(f"initial objects missing: {missing}")
        before_objects = self.conn.execute("SELECT COUNT(*) AS count FROM objects").fetchone()["count"]
        before_edges = self.conn.execute("SELECT COUNT(*) AS count FROM route_edges").fetchone()["count"]
        before_recipes = self.conn.execute("SELECT COUNT(*) AS count FROM recipe_cache").fetchone()["count"]
        placeholders = ",".join("?" for _ in INITIAL_OBJECT_IDS)
        with self.conn:
            # 还原会清空除初始元素外的事实和派生索引。
            self.conn.execute("DELETE FROM failures")
            self.conn.execute("DELETE FROM craft_events")
            self.conn.execute("DELETE FROM recipe_cache")
            self.conn.execute("DELETE FROM route_edges")
            self.conn.execute("DELETE FROM object_aliases")
            self.conn.execute("DELETE FROM embeddings")
            self.conn.execute("DELETE FROM embedding_jobs")
            self.conn.execute(f"DELETE FROM object_payloads WHERE object_id NOT IN ({placeholders})", INITIAL_OBJECT_IDS)
            self.conn.execute(f"DELETE FROM objects WHERE id NOT IN ({placeholders})", INITIAL_OBJECT_IDS)
            self.conn.execute(
                f"""
                UPDATE objects
                SET status = 'active', canonical_id = NULL, quality_flags = '[]'
                WHERE id IN ({placeholders})
                """,
                INITIAL_OBJECT_IDS,
            )
            if self.fts_enabled:
                self.conn.execute("DELETE FROM object_fts")
                rows = self.conn.execute(
                    f"""
                    SELECT object_payloads.payload_json, objects.status, objects.canonical_id, objects.quality_flags
                    FROM objects
                    JOIN object_payloads ON object_payloads.object_id = objects.id
                    WHERE objects.id IN ({placeholders})
                    """,
                    INITIAL_OBJECT_IDS,
                ).fetchall()
                for row in rows:
                    self._upsert_object_fts(self._object_from_row(row))
        self.rebuild_indexes()
        kept_objects = [
            {"id": obj.id, "name": obj.name, "emoji": obj.emoji, "type": obj.type}
            for obj in sorted(self.objects_by_id.values(), key=lambda item: item.id or 0)
        ]
        return {
            "ok": True,
            "kept_objects": kept_objects,
            "deleted_objects": max(0, int(before_objects) - len(INITIAL_OBJECT_IDS)),
            "deleted_edges": int(before_edges),
            "deleted_recipes": int(before_recipes),
        }

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
            WHERE status = 'active'
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
        self.conn.execute(
            """
            UPDATE objects
            SET status = ?, canonical_id = ?, quality_flags = ?
            WHERE id = ?
            """,
            (obj.status, obj.canonical_id, json.dumps(obj.quality_flags, ensure_ascii=False, separators=(",", ":")), obj.id),
        )
        self._upsert_object_fts(obj)

    def _insert_route_edge(self, edge: RouteEdge, source: str) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO route_edges(a_id, b_id, operation, result_id, source, status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (edge.a_id, edge.b_id, edge.operation, edge.result_id, source, edge.status),
        )
        if edge.status != "active":
            return
        key = recipe_key(edge.a_id, edge.b_id, edge.operation)
        self.conn.execute(
            """
            INSERT OR REPLACE INTO recipe_cache(recipe_key, a_id, b_id, operation, result_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (key, edge.a_id, edge.b_id, edge.operation, edge.result_id, _utc_now()),
        )

    def _insert_object_alias(self, alias: str, object_id: ObjectId, source: str) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO object_aliases(alias, normalized_alias, object_id, source, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (alias, normalize_name(alias), object_id, source, _utc_now()),
        )

    def _object_from_row(self, row: sqlite3.Row) -> SynthObject:
        payload = json.loads(row["payload_json"])
        payload["status"] = row["status"] or payload.get("status", "active")
        payload["canonical_id"] = row["canonical_id"]
        raw_flags = row["quality_flags"] or "[]"
        payload["quality_flags"] = json.loads(raw_flags)
        return SynthObject.model_validate(payload)

    def _is_online_object(self, obj: SynthObject) -> bool:
        return obj.status == "active" and not obj.is_banned

    def _upsert_object_fts(self, obj: SynthObject) -> None:
        if not self.fts_enabled or obj.id is None:
            return
        self.conn.execute("DELETE FROM object_fts WHERE object_id = ?", (obj.id,))
        if not self._is_online_object(obj):
            return
        self.conn.execute(
            """
            INSERT INTO object_fts(object_id, name, type, search_text)
            VALUES (?, ?, ?, ?)
            """,
            (obj.id, obj.name, obj.type, object_search_text(obj)),
        )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _fts_query(query: str) -> str:
    tokens = [token for token in tokenize(query) if token]
    return " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens[:16])


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _bounded_float(value: object, default: float, min_value: float, max_value: float) -> float:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        parsed = default
    return min(max(parsed, min_value), max_value)


def _mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def _protect_secret(value: str) -> str:
    if not value or sys.platform != "win32":
        return value
    try:
        return "dpapi:" + base64.b64encode(_dpapi_protect(value.encode("utf-8"))).decode("ascii")
    except Exception:
        return value


def _unprotect_secret(value: str) -> str:
    if not value.startswith("dpapi:"):
        return value
    if sys.platform != "win32":
        return ""
    try:
        return _dpapi_unprotect(base64.b64decode(value.removeprefix("dpapi:"))).decode("utf-8")
    except Exception:
        return ""


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_ulong),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _dpapi_protect(data: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    in_buffer = ctypes.create_string_buffer(data)
    in_blob = _DataBlob(len(data), ctypes.cast(in_buffer, ctypes.POINTER(ctypes.c_char)))
    out_blob = _DataBlob()
    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        "MySynthesizer LLM API key",
        None,
        None,
        None,
        0x01,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise OSError("CryptProtectData failed")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _dpapi_unprotect(data: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    in_buffer = ctypes.create_string_buffer(data)
    in_blob = _DataBlob(len(data), ctypes.cast(in_buffer, ctypes.POINTER(ctypes.c_char)))
    out_blob = _DataBlob()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        0x01,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise OSError("CryptUnprotectData failed")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _public_llm_settings(settings: dict[str, object]) -> dict[str, object]:
    public = dict(settings)
    if public.get("api_key"):
        public["api_key"] = _mask_secret(str(public["api_key"]))
    return public
