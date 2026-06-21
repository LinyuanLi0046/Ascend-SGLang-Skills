from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

from build_runtime_constraints import parse_launch_fields
from workflow_common import dump_json, load_state, save_state


LAUNCH_FILE_PATTERNS = ("run.sh", "launch*.sh", "start*.sh", "*.sh", "*.bash", "*.ps1", "*.cmd")
TIME_FILE_PATTERNS = ("*timestep*.txt", "*time*.txt", "*window*.txt", "*timestamp*.txt")
MODEL_MARKER_PATTERNS = (
    "config.json",
    "config*.json",
    "generation_config.json",
    "generation_config*.json",
    "quant_model_description.json",
    "quant_model_description*.json",
)
START_NS_RE = re.compile(r"start[^0-9]*(?P<value>\d+)\s*ns", re.IGNORECASE)
END_NS_RE = re.compile(r"end[^0-9]*(?P<value>\d+)\s*ns", re.IGNORECASE)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="在 Step1 前解析并补齐输入合同。")
    parser.add_argument("--workspace-dir", required=True)
    return parser


def _normalize_text(value: Any) -> str:
    return str(value or "").strip().strip('"').strip("'")


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    results: list[Path] = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        results.append(path)
    return results


def _path_candidates_from_inputs(inputs: dict[str, Any]) -> list[Path]:
    candidates: list[Path] = []
    for raw in inputs.get("supplemental_input_paths", []) or []:
        text = _normalize_text(raw)
        if text:
            candidates.append(Path(text))
    benchmark_path = _normalize_text(inputs.get("benchmark_result_path", ""))
    if benchmark_path:
        candidates.append(Path(benchmark_path))
    launch_file = _normalize_text(inputs.get("launch_command_file", ""))
    if launch_file:
        candidates.append(Path(launch_file))
    return _dedupe_paths(candidates)


def _root_candidates_from_paths(paths: list[Path]) -> list[Path]:
    roots: list[Path] = []
    for path in paths:
        roots.append(path if path.is_dir() else path.parent)
    return _dedupe_paths(roots)


def _safe_read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="ignore")


def _glob_files(roots: list[Path], patterns: tuple[str, ...]) -> list[Path]:
    matches: list[Path] = []
    for root in roots:
        if not root.exists() or not root.is_dir():
            continue
        for pattern in patterns:
            matches.extend(path for path in root.rglob(pattern) if path.is_file())
    return _dedupe_paths(sorted(matches))


def _score_launch_file(path: Path) -> int:
    score = 0
    text = _safe_read_text(path).lower()
    if "sglang.launch_server" in text:
        score += 5
    if "--model-path" in text:
        score += 4
    if "--speculative-draft-model-path" in text:
        score += 2
    if path.name.lower() == "run.sh":
        score += 3
    return score


def discover_launch_file(inputs: dict[str, Any]) -> tuple[str, str, list[dict[str, Any]]]:
    existing_file = _normalize_text(inputs.get("launch_command_file", ""))
    existing_text = _normalize_text(inputs.get("launch_command_text", ""))
    evidence: list[dict[str, Any]] = []
    if existing_file and Path(existing_file).exists():
        text = _safe_read_text(Path(existing_file))
        evidence.append({"source": "state.inputs.launch_command_file", "path": existing_file})
        return existing_file, text, evidence
    if existing_text:
        evidence.append({"source": "state.inputs.launch_command_text"})
        return "", existing_text, evidence
    roots = _root_candidates_from_paths(_path_candidates_from_inputs(inputs))
    candidates = _glob_files(_dedupe_paths(roots), LAUNCH_FILE_PATTERNS)
    if not candidates:
        return "", "", evidence
    ranked = sorted(candidates, key=lambda path: (-_score_launch_file(path), len(str(path))))
    best = ranked[0]
    evidence.append({"source": "supplemental_input_paths", "path": str(best), "score": _score_launch_file(best)})
    return str(best), _safe_read_text(best), evidence


def _parse_window_from_text(text: str) -> tuple[int, int]:
    start_match = START_NS_RE.search(text)
    end_match = END_NS_RE.search(text)
    if not start_match or not end_match:
        return 0, 0
    return int(start_match.group("value")), int(end_match.group("value"))


def discover_time_window(inputs: dict[str, Any]) -> tuple[int, int, list[dict[str, Any]]]:
    start_ns = int(inputs.get("window_start_ns", 0) or 0)
    end_ns = int(inputs.get("window_end_ns", 0) or 0)
    evidence: list[dict[str, Any]] = []
    if start_ns > 0 and end_ns > start_ns:
        evidence.append({"source": "state.inputs.window_start_ns/window_end_ns"})
        return start_ns, end_ns, evidence
    roots = _root_candidates_from_paths(_path_candidates_from_inputs(inputs))
    candidates = _glob_files(roots, TIME_FILE_PATTERNS)
    for candidate in candidates:
        parsed_start, parsed_end = _parse_window_from_text(_safe_read_text(candidate))
        if parsed_start > 0 and parsed_end > parsed_start:
            evidence.append({"source": "supplemental_input_paths", "path": str(candidate)})
            return parsed_start, parsed_end, evidence
    return start_ns, end_ns, evidence


def _has_model_markers(path: Path) -> tuple[bool, list[str], int]:
    if not path.exists() or not path.is_dir():
        return False, [], 0
    matches: list[str] = []
    score = 0
    for pattern in MODEL_MARKER_PATTERNS:
        for item in sorted(path.glob(pattern)):
            if not item.is_file():
                continue
            matches.append(item.name)
            lowered = item.name.lower()
            if lowered == "config.json":
                score += 6
            elif lowered.startswith("config"):
                score += 4
            elif lowered == "generation_config.json":
                score += 3
            elif lowered.startswith("generation_config"):
                score += 2
            elif lowered == "quant_model_description.json":
                score += 3
            elif lowered.startswith("quant_model_description"):
                score += 2
    matches = sorted(dict.fromkeys(matches))
    return bool(matches), matches, score


def _collect_model_dirs(roots: list[Path]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for root in roots:
        if not root.exists() or not root.is_dir():
            continue
        dirs = [root]
        dirs.extend(path for path in root.rglob("*") if path.is_dir() and len(path.relative_to(root).parts) <= 3)
        for directory in dirs:
            ok, markers, score = _has_model_markers(directory)
            if not ok:
                continue
            candidates.append(
                {
                    "path": directory,
                    "markers": markers,
                    "score": score,
                }
            )
    dedup: dict[str, dict[str, Any]] = {}
    for item in candidates:
        dedup[str(item["path"])] = item
    return sorted(dedup.values(), key=lambda item: (-int(item["score"]), len(str(item["path"]))))


def _resolve_model_dir(raw_model_path: str, model_dirs: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    if not raw_model_path:
        return "", {}
    path = Path(raw_model_path)
    if path.exists() and path.is_dir():
        ok, markers, score = _has_model_markers(path)
        if ok:
            return str(path), {"source": "launch_or_input_path", "path": str(path), "markers": markers, "score": score}
    basename = Path(raw_model_path.rstrip("/\\")).name.lower()
    for item in model_dirs:
        candidate = Path(item["path"])
        if candidate.name.lower() == basename:
            return str(candidate), {"source": "basename_match", "raw_path": raw_model_path, "path": str(candidate), "markers": item["markers"], "score": item["score"]}
    return "", {}


def _replace_token(raw_text: str, old_value: str, new_value: str) -> str:
    if not raw_text or not old_value or not new_value or old_value == new_value:
        return raw_text
    return raw_text.replace(old_value, new_value.replace("\\", "/"))


def resolve_inputs_for_workspace(workspace_dir: Path) -> dict[str, Any]:
    state = load_state(workspace_dir)
    inputs = state.setdefault("inputs", {})
    path_hints = _path_candidates_from_inputs(inputs)
    root_hints = _root_candidates_from_paths(path_hints)

    launch_file, launch_text, launch_evidence = discover_launch_file(inputs)
    if launch_file:
        inputs["launch_command_file"] = launch_file
    if launch_text:
        inputs["launch_command_text"] = launch_text
    parsed_launch_fields = parse_launch_fields(launch_text) if launch_text else {}

    window_start_ns, window_end_ns, time_evidence = discover_time_window(inputs)
    if window_start_ns > 0 and window_end_ns > window_start_ns:
        inputs["window_start_ns"] = window_start_ns
        inputs["window_end_ns"] = window_end_ns

    model_dirs = _collect_model_dirs(root_hints)
    existing_model_root = _normalize_text(inputs.get("model_root_path", ""))
    code_repo_path = _normalize_text(inputs.get("code_repo_path", ""))
    if existing_model_root and code_repo_path and Path(existing_model_root) == Path(code_repo_path):
        existing_model_root = ""
    model_path_from_launch = _normalize_text(parsed_launch_fields.get("model_path", ""))
    resolved_model_root, model_evidence = _resolve_model_dir(existing_model_root or model_path_from_launch, model_dirs)
    if not resolved_model_root and not existing_model_root and len(model_dirs) == 1:
        only = model_dirs[0]
        resolved_model_root = str(only["path"])
        model_evidence = {"source": "single_candidate", "path": resolved_model_root, "markers": only["markers"], "score": only["score"]}
    if resolved_model_root:
        inputs["model_root_path"] = resolved_model_root

    existing_draft_root = _normalize_text(inputs.get("draft_model_root_path", ""))
    draft_model_path_from_launch = _normalize_text(parsed_launch_fields.get("speculative_draft_model_path", ""))
    resolved_draft_root, draft_evidence = _resolve_model_dir(existing_draft_root or draft_model_path_from_launch, model_dirs)
    if resolved_draft_root:
        inputs["draft_model_root_path"] = resolved_draft_root

    normalized_launch_text = launch_text
    if resolved_model_root and model_path_from_launch:
        normalized_launch_text = _replace_token(normalized_launch_text, model_path_from_launch, resolved_model_root)
    if resolved_draft_root and draft_model_path_from_launch:
        normalized_launch_text = _replace_token(normalized_launch_text, draft_model_path_from_launch, resolved_draft_root)
    if normalized_launch_text:
        inputs["launch_command_text"] = normalized_launch_text

    resolution = {
        "schema_version": "input_resolution_v1",
        "workspace_dir": str(workspace_dir),
        "path_hints": [str(path) for path in path_hints],
        "root_hints": [str(path) for path in root_hints],
        "launch": {
            "launch_command_file": _normalize_text(inputs.get("launch_command_file", "")),
            "launch_command_text": launch_text,
            "normalized_launch_text": normalized_launch_text,
            "parsed_launch_fields": parsed_launch_fields,
            "evidence": launch_evidence,
        },
        "time_window": {
            "window_start_ns": int(inputs.get("window_start_ns", 0) or 0),
            "window_end_ns": int(inputs.get("window_end_ns", 0) or 0),
            "evidence": time_evidence,
        },
        "model_resolution": {
            "model_root_path": _normalize_text(inputs.get("model_root_path", "")),
            "draft_model_root_path": _normalize_text(inputs.get("draft_model_root_path", "")),
            "model_evidence": model_evidence,
            "draft_model_evidence": draft_evidence,
            "model_dir_candidates": [
                {
                    "path": str(item["path"]),
                    "markers": item["markers"],
                    "score": item["score"],
                }
                for item in model_dirs[:20]
            ],
        },
        "resolved_inputs": {
            "profiling_root_path": _normalize_text(inputs.get("profiling_root_path", "")),
            "code_repo_path": _normalize_text(inputs.get("code_repo_path", "")),
            "model_root_path": _normalize_text(inputs.get("model_root_path", "")),
            "draft_model_root_path": _normalize_text(inputs.get("draft_model_root_path", "")),
            "launch_command_file": _normalize_text(inputs.get("launch_command_file", "")),
            "window_start_ns": int(inputs.get("window_start_ns", 0) or 0),
            "window_end_ns": int(inputs.get("window_end_ns", 0) or 0),
        },
    }

    resolution_path = workspace_dir / "input" / "input_resolution.json"
    normalized_launch_path = workspace_dir / "input" / "launch_command.normalized.json"
    dump_json(resolution_path, resolution)
    dump_json(
        normalized_launch_path,
        {
            "schema_version": "launch_command_normalized_v1",
            "launch_command_file": _normalize_text(inputs.get("launch_command_file", "")),
            "normalized_launch_text": normalized_launch_text,
            "parsed_launch_fields": parsed_launch_fields,
            "resolved_model_root_path": _normalize_text(inputs.get("model_root_path", "")),
            "resolved_draft_model_root_path": _normalize_text(inputs.get("draft_model_root_path", "")),
        },
    )

    artifacts = state.setdefault("artifacts", {})
    artifacts["input_resolution_path"] = str(resolution_path)
    artifacts["normalized_launch_command_path"] = str(normalized_launch_path)
    flags = state.setdefault("flags", {})
    flags["input_resolution_done"] = True
    save_state(workspace_dir, state)
    return resolution


def main() -> int:
    args = build_parser().parse_args()
    resolve_inputs_for_workspace(Path(args.workspace_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
