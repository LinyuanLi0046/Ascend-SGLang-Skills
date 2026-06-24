from __future__ import annotations

import argparse
import json
import shlex
from pathlib import Path
from typing import Any

from workflow_common import dump_json, load_json, load_state, save_state


CONFIG_FILES = [
    "config.json",
    "generation_config.json",
    "tokenizer_config.json",
    "quant_model_description.json",
]
CONFIG_GLOB_PATTERNS = {
    "config.json": ("config.json", "config*.json"),
    "generation_config.json": ("generation_config.json", "generation_config*.json"),
    "tokenizer_config.json": ("tokenizer_config.json", "tokenizer_config*.json"),
    "quant_model_description.json": ("quant_model_description.json", "quant_model_description*.json"),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="构建 Step 5 运行约束工件。")
    parser.add_argument("--workspace-dir", required=True)
    return parser


def _normalize_text(value: Any) -> str:
    return str(value or "").strip().strip('"').strip("'")


def _read_input_resolution(workspace_dir: Path, state: dict[str, Any]) -> dict[str, Any]:
    resolution_path = str(state.get("artifacts", {}).get("input_resolution_path", "")).strip()
    candidate = Path(resolution_path) if resolution_path else workspace_dir / "input" / "input_resolution.json"
    if candidate.exists() and candidate.is_file():
        return load_json(candidate)
    return {}


def _read_launch_text(workspace_dir: Path, state: dict[str, Any]) -> str:
    input_resolution = _read_input_resolution(workspace_dir, state)
    normalized_launch_text = _normalize_text(input_resolution.get("launch", {}).get("normalized_launch_text", ""))
    if normalized_launch_text:
        return normalized_launch_text
    launch_file = _normalize_text(state["inputs"].get("launch_command_file", ""))
    launch_text = _normalize_text(state["inputs"].get("launch_command_text", ""))
    if launch_text:
        return launch_text
    if launch_file and Path(launch_file).exists():
        return Path(launch_file).read_text(encoding="utf-8")
    return ""


def parse_launch_fields(launch_text: str) -> dict[str, Any]:
    if not launch_text.strip():
        return {}
    try:
        tokens = shlex.split(launch_text, posix=False)
    except ValueError:
        tokens = launch_text.split()
    continuation_tokens = {"\\", "`"}
    tokens = [token for token in tokens if token not in continuation_tokens]
    fields: dict[str, Any] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("--"):
            index += 1
            continue
        stripped = token[2:]
        if "=" in stripped:
            key, value = stripped.split("=", 1)
            fields[key.replace("-", "_")] = _normalize_text(value)
            index += 1
            continue
        key = stripped.replace("-", "_")
        next_value = ""
        if index + 1 < len(tokens) and not tokens[index + 1].startswith("--"):
            next_value = _normalize_text(tokens[index + 1])
            index += 2
        else:
            next_value = "true"
            index += 1
        fields[key] = next_value
    return fields


def normalize_speculative_algorithm(value: Any) -> str:
    normalized = _normalize_text(value).lower()
    if not normalized:
        return ""
    if normalized == "nextn":
        return "eagle"
    return normalized


def _select_existing_config_file(model_root: Path, canonical_name: str) -> Path | None:
    for pattern in CONFIG_GLOB_PATTERNS[canonical_name]:
        matches = sorted(path for path in model_root.glob(pattern) if path.is_file())
        if matches:
            exact = [path for path in matches if path.name.lower() == canonical_name.lower()]
            return exact[0] if exact else matches[0]
    return None


def load_model_configs(model_root: Path | None) -> dict[str, Any]:
    if model_root is None:
        return {
            "model_root": "",
            "exists": False,
            "is_dir": False,
            "config_files": [],
            "parsed_configs": {},
            "errors": ["missing_model_root"],
            "architecture_candidates": [],
            "model_type": "",
            "resolved_model_family": "",
        }
    parsed_configs: dict[str, Any] = {}
    config_files: list[str] = []
    errors: list[str] = []
    for relative in CONFIG_FILES:
        path = _select_existing_config_file(model_root, relative)
        if path is None:
            continue
        config_files.append(str(path))
        try:
            parsed_configs[relative] = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            parsed_configs[relative] = {"_error": "invalid_json"}
            errors.append(f"invalid_json:{relative}")
    config_json = parsed_configs.get("config.json", {})
    architecture_candidates = [
        _normalize_text(item)
        for item in config_json.get("architectures", [])
        if _normalize_text(item)
    ] if isinstance(config_json, dict) else []
    model_type = _normalize_text(config_json.get("model_type", "")) if isinstance(config_json, dict) else ""
    return {
        "model_root": str(model_root),
        "exists": model_root.exists(),
        "is_dir": model_root.is_dir(),
        "config_files": config_files,
        "parsed_configs": parsed_configs,
        "errors": errors,
        "architecture_candidates": architecture_candidates,
        "model_type": model_type,
        "resolved_model_family": architecture_candidates[0] if architecture_candidates else model_type,
    }


def _phase_candidates(
    graph_enabled: bool,
    spec_algorithm: str,
    launch_fields: dict[str, Any],
    launch_text: str,
) -> list[str]:
    if not graph_enabled:
        return []
    normalized_text = launch_text.lower()
    phases: list[str] = []
    if spec_algorithm in {"eagle", "eagle3"} or any(
        key in launch_fields for key in ["speculative_draft_model_path", "speculative_num_steps", "speculative_num_draft_tokens"]
    ):
        phases.extend(["verify", "draft_prefill", "draft_decode"])
    elif "decode" in normalized_text:
        phases.append("decode")
    if not phases:
        phases.append("graph_replay")
    return phases


def _quant_candidates(
    model_context: dict[str, Any],
    launch_fields: dict[str, Any],
    launch_text: str,
    *,
    include_default_field_keys: bool = True,
    extra_field_keys: list[str] | None = None,
    allow_text_markers: bool = True,
) -> list[str]:
    normalized = launch_text.lower()
    candidates: set[str] = set()
    config_json = model_context.get("parsed_configs", {}).get("config.json", {})
    if isinstance(config_json, dict):
        for key in ["quantization_config", "quant_method", "quantization"]:
            value = config_json.get(key)
            if isinstance(value, dict):
                method = value.get("quant_method") or value.get("method")
                if method:
                    candidates.add(_normalize_text(method))
            elif value:
                candidates.add(_normalize_text(value))
    field_keys = ["quantization"] if include_default_field_keys else []
    if extra_field_keys:
        field_keys.extend(extra_field_keys)
    for key in field_keys:
        value = _normalize_text(launch_fields.get(key, ""))
        if value:
            candidates.add(value.lower())
    if allow_text_markers:
        for marker in ["modelslim", "awq", "gptq", "fp8", "int8"]:
            if marker in normalized:
                candidates.add(marker)
    return sorted(item for item in candidates if item)


def _backend_candidates(launch_fields: dict[str, Any], launch_text: str) -> list[str]:
    normalized = launch_text.lower()
    candidates = []
    backend_text = " ".join(
        _normalize_text(launch_fields.get(key, ""))
        for key in [
            "device",
            "attention_backend",
            "decode_attention_backend",
            "prefill_attention_backend",
            "speculative_draft_attention_backend",
        ]
    ).lower()
    if any(token in (normalized + " " + backend_text) for token in ["ascend", "npu"]):
        candidates.append("ascend_npu")
    if any(token in (normalized + " " + backend_text) for token in ["cuda", "gpu"]):
        candidates.append("cuda")
    return candidates or ["unknown"]


def _safe_int(value: Any) -> int:
    text = _normalize_text(value)
    if not text:
        return 0
    try:
        return int(text)
    except ValueError:
        return 0


def _bool_flag(fields: dict[str, Any], key: str) -> bool:
    value = _normalize_text(fields.get(key, ""))
    if not value:
        return False
    return value.lower() not in {"false", "0", "no"}


def _collect_precondition_status(
    launch_text: str,
    parsed_launch_fields: dict[str, Any],
    graph_enabled: bool,
    model_root: Path,
    primary_model_context: dict[str, Any],
    draft_model_root: Path | None,
    draft_model_context: dict[str, Any],
    spec_algorithm: str,
    quant_modes: list[str],
) -> tuple[list[str], list[str]]:
    blockers: list[str] = []
    warnings: list[str] = []
    if not launch_text.strip():
        blockers.append("缺少 launch_command_text，无法判断 graph/speculative/backend 分支。")
    if not model_root.exists() or not model_root.is_dir():
        blockers.append(f"主模型目录不存在或不是目录: {model_root}")
    if not primary_model_context.get("config_files"):
        blockers.append("主模型目录缺少可解析的 config/generation/tokenizer/quant 描述文件。")
    if not primary_model_context.get("resolved_model_family"):
        blockers.append("主模型 config 未解析到 architectures/model_type，无法缩圈模型实现文件。")
    if not graph_enabled:
        blockers.append("启动参数显示 graph 已关闭，Step5 不应尝试 graph replay 下钻。")
    requires_draft_model = spec_algorithm in {"eagle", "eagle3"} or bool(
        _normalize_text(parsed_launch_fields.get("speculative_draft_model_path", ""))
    )
    if requires_draft_model:
        if draft_model_root is None:
            blockers.append("speculative 模式缺少 draft 模型目录，无法区分 verify / draft_prefill / draft_decode。")
        else:
            if not draft_model_root.exists() or not draft_model_root.is_dir():
                blockers.append(f"draft 模型目录不存在或不是目录: {draft_model_root}")
            if not draft_model_context.get("config_files"):
                blockers.append("draft 模型目录缺少可解析的 config/generation/tokenizer/quant 描述文件。")
            if not draft_model_context.get("resolved_model_family"):
                blockers.append("draft 模型 config 未解析到 architectures/model_type，无法缩圈 draft 路径。")
    if "modelslim" in quant_modes:
        primary_quant_path = model_root / "quant_model_description.json"
        if not primary_quant_path.exists():
            blockers.append("主模型量化模式包含 modelslim，但缺少 quant_model_description.json。")
    draft_quant_modes = (
        _quant_candidates(
            draft_model_context,
            parsed_launch_fields,
            launch_text,
            include_default_field_keys=False,
            extra_field_keys=["speculative_draft_model_quantization"],
            allow_text_markers=False,
        )
        if draft_model_context
        else []
    )
    if "modelslim" in draft_quant_modes and draft_model_root is not None:
        draft_quant_path = draft_model_root / "quant_model_description.json"
        if not draft_quant_path.exists():
            blockers.append("draft 模型量化模式包含 modelslim，但缺少 quant_model_description.json。")
    if not any(key in parsed_launch_fields for key in ["attention_backend", "decode_attention_backend", "prefill_attention_backend"]):
        warnings.append("启动参数未显式给出 attention backend，后续需更依赖 repo registry 与模型结构判断。")
    if not any(_safe_int(parsed_launch_fields.get(key, 0)) > 1 for key in ["tp_size", "dp_size", "ep_size", "attn_cp_size"]):
        warnings.append("并行字段未显式给出大于 1 的值；若 profiling 中存在通信 span，后续仍需结合 stack 证据复核。")
    return blockers, warnings


def build_runtime_constraints_for_workspace(workspace_dir: Path) -> dict[str, Any]:
    state = load_state(workspace_dir)
    input_resolution = _read_input_resolution(workspace_dir, state)
    launch_text = _read_launch_text(workspace_dir, state)
    parsed_launch_fields = parse_launch_fields(launch_text)
    model_root_text = _normalize_text(state["inputs"].get("model_root_path", ""))
    model_root = Path(model_root_text) if model_root_text else None
    primary_model_context = load_model_configs(model_root)
    resolved_inputs = input_resolution.get("resolved_inputs", {})
    draft_model_path = _normalize_text(
        resolved_inputs.get("draft_model_root_path", "")
        or state["inputs"].get("draft_model_root_path", "")
        or parsed_launch_fields.get("speculative_draft_model_path", "")
    )
    draft_model_root = Path(draft_model_path) if draft_model_path else None
    draft_model_context = load_model_configs(draft_model_root) if draft_model_root else {}

    spec_algorithm_raw = _normalize_text(parsed_launch_fields.get("speculative_algorithm", ""))
    spec_algorithm = normalize_speculative_algorithm(spec_algorithm_raw)
    backend_candidates = _backend_candidates(parsed_launch_fields, launch_text)
    graph_enabled = (
        not _bool_flag(parsed_launch_fields, "disable_cuda_graph")
        and not _bool_flag(parsed_launch_fields, "disable_graph")
    )
    quant_modes = _quant_candidates(primary_model_context, parsed_launch_fields, launch_text)
    blockers, warnings = _collect_precondition_status(
        launch_text,
        parsed_launch_fields,
        graph_enabled,
        model_root or Path(),
        primary_model_context,
        draft_model_root,
        draft_model_context,
        spec_algorithm,
        quant_modes,
    )
    phase_candidates = _phase_candidates(graph_enabled, spec_algorithm, parsed_launch_fields, launch_text)
    attention_backend_candidates = sorted(
        {
            _normalize_text(parsed_launch_fields.get(key, ""))
            for key in [
                "attention_backend",
                "decode_attention_backend",
                "prefill_attention_backend",
                "speculative_draft_attention_backend",
            ]
            if _normalize_text(parsed_launch_fields.get(key, ""))
        }
    )
    spec_mode = "spec_v2" if spec_algorithm in {"eagle", "eagle3"} else ("decode_graph" if graph_enabled else "disabled")
    resolved_model_family = primary_model_context.get("resolved_model_family", "")
    model_type = primary_model_context.get("model_type", "")
    output = {
        "schema_version": "runtime_constraints_v2",
        "launch_text": launch_text,
        "parsed_launch_fields": parsed_launch_fields,
        "model_root": str(model_root or ""),
        "model_path": _normalize_text(parsed_launch_fields.get("model_path", "")),
        "draft_model_root": str(draft_model_root) if draft_model_root else "",
        "draft_model_path": draft_model_path,
        "speculative_algorithm": spec_algorithm_raw,
        "normalized_speculative_algorithm": spec_algorithm,
        "speculative_enabled": bool(spec_algorithm),
        "config_files": primary_model_context.get("config_files", []),
        "resolved_model_family": resolved_model_family,
        "architecture_candidates": primary_model_context.get("architecture_candidates", []),
        "model_type": model_type,
        "graph_enabled": graph_enabled,
        "spec_mode": spec_mode,
        "phase_candidates": phase_candidates,
        "backend_candidates": backend_candidates,
        "attention_backend_candidates": attention_backend_candidates,
        "quant_mode_candidates": quant_modes,
        "primary_model_context": primary_model_context,
        "draft_model_context": draft_model_context,
        "moe_enabled": any(token in (model_type + " " + " ".join(primary_model_context.get("architecture_candidates", []))).lower() for token in ["moe", "mixtral"]),
        "parallelism": {
            "tp_size": _safe_int(parsed_launch_fields.get("tp_size", 0)),
            "dp_size": _safe_int(parsed_launch_fields.get("dp_size", 0)),
            "ep_size": _safe_int(parsed_launch_fields.get("ep_size", 0)),
            "attn_cp_size": _safe_int(parsed_launch_fields.get("attn_cp_size", 0)),
        },
        "likely_runtime_entrypoints": [
            "python/sglang/srt/speculative/eagle_worker_v2.py",
            "python/sglang/srt/model_executor/model_runner.py",
            "python/sglang/srt/hardware_backend/npu/graph_runner",
        ],
        "step5_preconditions": {
            "status": "ready" if not blockers else "blocked",
            "ready": not blockers,
            "blockers": blockers,
            "warnings": warnings,
            "requires_draft_model": bool(draft_model_path or spec_algorithm in {"eagle", "eagle3"}),
        },
        "constraint_confidence": "high" if not blockers and resolved_model_family else ("medium" if resolved_model_family or backend_candidates != ["unknown"] else "low"),
    }

    output_path = workspace_dir / "input" / "runtime_constraints.json"
    dump_json(output_path, output)
    state["artifacts"]["runtime_constraints_path"] = str(output_path)
    state["flags"]["runtime_constraints_built"] = True
    save_state(workspace_dir, state)
    return output


def main() -> int:
    args = build_parser().parse_args()
    build_runtime_constraints_for_workspace(Path(args.workspace_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
