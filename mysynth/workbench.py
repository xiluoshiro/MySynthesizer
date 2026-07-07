from __future__ import annotations

import argparse
import json
import mimetypes
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .embeddings import FakeEmbeddingProvider, SQLiteVectorIndex
from .engine import RuleSynthesizerEngine
from .models import CraftRequest
from .store import SQLiteObjectStore


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_UI_DIR = ROOT / "ui"


class WorkbenchService:
    def __init__(self, store: SQLiteObjectStore) -> None:
        self.store = store

    def search_objects(self, query: str, limit: int = 20) -> dict[str, object]:
        if query.strip():
            objects = self.store.search_text(query, limit=limit)
        else:
            objects = list(self.store.list_objects())[:limit]
        return {"objects": [_public_object(obj) for obj in objects]}

    def craft(self, payload: dict[str, object]) -> dict[str, object]:
        a_id = int(payload["a_id"])
        b_id = int(payload["b_id"])
        operation = str(payload["operation"])
        a = self.store.get_object(a_id)
        b = self.store.get_object(b_id)
        if a is None or b is None:
            raise ValueError("ingredient id not found")
        request = CraftRequest(operation=operation, ingredient_a=a, ingredient_b=b)  # type: ignore[arg-type]
        request.options.persist = bool(payload.get("persist", True))
        request.options.use_vectors = bool(payload.get("use_vectors", False))
        vector_index = SQLiteVectorIndex(self.store.conn) if request.options.use_vectors else None
        provider = FakeEmbeddingProvider()
        result = RuleSynthesizerEngine(self.store, vector_index=vector_index, embedding_provider=provider).craft(request)
        return result.model_dump(mode="json")

    def review(self, action: str, payload: dict[str, object]) -> dict[str, object]:
        object_id = int(payload["id"])
        if action == "promote":
            obj = self.store.promote_pending_object(object_id)
        elif action == "reject":
            obj = self.store.reject_pending_object(object_id, reason=_optional_str(payload.get("reason")))
        elif action == "merge":
            obj = self.store.merge_pending_object(object_id, int(payload["canonical_id"]))
        else:
            raise ValueError(f"unknown review action: {action}")
        return {"ok": True, "object": obj.model_dump(mode="json")}


def run_workbench(
    *,
    db_path: str,
    source_path: str,
    host: str,
    port: int,
    ui_dir: Path = DEFAULT_UI_DIR,
    open_browser: bool = True,
) -> None:
    store = SQLiteObjectStore(db_path=db_path, source_path=source_path)
    store.initialize()
    service = WorkbenchService(store)
    handler_cls = _handler(service, ui_dir)
    server = HTTPServer((host, port), handler_cls)
    url = f"http://{host}:{server.server_address[1]}/"
    print(json.dumps({"ok": True, "url": url}, ensure_ascii=False), flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    finally:
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="mysynth-workbench")
    parser.add_argument("--db", default="data/engine/mysynth.db")
    parser.add_argument("--source", default="outputs/data/current/mysynthesizer_mine_full_routes_latest.json")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--ui-dir", default=str(DEFAULT_UI_DIR))
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    run_workbench(
        db_path=args.db,
        source_path=args.source,
        host=args.host,
        port=args.port,
        ui_dir=Path(args.ui_dir),
        open_browser=not args.no_browser,
    )


def _handler(service: WorkbenchService, ui_dir: Path) -> type[BaseHTTPRequestHandler]:
    class WorkbenchHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/api/search":
                params = parse_qs(parsed.query)
                query = params.get("q", [""])[0]
                limit = int(params.get("limit", ["20"])[0])
                self._json(service.search_objects(query, limit=limit))
                return
            self._static(parsed.path)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            try:
                payload = self._read_json()
                if parsed.path == "/api/craft":
                    self._json(service.craft(payload))
                    return
                if parsed.path.startswith("/api/review/"):
                    action = parsed.path.rsplit("/", 1)[-1]
                    self._json(service.review(action, payload))
                    return
                self._json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
            except Exception as exc:
                self._json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

        def log_message(self, format: str, *args: object) -> None:
            return

        def _read_json(self) -> dict[str, object]:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8")
            return json.loads(raw or "{}")

        def _json(self, payload: dict[str, object], status: HTTPStatus = HTTPStatus.OK) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _static(self, path: str) -> None:
            rel = "index.html" if path in {"", "/"} else path.lstrip("/")
            target = (ui_dir / rel).resolve()
            if not str(target).startswith(str(ui_dir.resolve())) or not target.is_file():
                self._json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
                return
            body = target.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return WorkbenchHandler


def _public_object(obj) -> dict[str, object]:
    return {
        "id": obj.id,
        "name": obj.name,
        "emoji": obj.emoji,
        "type": obj.type,
        "description": obj.description,
        "status": obj.status,
    }


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


if __name__ == "__main__":
    main()
