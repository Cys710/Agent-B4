from __future__ import annotations

import argparse
import json
import mimetypes
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingTCPServer
from urllib.parse import parse_qs, unquote, urlparse

from b3_tool_layer import get_tools_schema
from b4.evaluation import run_batch_tool_call_eval
from b4.service import compare_schema_passing_methods, generate_ai_message
from common.io_utils import append_jsonl, read_json, write_json
from common.logging_utils import now_iso


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = PROJECT_ROOT / "webui" / "B4"
DEFAULT_BATCH_CONFIG = PROJECT_ROOT / "data" / "messages" / "b4_5batch_tool_call_eval_sample.json"
DEFAULT_MESSAGES = PROJECT_ROOT / "data" / "messages" / "b4_2messages_with_multi_tool.json"
DEFAULT_MODEL_CONFIG = PROJECT_ROOT / "configs" / "model.yaml"
DEFAULT_EXPERT_CONFIG = PROJECT_ROOT / "configs" / "expert_models.yaml"
DEFAULT_TOOLS_CONFIG = PROJECT_ROOT / "configs" / "tools.yaml"
DEFAULT_SCHEMA_DIR = PROJECT_ROOT / "outputs" / "B4_webui" / "schema"
DEFAULT_TOOLS_SCHEMA = PROJECT_ROOT / "data" / "messages" / "tools_schema.json"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "outputs" / "B4_GUI"
DEFAULT_SCENARIO_OUTPUT_DIR = DEFAULT_OUTPUT_DIR / "part1"
MAX_BODY_BYTES = 4_000_000


def _public_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return str(path)


def _resolve_project_path(value: str | None, default: Path | None = None) -> Path:
    if not value:
        if default is None:
            raise ValueError("path is required")
        return default.resolve()
    normalized = str(value).strip().strip('"').strip("'").replace("\\", "/")
    path = Path(normalized)
    candidates = [path] if path.is_absolute() else [PROJECT_ROOT / path, PROJECT_ROOT / "code" / path]
    for candidate in candidates:
        resolved = candidate.resolve()
        try:
            resolved.relative_to(PROJECT_ROOT.resolve())
            return resolved
        except ValueError:
            continue
    raise ValueError(f"path must stay within project root: {value}")


def _resolve_batch_config_relative_path(value: str, source_batch_path: Path) -> Path:
    """Resolve paths inside a batch config using the original config file as base."""
    normalized = str(value).strip().strip('"').strip("'").replace("\\", "/")
    path = Path(normalized)
    if path.is_absolute():
        resolved = path.resolve()
    else:
        candidates = [
            (source_batch_path.resolve().parent / path).resolve(),
            (PROJECT_ROOT / path).resolve(),
        ]
        resolved = next((candidate for candidate in candidates if candidate.exists()), candidates[0])
    try:
        resolved.relative_to(PROJECT_ROOT.resolve())
    except ValueError as exc:
        raise ValueError(f"batch model_config must stay within project root: {value}") from exc
    return resolved


def _normalize_batch_model_config_paths(batch_config: dict, source_batch_path: Path) -> dict:
    """Make model_config paths stable after WebUI writes an edited batch config to outputs."""
    normalized = json.loads(json.dumps(batch_config, ensure_ascii=False))

    top_level_model_config = normalized.get("model_config")
    if isinstance(top_level_model_config, str) and top_level_model_config.strip():
        normalized["model_config"] = str(_resolve_batch_config_relative_path(top_level_model_config, source_batch_path))

    models = normalized.get("models", [])
    if isinstance(models, list):
        for model_entry in models:
            if not isinstance(model_entry, dict):
                continue
            model_config = model_entry.get("model_config")
            if isinstance(model_config, str) and model_config.strip():
                model_entry["model_config"] = str(_resolve_batch_config_relative_path(model_config, source_batch_path))
    return normalized


def _read_jsonl(path: Path, limit: int = 20) -> list[dict]:
    if not path.is_file():
        return []
    records = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                records.append(json.loads(stripped))
            except json.JSONDecodeError:
                continue
    return records[-limit:]


def _history_path() -> Path:
    return DEFAULT_OUTPUT_DIR / "b4_webui_run_log.jsonl"


def _append_history(record: dict) -> None:
    try:
        append_jsonl(record, _history_path())
    except Exception as exc:
        sys.stderr.write(f"[B4WebUI] history write failed: {type(exc).__name__}: {exc}\n")


def _batch_summary(config: dict) -> dict:
    models = config.get("models", [])
    cases = config.get("cases", [])
    return {
        "description": config.get("description", ""),
        "mode": config.get("mode", "prompt_json"),
        "planning": config.get("planning", "none"),
        "schema_passing": config.get("schema_passing", "prompt_injection"),
        "exact_args": bool(config.get("exact_args", False)),
        "model_count": len(models) if isinstance(models, list) else 0,
        "case_count": len(cases) if isinstance(cases, list) else 0,
        "models": [
            {
                "id": item.get("id"),
                "label": item.get("label", item.get("id")),
                "series": item.get("series", ""),
            }
            for item in models
            if isinstance(item, dict)
        ],
    }


def _load_tools_schema(payload: dict, output_dir: Path) -> tuple[list[dict], dict]:
    schema_mode = payload.get("schema_mode", "generate")
    if schema_mode == "file":
        schema_path = _resolve_project_path(payload.get("tools_schema_path"))
        schema = read_json(schema_path)
        if not isinstance(schema, list):
            raise ValueError("tools schema file must contain a JSON array")
        return schema, {"source": "file", "path": _public_path(schema_path), "tool_count": len(schema)}

    tools_config = _resolve_project_path(payload.get("tools_config_path"), DEFAULT_TOOLS_CONFIG)
    toolset = str(payload.get("toolset", "extended_tools"))
    schema_source = str(payload.get("schema_source", "config"))
    schema_dir = output_dir / "schema"
    schema = get_tools_schema(str(tools_config), toolset, str(schema_dir), schema_source=schema_source)
    return (
        schema,
        {
            "source": "generated",
            "tools_config": _public_path(tools_config),
            "toolset": toolset,
            "schema_source": schema_source,
            "path": _public_path(schema_dir / "tools_schema.json"),
            "tool_count": len(schema),
        },
    )


def _load_messages(payload: dict) -> tuple[list[dict], dict]:
    raw_messages = payload.get("messages")
    if raw_messages is not None:
        if not isinstance(raw_messages, list):
            raise ValueError("messages must be a JSON array")
        return raw_messages, {"source": "inline", "path": None, "message_count": len(raw_messages)}
    messages_path = _resolve_project_path(payload.get("messages_path"), DEFAULT_MESSAGES)
    messages = read_json(messages_path)
    if not isinstance(messages, list):
        raise ValueError("messages file must contain a JSON array")
    return messages, {"source": "file", "path": _public_path(messages_path), "message_count": len(messages)}


def _latest_report_path() -> Path:
    return DEFAULT_OUTPUT_DIR / "batch_tool_call_eval_report.json"


class B4WebUIHandler(BaseHTTPRequestHandler):
    server_version = "B4WebUI/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def _send_json(self, payload: dict | list, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status: HTTPStatus, message: str, error_type: str = "RequestError") -> None:
        self._send_json({"error": {"type": error_type, "message": message}}, status)

    def _read_request_json(self) -> dict:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length > MAX_BODY_BYTES:
            raise ValueError("request body is too large")
        body = self.rfile.read(length)
        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON body: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path == "/api/health":
                self._send_json({"status": "ok", "output_dir": _public_path(DEFAULT_OUTPUT_DIR)})
            elif path == "/api/defaults":
                config = read_json(DEFAULT_BATCH_CONFIG)
                self._send_json(
                    {
                        "batch_config_path": _public_path(DEFAULT_BATCH_CONFIG),
                        "messages_path": _public_path(DEFAULT_MESSAGES),
                        "model_config_path": _public_path(DEFAULT_MODEL_CONFIG),
                        "expert_config_path": _public_path(DEFAULT_EXPERT_CONFIG),
                        "tools_config_path": _public_path(DEFAULT_TOOLS_CONFIG),
                        "tools_schema_path": _public_path(DEFAULT_TOOLS_SCHEMA),
                        "schema_mode": "file",
                        "output_dir": _public_path(DEFAULT_SCENARIO_OUTPUT_DIR),
                        "schema_dir": _public_path(DEFAULT_SCHEMA_DIR),
                        "batch_summary": _batch_summary(config),
                        "history": _read_jsonl(_history_path()),
                    }
                )
            elif path == "/api/batch_config":
                config_path = _resolve_project_path(query.get("path", [""])[0], DEFAULT_BATCH_CONFIG)
                config = read_json(config_path)
                self._send_json({"path": _public_path(config_path), "summary": _batch_summary(config), "config": config})
            elif path == "/api/messages":
                messages_path = _resolve_project_path(query.get("path", [""])[0], DEFAULT_MESSAGES)
                messages = read_json(messages_path)
                if not isinstance(messages, list):
                    raise ValueError("messages file must contain a JSON array")
                self._send_json({"path": _public_path(messages_path), "messages": messages, "message_count": len(messages)})
            elif path == "/api/report":
                report_path = _resolve_project_path(query.get("path", [""])[0], _latest_report_path())
                report = read_json(report_path)
                self._send_json({"path": _public_path(report_path), "report": report})
            elif path == "/api/history":
                self._send_json({"records": _read_jsonl(_history_path())})
            elif path.startswith("/api/"):
                self._send_error_json(HTTPStatus.NOT_FOUND, f"unknown API path: {path}")
            else:
                self._serve_static(path)
        except FileNotFoundError as exc:
            self._send_error_json(HTTPStatus.NOT_FOUND, str(exc), type(exc).__name__)
        except Exception as exc:
            self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc), type(exc).__name__)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/clear_history":
            try:
                DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
                count = len(_read_jsonl(_history_path(), limit=1_000_000))
                _history_path().write_text("", encoding="utf-8")
                self._send_json({"cleared_records": count, "records": []})
            except Exception as exc:
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc), type(exc).__name__)
            return
        if parsed.path == "/api/run_generate":
            self._handle_generate()
            return
        if parsed.path == "/api/compare_schema":
            self._handle_compare_schema()
            return
        if parsed.path != "/api/run_batch":
            self._send_error_json(HTTPStatus.NOT_FOUND, f"unknown API path: {parsed.path}")
            return
        try:
            payload = self._read_request_json()
            output_dir = _resolve_project_path(payload.get("outdir"), DEFAULT_OUTPUT_DIR)
            output_dir.mkdir(parents=True, exist_ok=True)
            batch_config = payload.get("batch_config")
            if batch_config is not None:
                if not isinstance(batch_config, dict):
                    raise ValueError("batch_config must be a JSON object")
                source_batch_path = _resolve_project_path(payload.get("batch_config_path"), DEFAULT_BATCH_CONFIG)
                batch_config = _normalize_batch_model_config_paths(batch_config, source_batch_path)
                batch_config_path = output_dir / "webui_batch_config.json"
                write_json(batch_config, batch_config_path)
            else:
                batch_config_path = _resolve_project_path(payload.get("batch_config_path"), DEFAULT_BATCH_CONFIG)
            tools_schema, schema_meta = _load_tools_schema(payload, output_dir)
            mode_override = payload.get("mode") or None
            planning_override = payload.get("planning") or None
            schema_passing_override = payload.get("schema_passing") or None
            report = run_batch_tool_call_eval(
                str(batch_config_path),
                tools_schema,
                str(output_dir),
                mode_override=mode_override,
                planning_override=planning_override,
                schema_passing_override=schema_passing_override,
            )
            report_path = output_dir / "batch_tool_call_eval_report.json"
            summary_path = output_dir / "batch_tool_call_eval_summary.md"
            log_record = {
                "timestamp": now_iso(),
                "status": "success",
                "mode": mode_override or report["models"][0].get("mode", "-") if report.get("models") else mode_override,
                "batch_config_path": _public_path(batch_config_path),
                "report_path": _public_path(report_path),
                "summary_path": _public_path(summary_path),
                "schema": schema_meta,
                "model_count": report.get("total_models"),
                "case_count": report.get("total_cases"),
                "source": "webui",
            }
            _append_history(log_record)
            self._send_json(
                {
                    "report": report,
                    "report_path": _public_path(report_path),
                    "summary_path": _public_path(summary_path),
                    "schema": schema_meta,
                    "history": _read_jsonl(_history_path()),
                }
            )
        except Exception as exc:
            _append_history(
                {
                    "timestamp": now_iso(),
                    "status": "error",
                    "error": {"type": type(exc).__name__, "message": str(exc)},
                    "source": "webui",
                }
            )
            self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc), type(exc).__name__)

    def _handle_generate(self) -> None:
        try:
            payload = self._read_request_json()
            output_dir = _resolve_project_path(payload.get("outdir"), DEFAULT_OUTPUT_DIR) / "single"
            output_dir.mkdir(parents=True, exist_ok=True)
            tools_schema, schema_meta = _load_tools_schema(payload, output_dir)
            messages, message_meta = _load_messages(payload)
            model_config = _resolve_project_path(payload.get("model_config_path"), DEFAULT_MODEL_CONFIG)
            tools_config = _resolve_project_path(payload.get("tools_config_path"), DEFAULT_TOOLS_CONFIG)
            expert_config = _resolve_project_path(payload.get("expert_config_path"), DEFAULT_EXPERT_CONFIG)
            planning = str(payload.get("planning", "none"))
            result = generate_ai_message(
                str(model_config),
                messages,
                tools_schema,
                str(payload.get("mode", "mock")),
                str(output_dir),
                artifact_stem=planning,
                planning=planning,
                schema_passing=str(payload.get("schema_passing", "prompt_injection")),
                tools_config=str(tools_config),
                toolset=str(payload.get("toolset", "extended_tools")),
                expert_config_path=str(expert_config),
            )
            raw_path = output_dir / f"{planning}_raw_model_output.json"
            ai_path = output_dir / f"{planning}_ai_message.json"
            plan_path = output_dir / f"{planning}_plan.json"
            _append_history(
                {
                    "timestamp": now_iso(),
                    "status": result["status"],
                    "mode": payload.get("mode", "mock"),
                    "operation": "plan_execute" if planning == "plan_and_execute" else "single_generate",
                    "raw_output_path": _public_path(raw_path),
                    "ai_message_path": _public_path(ai_path),
                    "source": "webui",
                }
            )
            self._send_json(
                {
                    "result": result,
                    "schema": schema_meta,
                    "messages": message_meta,
                    "raw_output_path": _public_path(raw_path),
                    "ai_message_path": _public_path(ai_path),
                    "plan_path": _public_path(plan_path) if plan_path.is_file() else None,
                    "history": _read_jsonl(_history_path()),
                }
            )
        except Exception as exc:
            _append_history({"timestamp": now_iso(), "status": "error", "operation": "generate", "error": {"type": type(exc).__name__, "message": str(exc)}, "source": "webui"})
            self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc), type(exc).__name__)

    def _handle_compare_schema(self) -> None:
        try:
            payload = self._read_request_json()
            output_dir = _resolve_project_path(payload.get("outdir"), DEFAULT_OUTPUT_DIR) / "schema_compare"
            output_dir.mkdir(parents=True, exist_ok=True)
            tools_schema, schema_meta = _load_tools_schema(payload, output_dir)
            messages, message_meta = _load_messages(payload)
            model_config = _resolve_project_path(payload.get("model_config_path"), DEFAULT_MODEL_CONFIG)
            report = compare_schema_passing_methods(
                str(model_config),
                messages,
                tools_schema,
                str(payload.get("mode", "mock")),
                str(output_dir),
                planning=str(payload.get("planning", "none")),
            )
            report_path = output_dir / "schema_passing_report.json"
            _append_history(
                {
                    "timestamp": now_iso(),
                    "status": "success",
                    "mode": payload.get("mode", "mock"),
                    "operation": "schema_compare",
                    "report_path": _public_path(report_path),
                    "source": "webui",
                }
            )
            self._send_json(
                {
                    "report": report,
                    "schema": schema_meta,
                    "messages": message_meta,
                    "report_path": _public_path(report_path),
                    "history": _read_jsonl(_history_path()),
                }
            )
        except Exception as exc:
            _append_history({"timestamp": now_iso(), "status": "error", "operation": "schema_compare", "error": {"type": type(exc).__name__, "message": str(exc)}, "source": "webui"})
            self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc), type(exc).__name__)

    def _serve_static(self, request_path: str) -> None:
        relative = unquote(request_path.lstrip("/"))
        if not relative:
            relative = "index.html"
        target = (WEB_ROOT / relative).resolve()
        try:
            target.relative_to(WEB_ROOT.resolve())
        except ValueError:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        body = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve the B4 Tool-Call Evaluation WebUI.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18088)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not (WEB_ROOT / "index.html").is_file():
        print(f"fatal: missing WebUI file: {WEB_ROOT / 'index.html'}", file=sys.stderr)
        return 1
    ThreadingTCPServer.allow_reuse_address = True
    with ThreadingTCPServer((args.host, args.port), B4WebUIHandler) as httpd:
        print(f"B4 WebUI: http://{args.host}:{args.port}/")
        print(f"Output directory: {DEFAULT_OUTPUT_DIR}")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nB4 WebUI stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
