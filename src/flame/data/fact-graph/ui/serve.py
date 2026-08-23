#!/usr/bin/env python3
"""fact-graph UI 微型服务器 (纯 stdlib)。

职责:
  - 静态托管 ui/ 下的 index.html / vendor /
  - 只读暴露 .fact-graph/runs/ 下的 run: board.json / events.jsonl / RESULT.md / config.toml
  - 唯一写操作: POST /api/runs/<name>/hint 把 hint 追加进 inbox.jsonl
    (编排器下一拍会吸收入图; 这也是 UI 里 hint 注入功能的唯一后端)

用法:
  python3 serve.py                 # 默认 runs 根目录 ./.fact-graph/runs, 端口 8720
  python3 serve.py --root ~/.fact-graph/runs --port 9000
  python3 serve.py --open          # 自动打开浏览器
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

UI_DIR = Path(__file__).resolve().parent
RUN_DIRS = (".fact-graph/runs",)  # 默认相对启动目录

STATIC_EXT = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json",
    ".md": "text/markdown; charset=utf-8",
    ".toml": "text/plain; charset=utf-8",
}


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def run_summary(run_dir: Path) -> dict | None:
    board = load_json(run_dir / "board.json")
    if board is None:
        return None
    intents = board.get("intents", [])
    status_mtimes = run_dir / "status.json"
    updated = status_mtimes.stat().st_mtime if status_mtimes.is_file() else None
    return {
        "id": run_dir.name,
        "title": board.get("title", run_dir.name),
        "status": board.get("status", "unknown"),
        "facts": len(board.get("facts", [])),
        "intents_concluded": sum(1 for i in intents if i.get("to")),
        "intents_open": sum(1 for i in intents if not i.get("to")),
        "hints": len(board.get("hints", [])),
        "reason_rounds": board.get("reason_rounds", 0),
        "updated_at": board.get("completed_at") or board.get("created_at"),
        "file_mtime": updated if updated is not None else run_dir.stat().st_mtime,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "fact-graph-ui/0.1"
    roots: list[Path] = []

    # ---- 工具 ------------------------------------------------------------

    def log_message(self, fmt, *args):  # 精简访问日志
        return

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, data) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _send_text(self, code: int, text: str, ctype: str) -> None:
        self._send(code, text.encode("utf-8"), ctype)

    def _send_error_json(self, code: int, message: str) -> None:
        self._send_json(code, {"detail": message})

    def _find_run(self, name: str) -> Path | None:
        if "/" in name or "\\" in name or name in (".", ".."):
            return None
        for root in self.roots:
            candidate = root / name
            if candidate.is_dir() and (candidate / "board.json").is_file():
                return candidate
        return None

    # ---- 路由 ------------------------------------------------------------

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            return self._serve_static("index.html")
        if path.startswith("/api/"):
            return self._api_runs(path)
        # 其余一律按 UI_DIR 下的静态文件尝试 (vendor/、favicon.svg 等)
        return self._serve_static(path.lstrip("/"))

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/runs/"):
            return self._api_hint(path)
        return self._send_error_json(404, "not found")

    def _serve_static(self, rel: str) -> None:
        target = (UI_DIR / rel).resolve()
        try:
            target.relative_to(UI_DIR)
        except ValueError:
            return self._send_error_json(403, "forbidden")
        if not target.is_file():
            return self._send_error_json(404, "not found")
        ctype = STATIC_EXT.get(target.suffix, mimetypes.guess_type(str(target))[0]
                               or "application/octet-stream")
        self._send(200, target.read_bytes(), ctype)

    def _api_runs(self, path: str) -> None:
        if path == "/api/runs":
            summaries = []
            for root in self.roots:
                if not root.is_dir():
                    continue
                for child in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
                    summary = run_summary(child)
                    if summary is not None:
                        summaries.append(summary)
            return self._send_json(200, summaries)

        parts = path.split("/")  # ['', 'api', 'runs', <name>, <sub>]
        if len(parts) == 5 and parts[2] == "runs" and parts[4] == "board":
            run_dir = self._find_run(parts[3])
            if run_dir is None:
                return self._send_error_json(404, "run not found")
            board = load_json(run_dir / "board.json")
            return self._send_json(200, board) if board is not None \
                else self._send_error_json(500, "board.json unreadable")

        if len(parts) == 5 and parts[2] == "runs" and parts[4] in ("events", "result", "config"):
            run_dir = self._find_run(parts[3])
            if run_dir is None:
                return self._send_error_json(404, "run not found")
            if parts[4] == "events":
                text = (run_dir / "events.jsonl").read_text(encoding="utf-8", errors="replace") \
                    if (run_dir / "events.jsonl").is_file() else ""
                return self._send_text(200, text, "text/plain; charset=utf-8")
            if parts[4] == "result":
                path = run_dir / "RESULT.md"
                return self._send_text(200, path.read_text(encoding="utf-8", errors="replace")
                                       if path.is_file() else "", "text/markdown; charset=utf-8")
            path = run_dir / "config.toml"
            return self._send_text(200, path.read_text(encoding="utf-8", errors="replace")
                                   if path.is_file() else "", "text/plain; charset=utf-8")

        return self._send_error_json(404, "not found")

    def _api_hint(self, path: str) -> None:
        parts = path.split("/")  # ['', 'api', 'runs', <name>, 'hint']
        if len(parts) != 5 or parts[2] != "runs" or parts[4] != "hint":
            return self._send_error_json(404, "not found")
        run_dir = self._find_run(parts[3])
        if run_dir is None:
            return self._send_error_json(404, "run not found")
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._send_error_json(400, "body 不是合法 JSON")
        content = str(payload.get("content") or "").strip()
        if not content:
            return self._send_error_json(400, "content 不能为空")
        creator = str(payload.get("creator") or "human").strip() or "human"
        record = {"content": content, "creator": creator, "ts": datetime.now().isoformat(timespec="seconds")}
        # 单行原子追加; 单条记录远小于管道缓冲, 无跨行撕裂风险
        with open(run_dir / "inbox.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return self._send_json(200, {"ok": True, "hint": record})


def main() -> int:
    parser = argparse.ArgumentParser(description="fact-graph UI 服务器")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8720)
    parser.add_argument("--root", action="append",
                        help="runs 根目录(可多次); 默认 ./.fact-graph/runs")
    parser.add_argument("--open", action="store_true", help="启动后打开浏览器")
    args = parser.parse_args()

    Handler.roots = [Path(root).expanduser() for root in (args.root or RUN_DIRS)]
    for root in Handler.roots:
        root.mkdir(parents=True, exist_ok=True)
        print(f"runs 根目录: {root}")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"fact-graph UI: {url}")
    print("静态模式无需服务器; 直接打开 ui/index.html 即可(支持拖拽导入)。")
    if args.open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
