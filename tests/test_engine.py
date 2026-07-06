from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mysynth.engine import RuleSynthesizerEngine
from mysynth.evaluation import evaluate_routes
from mysynth.models import CraftOptions, CraftRequest
from mysynth.store import SQLiteObjectStore


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
            }
        ],
    }
    path.write_text(json.dumps(graph, ensure_ascii=False), encoding="utf-8")


class EngineScenarioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.graph_path = root / "graph.json"
        self.db_path = root / "mysynth.db"
        _write_graph(self.graph_path)
        self.store = SQLiteObjectStore(db_path=self.db_path, source_path=self.graph_path)
        self.store.initialize(force_import=True)

    def tearDown(self) -> None:
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

    # 测试点：store 能用无序 add 输入查询路线并返回输入邻接对象。
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

    # 测试点：未命中缓存且禁用持久化时会生成本地候选但不写入 SQLite。
    def test_uncached_no_persist_creates_transient_candidate(self) -> None:
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
        self.assertEqual(result.decision, "created_new")
        self.assertIsNone(result.result.id)
        self.assertIsNone(self.store.find_recipe("subtract:1:3"))

    # 测试点：回放评估会把 recipe cache 命中计入 exact 和 top5 指标。
    def test_route_replay_counts_cache_hits(self) -> None:
        summary = evaluate_routes(self.store, limit=10)
        self.assertEqual(summary.total, 1)
        self.assertEqual(summary.exact_id_match, 1)
        self.assertEqual(summary.top5_match, 1)
        self.assertIn("add:element+element->element", summary.buckets)

    # 测试点：评估报告会记录未精确命中的失败样本，便于后续调参。
    def test_route_replay_reports_failure_samples(self) -> None:
        self.store.conn.execute("DELETE FROM recipe_cache")
        self.store.conn.commit()
        self.store.rebuild_indexes()

        summary = evaluate_routes(self.store, limit=10, max_failures=5)

        self.assertEqual(summary.total, 1)
        self.assertLess(summary.exact_id_match, 1)
        self.assertEqual(len(summary.failures), 1)
        self.assertEqual(summary.failures[0].expected_name, "水")


if __name__ == "__main__":
    unittest.main()
