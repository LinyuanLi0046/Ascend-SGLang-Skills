from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

from workflow_common import dump_json, load_state, save_state


EXPECTED_PATHS = [
    "python/sglang/srt/models",
    "python/sglang/srt/layers",
    "python/sglang/srt/model_executor",
    "python/sglang/srt/speculative",
    "python/sglang/srt/hardware_backend/npu",
    "python/sglang/srt/hardware_backend/npu/attention",
    "python/sglang/srt/hardware_backend/npu/graph_runner",
    "python/sglang/srt/layers/quantization",
]

EXPECTED_FILES = [
    "python/sglang/srt/speculative/eagle_worker_v2.py",
    "python/sglang/srt/hardware_backend/npu/attention/ascend_backend.py",
    "python/sglang/srt/hardware_backend/npu/graph_runner/eagle_draft_npu_graph_runner.py",
    "python/sglang/srt/hardware_backend/npu/graph_runner/eagle_draft_extend_npu_graph_runner.py",
    "python/sglang/srt/hardware_backend/npu/memory_pool_npu.py",
    "python/sglang/srt/layers/communicator.py",
]

EXPECTED_SYMBOLS = [
    ("python/sglang/srt/speculative/eagle_worker_v2.py", "def verify("),
    ("python/sglang/srt/speculative/eagle_worker_v2.py", "def _draft_extend_for_decode("),
    ("python/sglang/srt/model_executor/model_runner.py", "self.model.forward("),
    ("python/sglang/srt/hardware_backend/npu/attention/ascend_backend.py", "torch_npu"),
    ("python/sglang/srt/hardware_backend/npu/memory_pool_npu.py", "reshape_and_cache"),
]

KNOWLEDGE_FILES = [
    "references/knowledge/sglang_path_map.md",
    "references/knowledge/forward_analysis_rules.md",
    "references/knowledge/model_config_and_launch_fields.md",
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="检查当前代码仓与预置知识文档的偏差。")
    parser.add_argument("--workspace-dir", required=True)
    return parser


def _read_text(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def _knowledge_file_status(skill_dir: Path) -> list[dict[str, Any]]:
    rows = []
    for relative in KNOWLEDGE_FILES:
        path = skill_dir / relative
        content = _read_text(path)
        rows.append(
            {
                "relative_path": relative.replace("\\", "/"),
                "exists": path.exists(),
                "is_blank": not content.strip(),
                "line_count": len(content.splitlines()) if content else 0,
            }
        )
    return rows


def _check_paths(repo_root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    rows = []
    missing = []
    for relative in EXPECTED_PATHS:
        path = repo_root / relative
        exists = path.exists()
        rows.append({"relative_path": relative.replace("\\", "/"), "exists": exists, "type": "directory"})
        if not exists:
            missing.append(relative.replace("\\", "/"))
    return rows, missing


def _check_files(repo_root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    rows = []
    missing = []
    for relative in EXPECTED_FILES:
        path = repo_root / relative
        exists = path.exists()
        rows.append({"relative_path": relative.replace("\\", "/"), "exists": exists, "type": "file"})
        if not exists:
            missing.append(relative.replace("\\", "/"))
    return rows, missing


def _find_symbol_line(path: Path, needle: str) -> int:
    if not path.exists():
        return 0
    pattern = re.compile(re.escape(needle))
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if pattern.search(line):
            return index
    return 0


def _check_symbols(repo_root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = []
    missing = []
    for relative, needle in EXPECTED_SYMBOLS:
        path = repo_root / relative
        line = _find_symbol_line(path, needle)
        row = {
            "relative_path": relative.replace("\\", "/"),
            "symbol_hint": needle,
            "matched_line": line,
            "exists": line > 0,
        }
        rows.append(row)
        if line <= 0:
            missing.append(row)
    return rows, missing


def _divergence_level(missing_paths: list[str], missing_files: list[str], missing_symbols: list[dict[str, Any]]) -> str:
    total = len(missing_paths) + len(missing_files) + len(missing_symbols)
    if total == 0:
        return "low"
    if total <= 3:
        return "medium"
    return "high"


def _knowledge_applicability(level: str, knowledge_rows: list[dict[str, Any]]) -> tuple[str, str]:
    all_blank = all(row["is_blank"] for row in knowledge_rows if row["exists"])
    if level == "low" and not all_blank:
        return "knowledge_applicable", "优先参考知识文档，同时继续以当前仓库代码验证。"
    if level == "high":
        return "knowledge_unreliable", "知识文档只能作为弱地图，分析必须优先跟随当前仓库。"
    return "knowledge_partially_applicable", "知识文档可用作目录地图和热点提示，但不能直接当成当前仓库事实。"


def check_repo_divergence_for_workspace(workspace_dir: Path) -> dict[str, Any]:
    state = load_state(workspace_dir)
    repo_root = Path(state["inputs"]["code_repo_path"])
    skill_dir = Path(state["skill_dir"])

    path_rows, missing_paths = _check_paths(repo_root)
    file_rows, missing_files = _check_files(repo_root)
    symbol_rows, missing_symbols = _check_symbols(repo_root)
    knowledge_rows = _knowledge_file_status(skill_dir)
    divergence_level = _divergence_level(missing_paths, missing_files, missing_symbols)
    knowledge_applicability, recommendation = _knowledge_applicability(divergence_level, knowledge_rows)

    output = {
        "schema_version": "repo_divergence_report_v1",
        "repo_root": str(repo_root),
        "skill_dir": str(skill_dir),
        "checked_paths": path_rows,
        "checked_files": file_rows,
        "checked_symbols": symbol_rows,
        "existing_paths": [row["relative_path"] for row in path_rows if row.get("exists")],
        "knowledge_files": knowledge_rows,
        "existing_files": [row["relative_path"] for row in file_rows if row.get("exists")],
        "missing_paths": missing_paths,
        "missing_files": missing_files,
        "existing_symbols": [row for row in symbol_rows if row.get("exists")],
        "missing_or_renamed_symbols": missing_symbols,
        "divergence_level": divergence_level,
        "knowledge_applicability": knowledge_applicability,
        "recommended_analysis_mode": knowledge_applicability,
        "recommendation": recommendation,
    }

    output_path = workspace_dir / "artifacts" / "repo" / "repo_divergence_report.json"
    dump_json(output_path, output)
    state["artifacts"]["repo_divergence_report_path"] = str(output_path)
    state["flags"]["repo_divergence_checked"] = True
    save_state(workspace_dir, state)
    return output


def main() -> int:
    args = build_parser().parse_args()
    check_repo_divergence_for_workspace(Path(args.workspace_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
