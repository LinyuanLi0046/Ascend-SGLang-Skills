from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


STEP_DEFINITIONS = {
    1: "INPUT_DISCOVERY_AND_SLICING",
    2: "TIMELINE_INDEX_BUILD",
    3: "TIMELINE_SEMANTIC_ANALYSIS",
    4: "NON_GRAPH_STACK_MAPPING",
    5: "GRAPH_PATH_RECONSTRUCTION",
    6: "FINAL_MAPPING_AND_OUTPUT_RENDER",
    7: "ARTIFACT_VALIDATION",
}

AGENT_NAMES = {
    "profiling_preprocessor",
    "timeline_analyst",
    "stack_mapper",
    "graph_path_analyst",
    "artifact_validator",
    "profiling_debugger",
    "artifact_renderer",
}

CODE_LOCATION_RE = re.compile(r"^[^:]+:\d+$")
TEMP_SCRIPT_PATTERNS = ("_*.py", "tmp*.py", "debug_*.py", "temp*.py")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_text(path: Path, content: str) -> None:
    ensure_parent(path)
    path.write_text(content, encoding="utf-8", newline="\n")


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def dump_json(path: Path, payload: dict[str, Any]) -> None:
    ensure_parent(path)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def _consume_until_named_array(handle, field_names: tuple[str, ...], missing_message: str) -> str:
    buffer = ""
    while True:
        chunk = handle.read(65536)
        if not chunk:
            raise ValueError(missing_message)
        buffer += chunk
        for field_name in field_names:
            match = re.search(rf'"{re.escape(field_name)}"\s*:\s*\[', buffer)
            if match:
                return buffer[match.end() :]
        if len(buffer) > 1024:
            buffer = buffer[-1024:]


def iter_classified_streams(path: Path) -> Iterator[dict[str, Any]]:
    decoder = json.JSONDecoder()
    with path.open("r", encoding="utf-8") as handle:
        buffer = _consume_until_named_array(handle, ("streams",), "classified_spans.json 缺少 streams 数组。")
        stream_index = 0
        while True:
            stripped = buffer.lstrip()
            while not stripped:
                chunk = handle.read(65536)
                if not chunk:
                    raise ValueError("classified_spans.json 的 streams 数组未正常结束。")
                buffer += chunk
                stripped = buffer.lstrip()
            consumed_prefix = len(buffer) - len(stripped)
            buffer = stripped
            if buffer[0] == "]":
                return
            if buffer[0] == ",":
                buffer = buffer[1:]
                continue
            while True:
                try:
                    stream_payload, end_index = decoder.raw_decode(buffer)
                    break
                except json.JSONDecodeError:
                    chunk = handle.read(65536)
                    if not chunk:
                        raise ValueError(
                            f"classified_spans.json 的第 {stream_index} 个 stream 对象解析失败或文件不完整。"
                        ) from None
                    buffer += chunk
            if not isinstance(stream_payload, dict):
                raise ValueError(f"classified_spans.json 的第 {stream_index} 个 stream 不是对象。")
            yield stream_payload
            stream_index += 1
            buffer = buffer[end_index:]


def iter_trace_events(path: Path) -> Iterator[dict[str, Any]]:
    decoder = json.JSONDecoder()
    with path.open("r", encoding="utf-8") as handle:
        first_non_ws = ""
        while True:
            chunk = handle.read(4096)
            if not chunk:
                return
            for char in chunk:
                if not char.isspace():
                    first_non_ws = char
                    break
            if first_non_ws:
                break
        handle.seek(0)
        if first_non_ws == "[":
            buffer = "["
            event_index = 0
        else:
            buffer = _consume_until_named_array(handle, ("traceEvents", "events"), "trace json 缺少 traceEvents/events 数组。")
            event_index = 0
        while True:
            stripped = buffer.lstrip()
            while not stripped:
                chunk = handle.read(65536)
                if not chunk:
                    raise ValueError("trace 事件数组未正常结束。")
                buffer += chunk
                stripped = buffer.lstrip()
            buffer = stripped
            if buffer[0] == "[":
                buffer = buffer[1:]
                continue
            if buffer[0] == "]":
                return
            if buffer[0] == ",":
                buffer = buffer[1:]
                continue
            while True:
                try:
                    event_payload, end_index = decoder.raw_decode(buffer)
                    break
                except json.JSONDecodeError:
                    chunk = handle.read(65536)
                    if not chunk:
                        raise ValueError(f"trace 第 {event_index} 个事件解析失败或文件不完整。") from None
                    buffer += chunk
            if not isinstance(event_payload, dict):
                raise ValueError(f"trace 第 {event_index} 个事件不是对象。")
            yield event_payload
            event_index += 1
            buffer = buffer[end_index:]


def compute_sha256(path: Path) -> str:
    if not path.exists():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _to_path(value: str | Path) -> Path:
    return value if isinstance(value, Path) else Path(str(value))


def collect_existing_file_hashes(paths: dict[str, str | Path]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for label, raw_path in paths.items():
        path = _to_path(raw_path)
        if path.exists() and path.is_file():
            hashes[label] = compute_sha256(path)
    return hashes


def list_workspace_temp_scripts(workspace_dir: Path) -> list[str]:
    results: list[str] = []
    for path in workspace_dir.rglob("*.py"):
        if not path.is_file():
            continue
        if any(fnmatch.fnmatch(path.name.lower(), pattern) for pattern in TEMP_SCRIPT_PATTERNS):
            results.append(str(path))
    return sorted(set(results))


def dispatch_completion_marker_path(workspace_dir: Path, agent_name: str) -> Path:
    if agent_name not in AGENT_NAMES:
        raise ValueError(f"未知 agent: {agent_name}")
    return workspace_dir / "audit" / f"subagent_completion_{agent_name}.json"


def provenance_manifest_path(workspace_dir: Path) -> Path:
    return workspace_dir / "audit" / "workspace_provenance.json"


def load_provenance_manifest(workspace_dir: Path) -> dict[str, Any]:
    path = provenance_manifest_path(workspace_dir)
    if not path.exists():
        return {
            "schema_version": "workspace_provenance_v1",
            "artifacts": {},
            "history": [],
        }
    payload = load_json(path)
    if not isinstance(payload, dict):
        raise ValueError(f"workspace provenance 非法: {path}")
    payload.setdefault("schema_version", "workspace_provenance_v1")
    payload.setdefault("artifacts", {})
    payload.setdefault("history", [])
    return payload


def save_provenance_manifest(workspace_dir: Path, payload: dict[str, Any]) -> Path:
    path = provenance_manifest_path(workspace_dir)
    dump_json(path, payload)
    return path


def document_hashes(workspace_dir: Path) -> dict[str, str]:
    return {
        "task_plan.md": compute_sha256(workspace_dir / "task_plan.md"),
        "findings.md": compute_sha256(workspace_dir / "findings.md"),
        "progress.md": compute_sha256(workspace_dir / "progress.md"),
    }


def load_state(workspace_dir: Path) -> dict[str, Any]:
    state_path = workspace_dir / "state.json"
    if not state_path.exists():
        raise FileNotFoundError(f"缺少状态文件: {state_path}")
    return load_json(state_path)


def resolve_artifact_path(
    workspace_dir: Path,
    state: dict[str, Any],
    artifact_key: str,
    fallback_relative_path: str,
) -> Path:
    raw_value = str(state.get("artifacts", {}).get(artifact_key, "") or "").strip()
    if raw_value:
        return Path(raw_value)
    fallback_path = workspace_dir / fallback_relative_path
    if fallback_path.exists():
        state.setdefault("artifacts", {})[artifact_key] = str(fallback_path)
        return fallback_path
    return Path(raw_value) if raw_value else fallback_path


def save_state(workspace_dir: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = now_iso()
    dump_json(workspace_dir / "state.json", state)


def required_step(step: int) -> str:
    if step not in STEP_DEFINITIONS:
        raise ValueError(f"不支持的 step: {step}")
    return STEP_DEFINITIONS[step]


def split_assignment(value: str) -> tuple[str, str]:
    if "=" not in value:
        raise ValueError(f"参数必须形如 key=value: {value}")
    key, raw_value = value.split("=", 1)
    key = key.strip()
    if not key:
        raise ValueError(f"参数键不能为空: {value}")
    return key, raw_value.strip()


def parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y"}:
        return True
    if normalized in {"0", "false", "no", "n"}:
        return False
    raise ValueError(f"无法解析布尔值: {value}")


def assert_file_exists(path: Path, label: str) -> None:
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"{label} 不存在: {path}")


def append_progress(workspace_dir: Path, line: str) -> None:
    progress_path = workspace_dir / "progress.md"
    content = read_text(progress_path).rstrip() + f"\n- {now_iso()} {line}\n"
    write_text(progress_path, content)


def append_findings(workspace_dir: Path, section: str, bullet: str) -> None:
    findings_path = workspace_dir / "findings.md"
    content = read_text(findings_path)
    marker = f"## {section}"
    if marker not in content:
        content = content.rstrip() + f"\n\n## {section}\n\n- {bullet}\n"
    else:
        content = content.replace(marker, f"{marker}\n\n- {bullet}", 1)
    write_text(findings_path, content)


def validate_code_location(code_location: str) -> bool:
    return bool(CODE_LOCATION_RE.match(code_location))


def normalize_repo_relative_path(raw_path: str, repo_root: str | Path) -> str:
    normalized = raw_path.replace("\\", "/")
    repo_root_path = repo_root if isinstance(repo_root, Path) else Path(repo_root)
    repo_name = repo_root_path.name
    marker = f"/{repo_name}/"
    if marker in normalized:
        return normalized.split(marker, 1)[1]
    if normalized.startswith(f"{repo_name}/"):
        return normalized[len(repo_name) + 1 :]
    return normalized.lstrip("/")


def write_error_context(workspace_dir: Path, payload: dict[str, Any]) -> Path:
    error_path = workspace_dir / "input" / "error_context.json"
    dump_json(error_path, payload)
    return error_path


def load_agent_index(workspace_dir: Path) -> list[dict[str, Any]]:
    index_path = workspace_dir / "logs" / "agent_calls" / "index.jsonl"
    if not index_path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with index_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def write_finalize_audit_record(workspace_dir: Path, agent_name: str, payload: dict[str, Any]) -> Path:
    record_path = workspace_dir / "audit" / f"finalize_{agent_name}_{now_iso().replace(':', '-')}.json"
    dump_json(record_path, payload)
    return record_path


def record_agent_status(
    workspace_dir: Path,
    agent_name: str,
    status: str,
    query_snapshot: str,
    output_path: str,
) -> None:
    if agent_name not in AGENT_NAMES:
        raise ValueError(f"未知 agent: {agent_name}")
    state = load_state(workspace_dir)
    agents = state.setdefault("agents", {})
    slot = agents.setdefault(agent_name, {})
    slot["last_status"] = status
    slot["last_called_at"] = now_iso()
    slot["last_query_snapshot"] = query_snapshot
    slot["last_output_path"] = output_path
    slot.setdefault("status_history", []).append(
        {
            "status": status,
            "at": slot["last_called_at"],
            "query_snapshot": query_snapshot,
            "output_path": output_path,
        }
    )
    save_state(workspace_dir, state)
