from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import urllib.request
import unittest
from pathlib import Path
from unittest.mock import patch

from mysynth.candidate_generators import LLMCandidateGenerator
from mysynth.embeddings import (
    EMBEDDING_STATUS_ACTIVE,
    EMBEDDING_STATUS_STALE,
    EmbeddingText,
    FakeEmbeddingProvider,
    SQLiteEmbeddingStore,
    SQLiteVectorIndex,
    build_candidate_embedding_text,
    build_object_embedding_text,
    build_request_embedding_text,
)
from mysynth.engine import RuleSynthesizerEngine
from mysynth.evaluation import evaluate_routes
from mysynth.models import CandidateObject, CraftOptions, CraftRequest, SynthObject
from mysynth.store import SQLiteObjectStore
from mysynth.workbench import WorkbenchService


def _write_graph(path: Path) -> None:
    graph = {
        "objects": [
            {
                "id": 1,
                "name": "水",
                "emoji": "💧",
                "type": "element",
                "description": "流动、滋养与变化的基础元素。",
                "source": "system",
                "category_ids": [],
            },
            {
                "id": 2,
                "name": "火",
                "emoji": "🔥",
                "type": "element",
                "description": "燃烧、热量与转化的基础元素。",
                "source": "system",
                "category_ids": [],
            },
            {
                "id": 3,
                "name": "氢气",
                "emoji": "💨",
                "type": "element",
                "description": "可燃气体，可作为清洁能源。",
                "source": "llm",
                "category_ids": [],
            },
            {
                "id": 4,
                "name": "石头",
                "emoji": "🪨",
                "type": "item",
                "description": "坚硬、稳定的普通物体。",
                "source": "llm",
                "category_ids": [],
            },
            {
                "id": 5,
                "name": "火蜥蜴",
                "emoji": "🦎",
                "type": "creature",
                "description": "只用于验证单边邻接噪声的生物。",
                "source": "llm",
                "category_ids": [],
            },
            {
                "id": 6,
                "name": "待定水",
                "emoji": "💧",
                "type": "element",
                "description": "特殊待定对象，不应进入默认在线召回。",
                "source": "local_engine",
                "category_ids": [],
                "status": "pending",
            },
            {
                "id": 8,
                "name": "标准水",
                "emoji": "💧",
                "type": "element",
                "description": "canonical 对象，用于验证 merged 解析。",
                "source": "local_engine",
                "category_ids": [],
            },
            {
                "id": 9,
                "name": "旧水",
                "emoji": "💧",
                "type": "element",
                "description": "已归并对象，不应直接作为结果返回。",
                "source": "local_engine",
                "category_ids": [],
                "status": "merged",
                "canonical_id": 8,
            },
        ],
        "route_edges": [
            {
                "result_id": 1,
                "result_name": "水",
                "result_type": "element",
                "result_description": "流动、滋养与变化的基础元素。",
                "operation": "add",
                "a_id": 2,
                "a_name": "火",
                "a_type": "element",
                "a_description": "燃烧、热量与转化的基础元素。",
                "b_id": 3,
                "b_name": "氢气",
                "b_type": "element",
                "b_description": "可燃气体，可作为清洁能源。",
            },
            {
                "result_id": 5,
                "result_name": "火蜥蜴",
                "result_type": "creature",
                "result_description": "只用于验证单边邻接噪声的生物。",
                "operation": "add",
                "a_id": 2,
                "a_name": "火",
                "a_type": "element",
                "a_description": "燃烧、热量与转化的基础元素。",
                "b_id": 4,
                "b_name": "石头",
                "b_type": "item",
                "b_description": "坚硬、稳定的普通物体。",
            }
        ],
    }
    path.write_text(json.dumps(graph, ensure_ascii=False), encoding="utf-8")


class _FakeLLMClient:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls = 0

    def complete(self, messages: list[dict[str, str]], *, timeout: float) -> str:
        self.calls += 1
        return self.response


def _llm_response(name: str = "星火灰烬") -> str:
    return json.dumps(
        {
            "candidates": [
                {
                    "name": name,
                    "type": "element",
                    "description": "由火的热量与石头的稳定性融合出的高温残余。",
                    "emoji": "🔥",
                    "core_tags": ["燃烧", "稳定"],
                    "anchors": [],
                    "source_reason": "LLM 根据两个输入的核心语义生成更自然的候选。",
                }
            ]
        },
        ensure_ascii=False,
    )


class EngineScenarioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.graph_path = root / "graph.json"
        self.db_path = root / "mysynth.db"
        _write_graph(self.graph_path)
        self.store = SQLiteObjectStore(db_path=self.db_path, source_path=self.graph_path)
        self.store.initialize(force_import=True, full_import=True)
        self.env_patch = patch.dict(os.environ, {"MYSYNTH_LLM_API_KEY": "", "MYSYNTH_LLM_MODEL": ""})
        self.env_patch.start()

    def tearDown(self) -> None:
        self.env_patch.stop()
        self.store.close()
        self.temp_dir.cleanup()

    # 测试点：图谱导入后对象和 recipe cache 能被 SQLite store 查询到。
    def test_store_imports_objects_and_recipe_cache(self) -> None:
        water = self.store.get_object(1)
        self.assertIsNotNone(water)
        self.assertEqual(water.name, "水")

        cached = self.store.find_recipe("add:2:3")
        self.assertIsNotNone(cached)
        self.assertEqual(cached.name, "水")

    # 测试点：store 能用无序 add 输入查询 active 路线并返回输入邻接对象。
    def test_store_queries_edges_and_neighbors(self) -> None:
        edges = self.store.find_edges_by_inputs(3, 2, "add")
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0].result_id, 1)

        neighbors = {obj.id for obj in self.store.get_neighbors(2)}
        self.assertIn(1, neighbors)
        self.assertIn(3, neighbors)

    # 测试点：type 加 token 召回会限制类型并返回语义相关对象。
    def test_store_searches_by_type_and_tokens(self) -> None:
        matches = self.store.search_by_type_and_tokens("element", ["燃烧", "清洁能源"], limit=10)
        names = {obj.name for obj in matches}
        self.assertIn("火", names)
        self.assertIn("氢气", names)

    # 测试点：本地文本搜索会召回 active 对象，并过滤 inactive 对象。
    def test_store_search_text_uses_local_index_and_filters_inactive(self) -> None:
        matches = self.store.search_text("清洁能源 特殊待定", limit=10)
        names = {obj.name for obj in matches}

        self.assertIn("氢气", names)
        self.assertNotIn("待定水", names)

    # 测试点：还原会只保留初始 ID 并清空路线、缓存和派生数据。
    def test_reset_to_initial_state_clears_graph_and_side_data(self) -> None:
        SQLiteEmbeddingStore(self.store.conn).ensure_embedding(build_object_embedding_text(self.store.get_object(1)), FakeEmbeddingProvider())
        self.store.conn.execute(
            "INSERT OR REPLACE INTO object_aliases(alias, normalized_alias, object_id, source, created_at) VALUES (?, ?, ?, ?, ?)",
            ("别名水", "别名水", 1, "test", "now"),
        )
        self.store.conn.execute(
            "INSERT INTO failures(request_json, reason, context_json, created_at) VALUES (?, ?, ?, ?)",
            ("{}", "test", "{}", "now"),
        )
        self.store.conn.commit()

        summary = self.store.reset_to_initial_state()

        self.assertTrue(summary["ok"])
        self.assertEqual({obj.id for obj in self.store.list_objects()}, {1, 2, 3, 4})
        self.assertEqual(self.store.list_route_edges(), [])
        for table in ["recipe_cache", "object_aliases", "craft_events", "failures", "embeddings", "embedding_jobs"]:
            count = self.store.conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"]
            self.assertEqual(count, 0, table)
        self.assertTrue({obj.id for obj in self.store.search_text("火蜥蜴 特殊待定", limit=10)} <= {1, 2, 3, 4})
        self.assertIn(2, {obj.id for obj in self.store.search_text("燃烧", limit=10)})

    # 测试点：默认在线视图会隔离 pending 对象，避免进入查询和召回。
    def test_store_filters_inactive_objects_from_online_view(self) -> None:
        self.assertIsNone(self.store.get_object(6))
        self.assertEqual(self.store.get_by_name("待定水"), [])

        matches = self.store.search_by_type_and_tokens("element", ["特殊待定"], limit=10)
        self.assertNotIn("待定水", {obj.name for obj in matches})

    # 测试点：recipe cache 指向 merged 对象时会解析到 canonical 对象。
    def test_recipe_cache_resolves_merged_result_to_canonical(self) -> None:
        self.store.conn.execute("UPDATE recipe_cache SET result_id = 9 WHERE recipe_key = 'add:2:3'")
        self.store.conn.commit()
        self.store.rebuild_indexes()

        cached = self.store.find_recipe("add:2:3")
        self.assertIsNotNone(cached)
        self.assertEqual(cached.id, 8)
        self.assertEqual(cached.name, "标准水")

    # 测试点：recipe cache 指向 pending 对象时不会作为确定结果返回。
    def test_recipe_cache_ignores_pending_result(self) -> None:
        self.store.conn.execute("UPDATE recipe_cache SET result_id = 6 WHERE recipe_key = 'add:2:3'")
        self.store.conn.commit()
        self.store.rebuild_indexes()

        self.assertIsNone(self.store.find_recipe("add:2:3"))

    # 测试点：disabled 路线不会进入默认直接路线证据。
    def test_disabled_route_edges_are_filtered_from_direct_evidence(self) -> None:
        self.store.conn.execute("UPDATE route_edges SET status = 'disabled' WHERE a_id = 2 AND b_id = 3")
        self.store.conn.commit()
        self.store.rebuild_indexes()

        edges = self.store.find_edges_by_inputs(2, 3, "add")

        self.assertEqual(edges, [])

    # 测试点：已有路线的合成会优先命中 recipe cache 并返回已有对象。
    def test_craft_hits_recipe_cache(self) -> None:
        fire = self.store.get_object(2)
        hydrogen = self.store.get_object(3)
        self.assertIsNotNone(fire)
        self.assertIsNotNone(hydrogen)

        result = RuleSynthesizerEngine(self.store).craft(
            CraftRequest(
                operation="add",
                ingredient_a=fire,
                ingredient_b=hydrogen,
                options=CraftOptions(persist=False),
            )
        )

        self.assertTrue(result.success)
        self.assertTrue(result.cached)
        self.assertEqual(result.decision, "matched_existing")
        self.assertEqual(result.result.name, "水")

    # 测试点：recipe cache 命中优先于冲突的直接路线证据。
    def test_recipe_cache_wins_over_conflicting_route_edge(self) -> None:
        self.store.conn.execute(
            """
            INSERT INTO route_edges(a_id, b_id, operation, result_id, source, status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (2, 3, "add", 5, "test", "active"),
        )
        self.store.conn.commit()
        self.store.rebuild_indexes()

        fire = self.store.get_object(2)
        hydrogen = self.store.get_object(3)
        self.assertIsNotNone(fire)
        self.assertIsNotNone(hydrogen)

        result = RuleSynthesizerEngine(self.store).craft(
            CraftRequest(
                operation="add",
                ingredient_a=fire,
                ingredient_b=hydrogen,
                options=CraftOptions(persist=False),
            )
        )

        self.assertTrue(result.cached)
        self.assertEqual(result.result.id, 1)

    # 测试点：recipe cache 缺失时，直接输入边可作为确定结果并修复 cache。
    def test_direct_route_hit_repairs_missing_recipe_cache(self) -> None:
        self.store.conn.execute("DELETE FROM recipe_cache WHERE recipe_key = 'add:2:3'")
        self.store.conn.commit()
        self.store.rebuild_indexes()

        fire = self.store.get_object(2)
        hydrogen = self.store.get_object(3)
        self.assertIsNotNone(fire)
        self.assertIsNotNone(hydrogen)

        result = RuleSynthesizerEngine(self.store).craft(
            CraftRequest(operation="add", ingredient_a=fire, ingredient_b=hydrogen)
        )

        self.assertTrue(result.success)
        self.assertFalse(result.cached)
        self.assertEqual(result.result.id, 1)
        self.assertIn("direct route 命中", result.explanation)
        self.assertIsNotNone(self.store.find_recipe("add:2:3"))

    # 测试点：默认 LLM auto on 时合法结构化候选会进入现有 ranker 和 pending 流程。
    def test_llm_candidate_enters_ranker_by_default(self) -> None:
        fire = self.store.get_object(2)
        stone = self.store.get_object(4)
        self.assertIsNotNone(fire)
        self.assertIsNotNone(stone)
        fake_client = _FakeLLMClient(_llm_response("星火灰烬"))

        result = RuleSynthesizerEngine(
            self.store,
            llm_candidate_generator=LLMCandidateGenerator(client=fake_client),
        ).craft(
            CraftRequest(
                operation="subtract",
                ingredient_a=fire,
                ingredient_b=stone,
                options=CraftOptions(persist=False, match_threshold=1.01, max_retrieved=0),
            )
        )

        self.assertEqual(fake_client.calls, 1)
        self.assertTrue(result.success)
        self.assertEqual(result.decision, "created_pending")
        self.assertEqual(result.candidate.name, "星火灰烬")
        self.assertEqual(result.result.name, "星火灰烬")

    # 测试点：LLM 返回坏 JSON 时会丢弃 LLM 候选并回退到规则候选。
    def test_llm_bad_json_falls_back_to_rule_candidate(self) -> None:
        fire = self.store.get_object(2)
        stone = self.store.get_object(4)
        self.assertIsNotNone(fire)
        self.assertIsNotNone(stone)
        fake_client = _FakeLLMClient("{bad json")

        result = RuleSynthesizerEngine(
            self.store,
            llm_candidate_generator=LLMCandidateGenerator(client=fake_client),
        ).craft(
            CraftRequest(
                operation="subtract",
                ingredient_a=fire,
                ingredient_b=stone,
                options=CraftOptions(persist=False, match_threshold=1.01, max_retrieved=0),
            )
        )

        self.assertEqual(fake_client.calls, 1)
        self.assertTrue(result.success)
        self.assertEqual(result.candidate.name, "无石头火")
        self.assertTrue(result.candidate_errors)

    # 测试点：LLM 返回缺字段 schema 时会丢弃该候选并保留规则兜底。
    def test_llm_bad_schema_falls_back_to_rule_candidate(self) -> None:
        fire = self.store.get_object(2)
        stone = self.store.get_object(4)
        self.assertIsNotNone(fire)
        self.assertIsNotNone(stone)
        fake_client = _FakeLLMClient(json.dumps({"candidates": [{"name": "缺字段候选"}]}, ensure_ascii=False))

        result = RuleSynthesizerEngine(
            self.store,
            llm_candidate_generator=LLMCandidateGenerator(client=fake_client),
        ).craft(
            CraftRequest(
                operation="subtract",
                ingredient_a=fire,
                ingredient_b=stone,
                options=CraftOptions(persist=False, match_threshold=1.01, max_retrieved=0),
            )
        )

        self.assertEqual(fake_client.calls, 1)
        self.assertTrue(result.success)
        self.assertEqual(result.candidate.name, "无石头火")
        self.assertIn("no valid candidates", result.candidate_errors[0])

    # 测试点：未配置真实 LLM 时默认 auto on 会回退规则候选而不阻断 craft。
    def test_unconfigured_llm_falls_back_to_rule_candidate(self) -> None:
        fire = self.store.get_object(2)
        stone = self.store.get_object(4)
        self.assertIsNotNone(fire)
        self.assertIsNotNone(stone)

        with patch.dict(os.environ, {}, clear=True):
            result = RuleSynthesizerEngine(self.store).craft(
                CraftRequest(
                    operation="subtract",
                    ingredient_a=fire,
                    ingredient_b=stone,
                    options=CraftOptions(persist=False, match_threshold=1.01, max_retrieved=0),
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.candidate.name, "无石头火")
        self.assertIn("LLM is not configured", result.candidate_errors[0])

    # 测试点：use_llm 关闭时即使注入了 LLM 生成器也不会调用它。
    def test_llm_is_not_called_when_option_is_disabled(self) -> None:
        fire = self.store.get_object(2)
        stone = self.store.get_object(4)
        self.assertIsNotNone(fire)
        self.assertIsNotNone(stone)
        fake_client = _FakeLLMClient(_llm_response())

        RuleSynthesizerEngine(
            self.store,
            llm_candidate_generator=LLMCandidateGenerator(client=fake_client),
        ).craft(
            CraftRequest(
                operation="subtract",
                ingredient_a=fire,
                ingredient_b=stone,
                options=CraftOptions(persist=False, use_llm=False),
            )
        )

        self.assertEqual(fake_client.calls, 0)

    # 测试点：recipe cache 命中时不会调用 LLM 候选层。
    def test_recipe_cache_hit_does_not_call_llm(self) -> None:
        fire = self.store.get_object(2)
        hydrogen = self.store.get_object(3)
        self.assertIsNotNone(fire)
        self.assertIsNotNone(hydrogen)
        fake_client = _FakeLLMClient(_llm_response())

        result = RuleSynthesizerEngine(
            self.store,
            llm_candidate_generator=LLMCandidateGenerator(client=fake_client),
        ).craft(
            CraftRequest(
                operation="add",
                ingredient_a=fire,
                ingredient_b=hydrogen,
                options=CraftOptions(persist=False),
            )
        )

        self.assertTrue(result.cached)
        self.assertEqual(fake_client.calls, 0)

    # 测试点：direct route 命中时不会调用 LLM 候选层。
    def test_direct_route_hit_does_not_call_llm(self) -> None:
        self.store.conn.execute("DELETE FROM recipe_cache WHERE recipe_key = 'add:2:3'")
        self.store.conn.commit()
        self.store.rebuild_indexes()
        fire = self.store.get_object(2)
        hydrogen = self.store.get_object(3)
        self.assertIsNotNone(fire)
        self.assertIsNotNone(hydrogen)
        fake_client = _FakeLLMClient(_llm_response())

        result = RuleSynthesizerEngine(
            self.store,
            llm_candidate_generator=LLMCandidateGenerator(client=fake_client),
        ).craft(
            CraftRequest(
                operation="add",
                ingredient_a=fire,
                ingredient_b=hydrogen,
                options=CraftOptions(persist=False),
            )
        )

        self.assertIn("direct route 命中", result.explanation)
        self.assertEqual(fake_client.calls, 0)

    # 测试点：候选召回不使用单边邻接，避免无关邻居进入候选池。
    def test_retrieval_does_not_expand_single_side_neighbors(self) -> None:
        fire = self.store.get_object(2)
        hydrogen = self.store.get_object(3)
        self.assertIsNotNone(fire)
        self.assertIsNotNone(hydrogen)
        candidate = CandidateObject(
            name="zz_unique_candidate",
            type="concept",
            description="zz_unique_candidate",
            core_tags=["zz_unique_candidate"],
            source_reason="test",
        )

        retrieved = RuleSynthesizerEngine(self.store)._retrieve_for_candidate(
            candidate,
            CraftRequest(
                operation="subtract",
                ingredient_a=fire,
                ingredient_b=hydrogen,
                options=CraftOptions(persist=False),
            ),
        )

        self.assertNotIn(5, {obj.id for obj in retrieved})

    # 测试点：默认 vector auto on 时已接入的 object top-k 会作为候选证据进入召回池。
    def test_vector_object_retrieval_adds_candidate_evidence_when_wired(self) -> None:
        vector_only = SynthObject(
            id=10,
            name="向量专用对象",
            emoji="💡",
            type="item",
            description="只通过向量召回的对象。",
            source="test",
        )
        self.store._upsert_object(vector_only, vector_only.model_dump(mode="json"))
        self.store.rebuild_indexes()
        candidate = CandidateObject(
            name="zz_vector_candidate",
            type="concept",
            description="zz_vector_candidate",
            core_tags=["zz_vector_candidate"],
            source_reason="test",
        )
        provider = FakeEmbeddingProvider(dimensions=8)
        embedding_store = SQLiteEmbeddingStore(self.store.conn)
        candidate_text = build_candidate_embedding_text(candidate)
        embedding_store.ensure_embedding(
            EmbeddingText(
                owner_type="object",
                owner_id=str(vector_only.id),
                text=candidate_text.text,
                text_hash=candidate_text.text_hash,
            ),
            provider,
        )
        fire = self.store.get_object(2)
        hydrogen = self.store.get_object(3)
        self.assertIsNotNone(fire)
        self.assertIsNotNone(hydrogen)

        retrieved = RuleSynthesizerEngine(
            self.store,
            vector_index=SQLiteVectorIndex(self.store.conn),
            embedding_provider=provider,
        )._retrieve_for_candidate(
            candidate,
            CraftRequest(
                operation="subtract",
                ingredient_a=fire,
                ingredient_b=hydrogen,
                options=CraftOptions(persist=False, vector_top_k=1),
            ),
        )

        self.assertIn(vector_only.id, {obj.id for obj in retrieved})

    # 测试点：未接入 vector provider 时默认 auto on 也不会产生向量召回。
    def test_vector_retrieval_skips_when_provider_is_not_wired(self) -> None:
        vector_only = SynthObject(
            id=11,
            name="未接入向量对象",
            emoji="💡",
            type="item",
            description="只通过向量召回的对象。",
            source="test",
        )
        self.store._upsert_object(vector_only, vector_only.model_dump(mode="json"))
        self.store.rebuild_indexes()
        candidate = CandidateObject(
            name="zz_default_vector_candidate",
            type="concept",
            description="zz_default_vector_candidate",
            core_tags=["zz_default_vector_candidate"],
            source_reason="test",
        )
        candidate_text = build_candidate_embedding_text(candidate)
        SQLiteEmbeddingStore(self.store.conn).ensure_embedding(
            EmbeddingText(
                owner_type="object",
                owner_id=str(vector_only.id),
                text=candidate_text.text,
                text_hash=candidate_text.text_hash,
            ),
            FakeEmbeddingProvider(dimensions=8),
        )
        fire = self.store.get_object(2)
        hydrogen = self.store.get_object(3)
        self.assertIsNotNone(fire)
        self.assertIsNotNone(hydrogen)

        retrieved = RuleSynthesizerEngine(self.store)._retrieve_for_candidate(
            candidate,
            CraftRequest(
                operation="subtract",
                ingredient_a=fire,
                ingredient_b=hydrogen,
                options=CraftOptions(persist=False, vector_top_k=1),
            ),
        )

        self.assertNotIn(vector_only.id, {obj.id for obj in retrieved})

    # 测试点：recipe vector top-k 结果只作为相似历史结果进入召回池。
    def test_vector_recipe_retrieval_adds_result_evidence(self) -> None:
        fire = self.store.get_object(2)
        hydrogen = self.store.get_object(3)
        self.assertIsNotNone(fire)
        self.assertIsNotNone(hydrogen)
        request = CraftRequest(
            operation="subtract",
            ingredient_a=fire,
            ingredient_b=hydrogen,
            options=CraftOptions(persist=False, vector_top_k=1, use_vectors=True),
        )
        provider = FakeEmbeddingProvider(dimensions=8)
        embedding_store = SQLiteEmbeddingStore(self.store.conn)
        request_text = build_request_embedding_text(request)
        embedding_store.ensure_embedding(
            EmbeddingText(
                owner_type="recipe",
                owner_id="subtract:2:3:1",
                text=request_text.text,
                text_hash=request_text.text_hash,
            ),
            provider,
        )
        candidate = CandidateObject(
            name="zz_recipe_vector_candidate",
            type="concept",
            description="zz_recipe_vector_candidate",
            core_tags=["zz_recipe_vector_candidate"],
            source_reason="test",
        )

        retrieved = RuleSynthesizerEngine(
            self.store,
            vector_index=SQLiteVectorIndex(self.store.conn),
            embedding_provider=provider,
        )._retrieve_for_candidate(candidate, request)

        self.assertIn(1, {obj.id for obj in retrieved})

    # 测试点：recipe cache 命中时不会被冲突的 vector 相似结果覆盖。
    def test_recipe_cache_wins_over_conflicting_vector_result(self) -> None:
        fire = self.store.get_object(2)
        hydrogen = self.store.get_object(3)
        self.assertIsNotNone(fire)
        self.assertIsNotNone(hydrogen)
        request = CraftRequest(
            operation="add",
            ingredient_a=fire,
            ingredient_b=hydrogen,
            options=CraftOptions(persist=False, vector_top_k=1, use_vectors=True),
        )
        provider = FakeEmbeddingProvider(dimensions=8)
        request_text = build_request_embedding_text(request)
        SQLiteEmbeddingStore(self.store.conn).ensure_embedding(
            EmbeddingText(
                owner_type="recipe",
                owner_id="add:2:3:5",
                text=request_text.text,
                text_hash=request_text.text_hash,
            ),
            provider,
        )

        result = RuleSynthesizerEngine(
            self.store,
            vector_index=SQLiteVectorIndex(self.store.conn),
            embedding_provider=provider,
        ).craft(request)

        self.assertTrue(result.cached)
        self.assertEqual(result.result.id, 1)

    # 测试点：未命中缓存且禁用持久化时会生成 transient pending 候选但不写入 SQLite。
    def test_uncached_no_persist_creates_transient_pending_candidate(self) -> None:
        water = self.store.get_object(1)
        hydrogen = self.store.get_object(3)
        self.assertIsNotNone(water)
        self.assertIsNotNone(hydrogen)

        result = RuleSynthesizerEngine(self.store).craft(
            CraftRequest(
                operation="subtract",
                ingredient_a=water,
                ingredient_b=hydrogen,
                options=CraftOptions(persist=False),
            )
        )

        self.assertTrue(result.success)
        self.assertEqual(result.decision, "created_pending")
        self.assertIsNone(result.result.id)
        self.assertEqual(result.result.status, "pending")
        self.assertIsNone(self.store.find_recipe("subtract:1:3"))

    # 测试点：还原后初始元素合成不命中旧路线，会生成隔离的 pending 对象。
    def test_craft_after_reset_creates_pending_without_active_recall(self) -> None:
        self.store.reset_to_initial_state()
        fire = self.store.get_object(2)
        initial = self.store.get_object(3)
        self.assertIsNotNone(fire)
        self.assertIsNotNone(initial)

        result = RuleSynthesizerEngine(self.store).craft(
            CraftRequest(operation="add", ingredient_a=fire, ingredient_b=initial)
        )

        self.assertEqual(result.decision, "created_pending")
        self.assertIsNotNone(result.result.id)
        self.assertIsNone(self.store.get_object(result.result.id))
        self.assertEqual(self.store.find_edges_by_inputs(2, 3, "add"), [])

    # 测试点：pending 新对象持久化后不会进入默认在线对象、active route 或 recipe cache。
    def test_created_pending_persists_without_active_recall_or_cache(self) -> None:
        fire = self.store.get_object(2)
        hydrogen = self.store.get_object(3)
        self.assertIsNotNone(fire)
        self.assertIsNotNone(hydrogen)

        result = RuleSynthesizerEngine(self.store).craft(
            CraftRequest(operation="subtract", ingredient_a=fire, ingredient_b=hydrogen)
        )

        self.assertTrue(result.success)
        self.assertEqual(result.decision, "created_pending")
        self.assertIsNotNone(result.result.id)
        self.assertEqual(result.result.status, "pending")
        self.assertIsNone(self.store.get_object(result.result.id))
        self.assertEqual(self.store.all_objects_by_id[result.result.id].status, "pending")
        self.assertIsNone(self.store.find_recipe("subtract:2:3"))

        edge_row = self.store.conn.execute(
            "SELECT status FROM route_edges WHERE a_id = ? AND b_id = ? AND operation = ? AND result_id = ?",
            (2, 3, "subtract", result.result.id),
        ).fetchone()
        self.assertIsNotNone(edge_row)
        self.assertEqual(edge_row["status"], "disabled")

    # 测试点：promote 会把 pending 对象、disabled route 和 recipe cache 一起激活。
    def test_promote_pending_object_activates_route_and_cache(self) -> None:
        fire = self.store.get_object(2)
        hydrogen = self.store.get_object(3)
        self.assertIsNotNone(fire)
        self.assertIsNotNone(hydrogen)
        result = RuleSynthesizerEngine(self.store).craft(
            CraftRequest(operation="subtract", ingredient_a=fire, ingredient_b=hydrogen)
        )
        pending_id = result.result.id
        self.assertIsNotNone(pending_id)

        promoted = self.store.promote_pending_object(pending_id)

        self.assertEqual(promoted.status, "active")
        self.assertEqual(promoted.quality_flags, [])
        self.assertIsNotNone(self.store.get_object(pending_id))
        self.assertIsNotNone(self.store.find_recipe("subtract:2:3"))
        edges = self.store.find_edges_by_inputs(2, 3, "subtract")
        self.assertEqual([edge.result_id for edge in edges], [pending_id])

    # 测试点：promote 只接受 pending 对象，避免误改 active 事实。
    def test_promote_rejects_non_pending_object(self) -> None:
        with self.assertRaises(ValueError):
            self.store.promote_pending_object(1)

    # 测试点：reject 会把 pending 对象标记为 rejected，并保持路线和 cache 隔离。
    def test_reject_pending_object_keeps_route_and_cache_inactive(self) -> None:
        fire = self.store.get_object(2)
        hydrogen = self.store.get_object(3)
        self.assertIsNotNone(fire)
        self.assertIsNotNone(hydrogen)
        result = RuleSynthesizerEngine(self.store).craft(
            CraftRequest(operation="subtract", ingredient_a=fire, ingredient_b=hydrogen)
        )
        pending_id = result.result.id
        self.assertIsNotNone(pending_id)

        rejected = self.store.reject_pending_object(pending_id, reason="bad_name")

        self.assertEqual(rejected.status, "rejected")
        self.assertIn("rejected", rejected.quality_flags)
        self.assertIn("review_reason:bad_name", rejected.quality_flags)
        self.assertIsNone(self.store.get_object(pending_id))
        self.assertIsNone(self.store.find_recipe("subtract:2:3"))
        edges = self.store.find_edges_by_inputs(2, 3, "subtract")
        self.assertEqual(edges, [])

    # 测试点：merge 会把 pending 对象归并到 canonical，并将路线和 cache 指向 canonical。
    def test_merge_pending_object_points_route_and_cache_to_canonical(self) -> None:
        fire = self.store.get_object(2)
        hydrogen = self.store.get_object(3)
        canonical = self.store.get_object(1)
        self.assertIsNotNone(fire)
        self.assertIsNotNone(hydrogen)
        self.assertIsNotNone(canonical)
        result = RuleSynthesizerEngine(self.store).craft(
            CraftRequest(operation="subtract", ingredient_a=fire, ingredient_b=hydrogen)
        )
        pending_id = result.result.id
        self.assertIsNotNone(pending_id)

        merged = self.store.merge_pending_object(pending_id, canonical.id)

        self.assertEqual(merged.status, "merged")
        self.assertEqual(merged.canonical_id, canonical.id)
        self.assertEqual(self.store.get_object(pending_id).id, canonical.id)
        cached = self.store.find_recipe("subtract:2:3")
        self.assertIsNotNone(cached)
        self.assertEqual(cached.id, canonical.id)
        edges = self.store.find_edges_by_inputs(2, 3, "subtract")
        self.assertEqual([edge.result_id for edge in edges], [canonical.id])
        alias_row = self.store.conn.execute("SELECT object_id FROM object_aliases WHERE alias = ?", (result.result.name,)).fetchone()
        self.assertIsNotNone(alias_row)
        self.assertEqual(alias_row["object_id"], canonical.id)

    # 测试点：候选名称与 active 对象归一化一致时会归并到已有 canonical 对象。
    def test_duplicate_candidate_name_merges_existing_object(self) -> None:
        duplicate = SynthObject(
            id=10,
            name="无氢气水",
            emoji="💧",
            type="element",
            description="已有 canonical 对象。",
            source="test",
        )
        self.store._upsert_object(duplicate, duplicate.model_dump(mode="json"))
        self.store.rebuild_indexes()
        water = self.store.get_object(1)
        hydrogen = self.store.get_object(3)
        self.assertIsNotNone(water)
        self.assertIsNotNone(hydrogen)

        result = RuleSynthesizerEngine(self.store).craft(
            CraftRequest(operation="subtract", ingredient_a=water, ingredient_b=hydrogen)
        )

        self.assertTrue(result.success)
        self.assertEqual(result.decision, "merged_existing")
        self.assertEqual(result.result.id, 10)
        self.assertIsNotNone(self.store.find_recipe("subtract:1:3"))
        alias_row = self.store.conn.execute("SELECT object_id FROM object_aliases WHERE alias = ?", ("无氢气水",)).fetchone()
        self.assertIsNotNone(alias_row)
        self.assertEqual(alias_row["object_id"], 10)

    # 测试点：回放评估会把 recipe cache 命中计入 exact 和 top5 指标。
    def test_route_replay_counts_cache_hits(self) -> None:
        summary = evaluate_routes(self.store, limit=1)
        self.assertEqual(summary.total, 1)
        self.assertEqual(summary.exact_id_match, 1)
        self.assertEqual(summary.top5_match, 1)
        self.assertIn("add:element+element->element", summary.buckets)

    # 测试点：回放评估在 cache 缺失时仍可通过 direct route 命中。
    def test_route_replay_counts_direct_route_hits_when_cache_missing(self) -> None:
        self.store.conn.execute("DELETE FROM recipe_cache")
        self.store.conn.commit()
        self.store.rebuild_indexes()

        summary = evaluate_routes(self.store, limit=1, max_failures=5)

        self.assertEqual(summary.total, 1)
        self.assertEqual(summary.exact_id_match, 1)
        self.assertEqual(len(summary.failures), 0)

    # 测试点：同一对象文本和模型重复生成 embedding 时会复用已有 active 记录。
    def test_embedding_store_deduplicates_same_text(self) -> None:
        water = self.store.get_object(1)
        self.assertIsNotNone(water)
        provider = FakeEmbeddingProvider(dimensions=8)
        embedding_store = SQLiteEmbeddingStore(self.store.conn)
        text = build_object_embedding_text(water)

        first = embedding_store.ensure_embedding(text, provider)
        second = embedding_store.ensure_embedding(text, provider)

        self.assertEqual(first.id, second.id)
        self.assertEqual(embedding_store.count_by_status(EMBEDDING_STATUS_ACTIVE), 1)

    # 测试点：对象 embedding 文本变化后旧 active 记录会标记为 stale。
    def test_embedding_store_marks_changed_text_stale(self) -> None:
        water = self.store.get_object(1)
        self.assertIsNotNone(water)
        provider = FakeEmbeddingProvider(dimensions=8)
        embedding_store = SQLiteEmbeddingStore(self.store.conn)

        embedding_store.ensure_embedding(build_object_embedding_text(water), provider)
        changed = water.model_copy(update={"description": "新的水描述"})
        embedding_store.ensure_embedding(build_object_embedding_text(changed), provider)

        self.assertEqual(embedding_store.count_by_status(EMBEDDING_STATUS_ACTIVE), 1)
        self.assertEqual(embedding_store.count_by_status(EMBEDDING_STATUS_STALE), 1)

    # 测试点：vector search 会按模型维度过滤，避免不同维度向量互相召回。
    def test_vector_search_filters_by_dimensions(self) -> None:
        water = self.store.get_object(1)
        self.assertIsNotNone(water)
        provider8 = FakeEmbeddingProvider(dimensions=8)
        provider16 = FakeEmbeddingProvider(dimensions=16)
        SQLiteEmbeddingStore(self.store.conn).ensure_embedding(build_object_embedding_text(water), provider8)

        results = SQLiteVectorIndex(self.store.conn).search_text(
            build_object_embedding_text(water),
            provider16,
            owner_type="object",
            top_k=5,
        )

        self.assertEqual(results, [])

    # 测试点：WorkbenchService 能通过本地 API 边界完成一次 craft。
    def test_workbench_service_can_craft_once(self) -> None:
        result = WorkbenchService(self.store).craft({"a_id": 2, "b_id": 3, "operation": "add", "persist": False})

        self.assertTrue(result["success"])
        self.assertEqual(result["result"]["id"], 1)
        self.assertTrue(result["cached"])

    # 测试点：WorkbenchService 能读取对象详情供本地 UI 展示。
    def test_workbench_service_returns_object_detail(self) -> None:
        result = WorkbenchService(self.store).get_object_detail(1)

        self.assertEqual(result["object"]["name"], "水")
        self.assertEqual(result["object"]["status"], "active")

    # 测试点：WorkbenchService reset 会调用同一套还原逻辑并返回摘要。
    def test_workbench_service_resets_to_initial_state(self) -> None:
        result = WorkbenchService(self.store).reset()

        self.assertTrue(result["ok"])
        self.assertEqual({obj.id for obj in self.store.list_objects()}, {1, 2, 3, 4})

    # 测试点：workbench HTTP 入口能访问搜索、详情、LLM craft 和还原接口。
    def test_workbench_http_endpoints_respond(self) -> None:
        process = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-m",
                "mysynth",
                "--db",
                str(self.db_path),
                "--source",
                str(self.graph_path),
                "workbench",
                "--port",
                "0",
                "--no-browser",
            ],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "MYSYNTH_LLM_FAKE_RESPONSE": _llm_response("接口星火")},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            self.assertIsNotNone(process.stdout)
            line = process.stdout.readline()
            payload = json.loads(line)
            with urllib.request.urlopen(f"{payload['url']}api/search?q=%E6%B0%B4&limit=1", timeout=5) as response:
                search_data = json.loads(response.read().decode("utf-8"))
            with urllib.request.urlopen(f"{payload['url']}api/objects/1", timeout=5) as response:
                detail_data = json.loads(response.read().decode("utf-8"))
            craft_request = urllib.request.Request(
                f"{payload['url']}api/craft",
                data=json.dumps(
                    {
                        "a_id": 2,
                        "b_id": 4,
                        "operation": "subtract",
                        "persist": False,
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(craft_request, timeout=5) as response:
                craft_data = json.loads(response.read().decode("utf-8"))
            reset_request = urllib.request.Request(
                f"{payload['url']}api/reset",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(reset_request, timeout=5) as response:
                reset_data = json.loads(response.read().decode("utf-8"))

            self.assertIn("objects", search_data)
            self.assertEqual(detail_data["object"]["name"], "水")
            self.assertEqual(craft_data["candidate"]["name"], "接口星火")
            self.assertEqual({obj["id"] for obj in reset_data["kept_objects"]}, {1, 2, 3, 4})
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()

    # 测试点：桌面打包脚本 dry-run 会输出单机包结构计划。
    def test_build_desktop_dry_run_reports_package_plan(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-B", "scripts/build_desktop.py", "--dry-run"],
            cwd=Path(__file__).resolve().parents[1],
            text=True,
            capture_output=True,
            check=False,
        )
        payload = json.loads(completed.stdout)

        self.assertEqual(completed.returncode, 0)
        self.assertTrue(payload["ok"])
        self.assertIn("ui", payload["plan"])

    # 测试点：CLI init 默认只保留初始元素，--full 保留完整导入。
    def test_cli_init_default_and_full_modes(self) -> None:
        root = Path(__file__).resolve().parents[1]
        default_db = Path(self.temp_dir.name) / "cli_default.db"
        full_db = Path(self.temp_dir.name) / "cli_full.db"

        default_result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "mysynth",
                "--db",
                str(default_db),
                "--source",
                str(self.graph_path),
                "init",
                "--force",
            ],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )
        full_result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "mysynth",
                "--db",
                str(full_db),
                "--source",
                str(self.graph_path),
                "init",
                "--force",
                "--full",
            ],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(default_result.returncode, 0)
        self.assertEqual(full_result.returncode, 0)
        default_store = SQLiteObjectStore(db_path=default_db, source_path=self.graph_path)
        full_store = SQLiteObjectStore(db_path=full_db, source_path=self.graph_path)
        try:
            default_store.initialize(full_import=True)
            full_store.initialize(full_import=True)
            self.assertEqual({obj.id for obj in default_store.list_objects()}, {1, 2, 3, 4})
            self.assertGreater(len(list(full_store.list_objects())), 4)
        finally:
            default_store.close()
            full_store.close()

    # 测试点：CLI craft 默认会读取兼容 LLM 候选并保持 pending 输出。
    def test_cli_craft_uses_llm_by_default_with_fake_response(self) -> None:
        root = Path(__file__).resolve().parents[1]
        db_path = Path(self.temp_dir.name) / "cli_llm.db"

        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "mysynth",
                "--db",
                str(db_path),
                "--source",
                str(self.graph_path),
                "craft",
                "--a",
                "2",
                "--b",
                "4",
                "--operation",
                "subtract",
                "--no-persist",
            ],
            cwd=root,
            env={**os.environ, "MYSYNTH_LLM_FAKE_RESPONSE": _llm_response("命令行星火")},
            text=True,
            capture_output=True,
            check=False,
        )
        payload = json.loads(completed.stdout)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(payload["candidate"]["name"], "命令行星火")
        self.assertEqual(payload["decision"], "created_pending")

    # 测试点：CLI craft --no-llm 会关闭 LLM 候选用于开发排障。
    def test_cli_craft_can_disable_llm_for_debugging(self) -> None:
        root = Path(__file__).resolve().parents[1]
        db_path = Path(self.temp_dir.name) / "cli_no_llm.db"

        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "mysynth",
                "--db",
                str(db_path),
                "--source",
                str(self.graph_path),
                "craft",
                "--a",
                "2",
                "--b",
                "4",
                "--operation",
                "subtract",
                "--no-llm",
                "--no-persist",
            ],
            cwd=root,
            env={**os.environ, "MYSYNTH_LLM_FAKE_RESPONSE": _llm_response("不应出现")},
            text=True,
            capture_output=True,
            check=False,
        )
        payload = json.loads(completed.stdout)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotEqual(payload["candidate"]["name"], "不应出现")

    # 测试点：CLI reset 必须显式确认，确认后只保留初始元素。
    def test_cli_reset_requires_yes_and_resets(self) -> None:
        root = Path(__file__).resolve().parents[1]
        db_path = Path(self.temp_dir.name) / "cli_reset.db"
        subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "mysynth",
                "--db",
                str(db_path),
                "--source",
                str(self.graph_path),
                "init",
                "--force",
                "--full",
            ],
            cwd=root,
            text=True,
            capture_output=True,
            check=True,
        )

        rejected = subprocess.run(
            [sys.executable, "-B", "-m", "mysynth", "--db", str(db_path), "--source", str(self.graph_path), "reset"],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )
        accepted = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "mysynth",
                "--db",
                str(db_path),
                "--source",
                str(self.graph_path),
                "reset",
                "--yes",
            ],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )
        payload = json.loads(accepted.stdout)

        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(accepted.returncode, 0)
        self.assertEqual({obj["id"] for obj in payload["kept_objects"]}, {1, 2, 3, 4})


if __name__ == "__main__":
    unittest.main()
