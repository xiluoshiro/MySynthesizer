from __future__ import annotations

import argparse
import json
from pathlib import Path

from .candidate_generators import LLMCandidateGenerator
from .embeddings import (
    EMBEDDING_STATUS_ACTIVE,
    FakeEmbeddingProvider,
    SQLiteEmbeddingStore,
    SQLiteVectorIndex,
    build_object_embedding_text,
    build_recipe_embedding_text,
)
from .engine import RuleSynthesizerEngine
from .evaluation import evaluate_routes
from .models import CraftRequest
from .store import SQLiteObjectStore
from .workbench import run_workbench


def main() -> None:
    parser = argparse.ArgumentParser(prog="mysynth")
    parser.add_argument("--db", default="data/engine/mysynth.db")
    parser.add_argument("--source", default="outputs/data/current/mysynthesizer_mine_full_routes_latest.json")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("--force", action="store_true")
    init_parser.add_argument("--full", action="store_true")

    reset_parser = subparsers.add_parser("reset")
    reset_parser.add_argument("--yes", action="store_true")

    craft_parser = subparsers.add_parser("craft")
    craft_parser.add_argument("--a", type=int, required=True)
    craft_parser.add_argument("--b", type=int, required=True)
    craft_parser.add_argument("--operation", choices=["add", "subtract"], required=True)
    craft_parser.add_argument("--no-persist", action="store_true")
    craft_parser.add_argument("--use-vectors", action="store_true")
    craft_parser.add_argument("--use-llm", action="store_true")
    craft_parser.add_argument("--vector-dimensions", type=int, default=16)

    eval_parser = subparsers.add_parser("eval")
    eval_parser.add_argument("--limit", type=int)
    eval_parser.add_argument("--failures", type=int, default=20)

    workbench_parser = subparsers.add_parser("workbench")
    workbench_parser.add_argument("--host", default="127.0.0.1")
    workbench_parser.add_argument("--port", type=int, default=8765)
    workbench_parser.add_argument("--ui-dir", default="ui")
    workbench_parser.add_argument("--no-browser", action="store_true")

    review_parser = subparsers.add_parser("review")
    review_subparsers = review_parser.add_subparsers(dest="review_command", required=True)
    promote_parser = review_subparsers.add_parser("promote")
    promote_parser.add_argument("--id", type=int, required=True)
    reject_parser = review_subparsers.add_parser("reject")
    reject_parser.add_argument("--id", type=int, required=True)
    reject_parser.add_argument("--reason")
    merge_parser = review_subparsers.add_parser("merge")
    merge_parser.add_argument("--id", type=int, required=True)
    merge_parser.add_argument("--canonical-id", type=int, required=True)

    embed_parser = subparsers.add_parser("embed")
    embed_subparsers = embed_parser.add_subparsers(dest="embed_command", required=True)
    embed_objects_parser = embed_subparsers.add_parser("objects")
    embed_objects_parser.add_argument("--limit", type=int)
    embed_objects_parser.add_argument("--dimensions", type=int, default=16)
    embed_recipes_parser = embed_subparsers.add_parser("recipes")
    embed_recipes_parser.add_argument("--limit", type=int)
    embed_recipes_parser.add_argument("--dimensions", type=int, default=16)

    args = parser.parse_args()
    if args.command == "reset" and not args.yes:
        raise SystemExit("reset is destructive; rerun with --yes")
    if args.command == "workbench":
        run_workbench(
            db_path=args.db,
            source_path=args.source,
            host=args.host,
            port=args.port,
            ui_dir=Path(args.ui_dir),
            open_browser=not args.no_browser,
        )
        return

    store = SQLiteObjectStore(db_path=args.db, source_path=args.source)
    store.initialize(force_import=getattr(args, "force", False), full_import=getattr(args, "full", False))
    try:
        if args.command == "init":
            print(json.dumps({"ok": True, "db": str(Path(args.db)), "full": args.full}, ensure_ascii=False))
        elif args.command == "reset":
            print(json.dumps(store.reset_to_initial_state(), ensure_ascii=False, indent=2))
        elif args.command == "craft":
            a = store.get_object(args.a)
            b = store.get_object(args.b)
            if a is None or b is None:
                raise SystemExit("ingredient id not found")
            request = CraftRequest(operation=args.operation, ingredient_a=a, ingredient_b=b)
            request.options.persist = not args.no_persist
            request.options.use_vectors = args.use_vectors
            request.options.use_llm = args.use_llm
            provider = FakeEmbeddingProvider(dimensions=args.vector_dimensions)
            vector_index = SQLiteVectorIndex(store.conn) if request.options.use_vectors else None
            llm_generator = LLMCandidateGenerator() if request.options.use_llm else None
            result = RuleSynthesizerEngine(
                store,
                vector_index=vector_index,
                embedding_provider=provider,
                llm_candidate_generator=llm_generator,
            ).craft(request)
            print(result.model_dump_json(indent=2))
        elif args.command == "eval":
            summary = evaluate_routes(store, limit=args.limit, max_failures=args.failures)
            print(json.dumps(summary.as_dict(), ensure_ascii=False, indent=2))
        elif args.command == "review":
            if args.review_command == "promote":
                promoted = store.promote_pending_object(args.id)
                print(
                    json.dumps(
                        {
                            "ok": True,
                            "object": promoted.model_dump(mode="json"),
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            elif args.review_command == "reject":
                rejected = store.reject_pending_object(args.id, reason=args.reason)
                print(
                    json.dumps(
                        {
                            "ok": True,
                            "object": rejected.model_dump(mode="json"),
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            elif args.review_command == "merge":
                merged = store.merge_pending_object(args.id, args.canonical_id)
                print(
                    json.dumps(
                        {
                            "ok": True,
                            "object": merged.model_dump(mode="json"),
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
        elif args.command == "embed":
            if args.embed_command == "objects":
                provider = FakeEmbeddingProvider(dimensions=args.dimensions)
                embedding_store = SQLiteEmbeddingStore(store.conn)
                objects = list(store.list_objects())
                if args.limit is not None:
                    objects = objects[: args.limit]
                for obj in objects:
                    embedding_store.ensure_embedding(build_object_embedding_text(obj), provider)
                print(
                    json.dumps(
                        {
                            "embedded": len(objects),
                            "model": provider.model,
                            "dimensions": provider.dimensions,
                            "active": embedding_store.count_by_status(EMBEDDING_STATUS_ACTIVE),
                        },
                        ensure_ascii=False,
                    )
                )
            elif args.embed_command == "recipes":
                provider = FakeEmbeddingProvider(dimensions=args.dimensions)
                embedding_store = SQLiteEmbeddingStore(store.conn)
                edges = store.list_route_edges(limit=args.limit)
                for edge in edges:
                    embedding_store.ensure_embedding(build_recipe_embedding_text(edge), provider)
                print(
                    json.dumps(
                        {
                            "embedded": len(edges),
                            "model": provider.model,
                            "dimensions": provider.dimensions,
                            "active": embedding_store.count_by_status(EMBEDDING_STATUS_ACTIVE),
                        },
                        ensure_ascii=False,
                    )
                )
    finally:
        store.close()


if __name__ == "__main__":
    main()
