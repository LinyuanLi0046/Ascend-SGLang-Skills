from __future__ import annotations

from pathlib import Path
from typing import Any


TARGET_SCRIPT_SEQUENCE = {
    "step4_stack_mapper": [
        "check_repo_divergence.py",
        "build_runtime_constraints.py",
        "build_stack_evidence.py",
        "build_graph_phase_stack_evidence.py",
        "classify_graph_groups.py",
        "build_graph_mapping_targets.py",
        "build_external_mapping_targets.py",
        "build_stack_call_paths.py",
    ],
}

REQUIRED_ARTIFACTS_BY_TARGET = {
    "step4_stack_mapper": {
        "repo_divergence_report_path": "repo_divergence_report.json",
        "runtime_constraints_path": "runtime_constraints.json",
        "stack_evidence_path": "stack_evidence.json",
        "stack_evidence_lite_path": "stack_evidence_lite.json",
        "graph_phase_stack_evidence_path": "graph_phase_stack_evidence.json",
        "graph_execution_plan_path": "graph_execution_plan.json",
        "graph_mapping_targets_path": "graph_mapping_targets.json",
        "external_mapping_targets_path": "external_mapping_targets.json",
        "stack_call_paths_path": "stack_call_paths.json",
    },
}

REQUIRED_FLAGS_BY_TARGET = {
    "step4_stack_mapper": {
        "repo_divergence_checked": "repo_divergence_checked",
        "runtime_constraints_built": "runtime_constraints_built",
        "stack_evidence_built": "stack_evidence_built",
        "graph_phase_stack_evidence_built": "graph_phase_stack_evidence_built",
        "graph_mapping_targets_built": "graph_mapping_targets_built",
        "external_mapping_targets_built": "external_mapping_targets_built",
        "stack_call_paths_built": "stack_call_paths_built",
    },
}


def step4_bootstrap_lock_path(workspace_dir: Path) -> Path:
    return workspace_dir / "logs" / "wrapper_runs" / "step4_bootstrap.lock.json"


def step4_bootstrap_status_path(workspace_dir: Path) -> Path:
    return workspace_dir / "audit" / "step4_bootstrap_in_progress.json"


def build_readiness_snapshot(state: dict[str, Any], target: str) -> dict[str, Any]:
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
