from __future__ import annotations

import argparse
import json
from pathlib import Path

from .engine import RuleSynthesizerEngine
from .evaluation import evaluate_routes
from .models import CraftRequest
from .store import SQLiteObjectStore


def main() -> None:
    parser = argparse.ArgumentParser(prog="mysynth")
    parser.add_argument("--db", default="data/engine/mysynth.db")
    parser.add_argument("--source", default="outputs/data/current/mysynthesizer_mine_full_routes_latest.json")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("--force", action="store_true")

    craft_parser = subparsers.add_parser("craft")
    craft_parser.add_argument("--a", type=int, required=True)
    craft_parser.add_argument("--b", type=int, required=True)
    craft_parser.add_argument("--operation", choices=["add", "subtract"], required=True)
    craft_parser.add_argument("--no-persist", action="store_true")

    eval_parser = subparsers.add_parser("eval")
    eval_parser.add_argument("--limit", type=int)

    args = parser.parse_args()
    store = SQLiteObjectStore(db_path=args.db, source_path=args.source)
    store.initialize(force_import=getattr(args, "force", False))
    try:
        if args.command == "init":
            print(json.dumps({"ok": True, "db": str(Path(args.db))}, ensure_ascii=False))
        elif args.command == "craft":
            a = store.get_object(args.a)
            b = store.get_object(args.b)
            if a is None or b is None:
                raise SystemExit("ingredient id not found")
            request = CraftRequest(operation=args.operation, ingredient_a=a, ingredient_b=b)
            request.options.persist = not args.no_persist
            result = RuleSynthesizerEngine(store).craft(request)
            print(result.model_dump_json(indent=2))
        elif args.command == "eval":
            summary = evaluate_routes(store, limit=args.limit)
            print(json.dumps(summary.as_dict(), ensure_ascii=False, indent=2))
    finally:
        store.close()


if __name__ == "__main__":
    main()
