from __future__ import annotations

from collections.abc import Callable
import fnmatch
import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
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
    "step4_bootstrap_runner",
    "stack_mapper",
    "graph_bootstrap_runner",
    "graph_path_analyst",
    "artifact_validator",
    "profiling_debugger",
    "artifact_renderer",
}

CODE_LOCATION_RE = re.compile(r"^[^:]+:\d+$")
SELF_CALL_RE = re.compile(r"\bself(?:\.\w+)?\s*\(")
MODULE_CALL_RE = re.compile(r"\b(?:module|layer)\s*\(")
CONSTRUCTOR_LINE_RE = re.compile(r"^\s*self\.\w+\s*=\s*[A-Za-z_][\w\.]*\(")
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
    tmp_path = path.parent / f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with tmp_path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def sanitize_log_token(value: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z._-]+", "_", value.strip())
    normalized = normalized.strip("._-")
    return normalized or "unnamed"


def child_run_logs_dir(workspace_dir: Path) -> Path:
    path = workspace_dir / "logs" / "wrapper_runs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def build_child_process_env(extra_env: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("PYTHONIOENCODING", "utf-8")
    if extra_env:
        env.update({str(key): str(value) for key, value in extra_env.items()})
    return env


def run_child_script_with_logs(
    *,
    script_path: Path,
    workspace_dir: Path,
    repo_root: Path,
    log_prefix: str,
    heartbeat_seconds: int = 30,
    extra_env: dict[str, str] | None = None,
    extra_args: list[str] | None = None,
    on_heartbeat: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    ensure_parent(script_path)
    logs_dir = child_run_logs_dir(workspace_dir)
    script_token = sanitize_log_token(log_prefix)
    combined_log_path = logs_dir / f"{script_token}.combined.log"
    metadata_path = logs_dir / f"{script_token}.meta.json"
    command = [sys.executable, "-u", str(script_path), "--workspace-dir", str(workspace_dir)]
    if extra_args:
        command.extend(str(item) for item in extra_args)
    env = build_child_process_env(extra_env)
    start_iso = now_iso()
    start_perf = time.perf_counter()
    output_line_count = 0
    heartbeat_count = 0
    last_output_at = time.monotonic()
    line_queue: queue.Queue[str | None] = queue.Queue()
    reader_finished = False

    with combined_log_path.open("w", encoding="utf-8", newline="\n") as log_handle:
        header = (
            f"[child-runner] start script={script_path.name} cwd={repo_root} "
            f"workspace={workspace_dir} platform={sys.platform} heartbeat_seconds={heartbeat_seconds}"
        )
        print(header, flush=True)
        log_handle.write(header + "\n")
        log_handle.flush()

        process = subprocess.Popen(
            command,
            cwd=repo_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )

        def _reader() -> None:
            try:
                assert process.stdout is not None
                for raw_line in process.stdout:
                    line_queue.put(raw_line.rstrip("\r\n"))
            finally:
                line_queue.put(None)

        reader = threading.Thread(target=_reader, name=f"{script_token}_reader", daemon=True)
        reader.start()

        while True:
            try:
                line = line_queue.get(timeout=heartbeat_seconds)
            except queue.Empty:
                if process.poll() is None:
                    heartbeat_count += 1
                    heartbeat_payload = {
                        "script_name": script_path.name,
                        "elapsed_seconds": round(time.perf_counter() - start_perf, 6),
                        "idle_seconds": round(time.monotonic() - last_output_at, 6),
                        "output_line_count": output_line_count,
                        "heartbeat_count": heartbeat_count,
                        "combined_log_path": str(combined_log_path),
                        "metadata_path": str(metadata_path),
                    }
                    heartbeat = (
                        f"[child-runner] heartbeat script={script_path.name} "
                        f"elapsed_seconds={heartbeat_payload['elapsed_seconds']:.2f} "
                        f"idle_seconds={heartbeat_payload['idle_seconds']:.2f} "
                        f"output_line_count={output_line_count}"
                    )
                    print(heartbeat, flush=True)
                    log_handle.write(heartbeat + "\n")
                    log_handle.flush()
                    if on_heartbeat is not None:
                        on_heartbeat(heartbeat_payload)
                    continue
                if reader_finished and line_queue.empty():
                    break
                continue

            if line is None:
                reader_finished = True
                if process.poll() is not None and line_queue.empty():
                    break
                continue

            output_line_count += 1
            last_output_at = time.monotonic()
            print(line, flush=True)
            log_handle.write(line + "\n")
            log_handle.flush()

        return_code = process.wait()
        reader.join(timeout=1.0)
        duration_seconds = time.perf_counter() - start_perf
        footer = (
            f"[child-runner] done script={script_path.name} return_code={return_code} "
            f"duration_seconds={duration_seconds:.2f} output_line_count={output_line_count} "
            f"heartbeat_count={heartbeat_count}"
        )
        print(footer, flush=True)
        log_handle.write(footer + "\n")
        log_handle.flush()

    metadata = {
        "schema_version": "child_run_metadata_v1",
        "script_path": str(script_path),
        "workspace_dir": str(workspace_dir),
        "repo_root": str(repo_root),
        "command": command,
        "combined_log_path": str(combined_log_path),
        "start_at": start_iso,
        "end_at": now_iso(),
        "duration_seconds": round(duration_seconds, 6),
        "return_code": return_code,
        "heartbeat_seconds": heartbeat_seconds,
        "heartbeat_count": heartbeat_count,
        "output_line_count": output_line_count,
        "reader_finished": reader_finished,
        "platform": sys.platform,
        "is_windows": os.name == "nt",
    }
    dump_json(metadata_path, metadata)
    metadata["metadata_path"] = str(metadata_path)
    if return_code != 0:
        raise subprocess.CalledProcessError(return_code, command)
    return metadata


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


def normalize_substep(value: Any) -> str:
    normalized = str(value or "").strip().upper()
    return normalized if normalized in {"A", "B"} else ""


def effective_substep(state: dict[str, Any], step: int | None = None) -> str:
    current_step = int(step if step is not None else int(state.get("current_step", 0) or 0))
    raw_substep = normalize_substep(state.get("current_substep", ""))
    if current_step not in {4, 5}:
        return raw_substep
    if raw_substep in {"A", "B"}:
        return raw_substep
    flags = state.get("flags", {})
    artifacts = state.get("artifacts", {})
    if current_step == 4:
        step4_bootstrap_result_path = Path(str(artifacts.get("step4_bootstrap_result_path", "")).strip())
        if bool(flags.get("external_span_mapping_built")):
            return "B"
        if step4_bootstrap_result_path.exists():
            return "B"
        return "A"

    graph_bootstrap_result_path = Path(str(artifacts.get("graph_bootstrap_result_path", "")).strip())
    if bool(flags.get("graph_span_alignment_built")):
        return "B"
    if graph_bootstrap_result_path.exists():
        return "B"
    return "A"


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


def extract_graph_alignment_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        rows = payload.get("rows")
        if isinstance(rows, list):
            return [item for item in rows if isinstance(item, dict)]
        items = payload.get("items")
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def collect_graph_mapping_target_ids(payload: dict[str, Any]) -> set[str]:
    rows = payload.get("rows", [])
    if not isinstance(rows, list):
        return set()
    return {
        str(row.get("span_id", "")).strip()
        for row in rows
        if isinstance(row, dict) and str(row.get("span_id", "")).strip()
    }


def collect_graph_operator_span_map(payload: dict[str, Any]) -> dict[str, str]:
    rows = payload.get("rows", [])
    if not isinstance(rows, list):
        return {}
    operator_span_map: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        graph_operator_span_id = str(row.get("graph_operator_span_id", "")).strip()
        span_id = str(row.get("span_id", "")).strip()
        if graph_operator_span_id and span_id:
            operator_span_map[graph_operator_span_id] = span_id
    return operator_span_map


def collect_graph_operator_span_ids(payload: dict[str, Any]) -> set[str]:
    return set(collect_graph_operator_span_map(payload).keys())


def read_repo_source_line(repo_root: Path, code_location: str) -> str:
    path_text, _, line_text = str(code_location).rpartition(":")
    if not path_text or not line_text.isdigit():
        return ""
    line_number = int(line_text)
    if line_number <= 0:
        return ""
    file_path = repo_root / path_text
    if not file_path.exists():
        return ""
    try:
        lines = file_path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    if 1 <= line_number <= len(lines):
        return lines[line_number - 1].strip()
    return ""


def graph_source_line_violation(code_location: str, repo_root: Path) -> str:
    source_line = read_repo_source_line(repo_root, code_location)
    stripped = source_line.strip()
    if not stripped:
        return ""
    if ".replay(" in stripped:
        return "graph_replay_entry"
    if CONSTRUCTOR_LINE_RE.match(stripped):
        return "constructor_line"
    if SELF_CALL_RE.search(stripped) or MODULE_CALL_RE.search(stripped):
        return "module_call_anchor"
    return ""


def validate_graph_alignment_row(
    row: dict[str, Any],
    operator_span_map: dict[str, str],
    formal_graph_target_ids: set[str],
    *,
    require_final_operator_call: bool,
) -> list[str]:
    violations: list[str] = []
    span_id = str(row.get("span_id", "")).strip()
    graph_operator_span_id = str(row.get("graph_operator_span_id", "")).strip()
    location_kind = str(row.get("location_kind", "")).strip()
    operator_evidence_kind = str(row.get("operator_evidence_kind", "")).strip()
    requires_further_drilldown = row.get("requires_further_drilldown")
    code_location = str(row.get("code_location", "")).strip()
    if not span_id:
        violations.append("span_id missing")
    elif formal_graph_target_ids and span_id not in formal_graph_target_ids:
        violations.append(f"span_id out_of_scope={span_id}")
    if not graph_operator_span_id:
        violations.append("graph_operator_span_id missing")
    else:
        expected_span_id = operator_span_map.get(graph_operator_span_id)
        if not expected_span_id:
            violations.append(f"graph_operator_span_id unresolved={graph_operator_span_id}")
        elif span_id and span_id != expected_span_id:
            violations.append(
                f"span_id/operator_mismatch span_id={span_id} expected={expected_span_id} graph_operator_span_id={graph_operator_span_id}"
            )
    if not operator_evidence_kind:
        violations.append("operator_evidence_kind missing")
    if require_final_operator_call:
        if location_kind != "operator_call":
            violations.append(f"location_kind={location_kind or '<missing>'}")
        if requires_further_drilldown is not False:
            violations.append(f"requires_further_drilldown={requires_further_drilldown!r}")
        if code_location and not validate_code_location(code_location):
            violations.append(f"invalid_code_location={code_location}")
    return violations


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
