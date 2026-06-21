from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from workflow_common import dump_json, load_json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="校验 graph_forward_candidate_graph.json 的结构。")
    parser.add_argument("--candidate-graph", required=True, help="候选图 JSON 绝对路径。")
    parser.add_argument("--output", required=False, help="校验结果输出路径。")
    return parser


def _ensure(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def validate_payload(payload: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    nodes = payload.get("nodes", [])
    edges = payload.get("edges", [])
    operator_nodes = payload.get("operator_nodes", [])

    _ensure(isinstance(nodes, list), "nodes 必须是 list。", errors)
    _ensure(isinstance(edges, list), "edges 必须是 list。", errors)
    _ensure(isinstance(operator_nodes, list), "operator_nodes 必须是 list。", errors)

    for field in ["entry_phase", "runtime_entry", "model_file_candidates"]:
        if field not in payload:
            errors.append(f"缺少字段: {field}")

    if not operator_nodes:
        warnings.append("operator_nodes 为空，候选图目前可能仍停留在顶层 forward 链。")

    return {
        "schema_version": "graph_candidate_graph_validation_v1",
        "status": "passed" if not errors else "failed",
        "errors": errors,
        "warnings": warnings,
        "stats": {
            "node_count": len(nodes) if isinstance(nodes, list) else 0,
            "edge_count": len(edges) if isinstance(edges, list) else 0,
            "operator_node_count": len(operator_nodes) if isinstance(operator_nodes, list) else 0,
        },
    }


def main() -> int:
    args = build_parser().parse_args()
    candidate_path = Path(args.candidate_graph)
    payload = load_json(candidate_path)
    result = validate_payload(payload)
    output_path = Path(args.output) if args.output else candidate_path.with_suffix(".validation.json")
    dump_json(output_path, result)
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
