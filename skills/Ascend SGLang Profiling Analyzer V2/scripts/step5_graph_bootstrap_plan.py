from __future__ import annotations

from pathlib import Path
from typing import Any


BOOTSTRAP_TARGET = "step5_graph_path_analyst"
TARGET_SCRIPT_SEQUENCE = {
    BOOTSTRAP_TARGET: [
        "build_graph_forward_context.py",
        "build_graph_seed_context.py",
        "build_graph_operator_spans.py",
    ],
}

REQUIRED_ARTIFACTS_BY_TARGET = {
    BOOTSTRAP_TARGET: {
        "graph_forward_context_path": "graph_forward_context.json",
        "graph_seed_context_path": "graph_seed_context.json",
        "graph_operator_spans_path": "graph_operator_spans.json",
    },
}

REQUIRED_FLAGS_BY_TARGET = {
    BOOTSTRAP_TARGET: {
        "graph_forward_context_built": "graph_forward_context_built",
        "graph_seed_context_built": "graph_seed_context_built",
        "graph_operator_spans_built": "graph_operator_spans_built",
    },
}


def step5_graph_bootstrap_lock_path(workspace_dir: Path) -> Path:
    return workspace_dir / "logs" / "wrapper_runs" / "step5_graph_bootstrap.lock.json"


def step5_graph_bootstrap_status_path(workspace_dir: Path) -> Path:
    return workspace_dir / "audit" / "step5_graph_bootstrap_in_progress.json"


def build_readiness_snapshot(state: dict[str, Any], target: str = BOOTSTRAP_TARGET) -> dict[str, Any]:
    artifact_results = []
    flag_results = []
    artifacts = state.get("artifacts", {})
    flags = state.get("flags", {})
    for artifact_key, label in REQUIRED_ARTIFACTS_BY_TARGET[target].items():
        raw_path = str(artifacts.get(artifact_key, "")).strip()
        path = Path(raw_path) if raw_path else Path()
        ready = bool(raw_path) and path.exists() and path.is_file()
        artifact_results.append(
            {
                "artifact_key": artifact_key,
                "label": label,
                "path": raw_path,
                "ready": ready,
            }
        )
    for flag_key, label in REQUIRED_FLAGS_BY_TARGET[target].items():
        ready = bool(flags.get(flag_key))
        flag_results.append(
            {
                "flag_key": flag_key,
                "label": label,
                "ready": ready,
            }
        )
    missing_artifacts = [item["artifact_key"] for item in artifact_results if not item["ready"]]
    missing_flags = [item["flag_key"] for item in flag_results if not item["ready"]]
    return {
        "bootstrap_target": target,
        "required_artifacts_ready": not missing_artifacts,
        "required_flags_ready": not missing_flags,
        "ready": not missing_artifacts and not missing_flags,
        "artifacts": artifact_results,
        "flags": flag_results,
        "missing_artifacts": missing_artifacts,
        "missing_flags": missing_flags,
    }
