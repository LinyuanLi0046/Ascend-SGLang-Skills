from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

from workflow_common import dump_json, iter_classified_streams, load_json, load_state, save_state, validate_code_location


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成 span_code_mapping.json。")
    parser.add_argument("--workspace-dir", required=True)
    return parser


def load_graph_execution_plan(path: Path) -> dict[str, Any]:
    if not str(path) or str(path) == "." or not path.exists() or not path.is_file():
        return {"mapping_hints": []}
    return load_json(path)


def load_graph_span_alignment(path: Path) -> dict[str, Any]:
    if not str(path) or str(path) == "." or not path.exists() or not path.is_file():
        return {"items": []}
    return load_json(path)


def load_external_span_mapping(path: Path) -> dict[str, Any]:
    if not str(path) or str(path) == "." or not path.exists() or not path.is_file():
        return {"rows": []}
    return load_json(path)


def load_graph_operator_spans(path: Path) -> dict[str, Any]:
    if not str(path) or str(path) == "." or not path.exists() or not path.is_file():
        return {"rows": []}
    return load_json(path)


def load_graph_mapping_targets(path: Path) -> dict[str, Any]:
    if not str(path) or str(path) == "." or not path.exists() or not path.is_file():
        return {"rows": []}
    return load_json(path)


def resolve_stack_evidence_path(state: dict[str, Any]) -> Path:
    artifacts = state.get("artifacts", {})
    lite_path_str = str(artifacts.get("stack_evidence_lite_path", "")).strip()
    if lite_path_str:
        lite_path = Path(lite_path_str)
        if lite_path.exists() and lite_path.is_file():
            return lite_path
    full_path = Path(artifacts["stack_evidence_path"])
    sibling_lite_path = full_path.with_name("stack_evidence_lite.json")
    if sibling_lite_path.exists() and sibling_lite_path.is_file():
        return sibling_lite_path
    return full_path


TOKEN_RE = re.compile(r"[a-z0-9_./]+")


def tokenize_search_text(text: str) -> set[str]:
    return {token for token in TOKEN_RE.findall(text.lower()) if len(token) >= 3}


def extract_graph_alignment_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("items", "rows"):
        value = payload.get(key, [])
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def build_stack_search_index(stack_rows: list[dict[str, Any]]) -> dict[str, Any]:
    prepared_rows: list[dict[str, Any]] = []
    token_index: dict[str, list[dict[str, Any]]] = {}
    for row in stack_rows:
        primary_file_function = row.get("primary_file_function") or {}
        code_line_candidates = row.get("code_line_candidates", [])
        if not primary_file_function and not code_line_candidates:
            continue
        op_name = str(row.get("op_name", "")).lower()
        raw_stack = str(row.get("raw_call_stack", "")).lower()
        function_text = " ".join(
            str(candidate.get("file_function", ""))
            for candidate in row.get("file_function_candidates", [])
            if isinstance(candidate, dict)
        ).lower()
        prepared = {
            **row,
            "_prepared_op_name": op_name,
            "_prepared_raw_stack": raw_stack,
            "_prepared_function_text": function_text,
        }
        prepared_rows.append(prepared)
        row_tokens = tokenize_search_text(" ".join([op_name, raw_stack, function_text]))
        for token in row_tokens:
            token_index.setdefault(token, []).append(prepared)
    return {"prepared_rows": prepared_rows, "token_index": token_index}


def select_stack_candidate(span: dict[str, Any], stack_index: dict[str, Any]) -> dict[str, Any]:
    search_terms = {
        str(span.get("span_name", "")).strip().lower(),
        str(span.get("semantic_class", "")).strip().lower(),
    }
    search_terms.update(str(item).strip().lower() for item in span.get("related_op_names", []) if str(item).strip())
    search_terms.discard("")
    search_tokens = set()
    for term in search_terms:
        search_tokens.update(tokenize_search_text(term))
    candidate_rows = []
    token_index = stack_index.get("token_index", {})
    seen_row_ids: set[str] = set()
    for token in search_tokens:
        for row in token_index.get(token, []):
            row_id = str(row.get("op_row_id", "")).strip()
            if row_id and row_id in seen_row_ids:
                continue
            seen_row_ids.add(row_id)
            candidate_rows.append(row)
    if not candidate_rows:
        candidate_rows = stack_index.get("prepared_rows", [])
    candidates = []
    for row in candidate_rows:
        op_name = str(row.get("_prepared_op_name", ""))
        raw_stack = str(row.get("_prepared_raw_stack", ""))
        function_text = str(row.get("_prepared_function_text", ""))
        score = 0
        for term in search_terms:
            if term and term in op_name:
                score += 3
            if term and term in raw_stack:
                score += 2
            if term and term in function_text:
                score += 2
        if row.get("has_spec_v2_anchor"):
            score += 1
        if row.get("has_replay_anchor"):
            score -= 1
        if score > 0:
            candidates.append((score, row))
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1] if candidates else {}


def select_graph_hint(span: dict[str, Any], graph_plan: dict[str, Any]) -> dict[str, Any]:
    hints = graph_plan.get("mapping_hints", [])
    span_id = str(span.get("span_id", ""))
    for hint in hints:
        if span_id and span_id in hint.get("candidate_span_ids", []):
            return hint
    return {}


def select_external_span_mapping(span: dict[str, Any], external_rows: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return external_rows.get(str(span.get("span_id", "")), {})


def build_graph_alignment_support(graph_alignment_row: dict[str, Any], is_graph_candidate: bool) -> dict[str, Any]:
    if not isinstance(graph_alignment_row, dict) or not graph_alignment_row:
        return {
            "graph_alignment_present": False,
            "graph_alignment_support_status": "missing" if is_graph_candidate else "not_applicable",
            "graph_alignment_location_kind": "",
            "graph_alignment_requires_further_drilldown": None,
            "graph_alignment_candidate_code_location": "",
            "graph_alignment_graph_operator_span_id": "",
            "graph_alignment_operator_evidence_kind": "",
            "graph_alignment_operator_ref_valid": None,
            "graph_alignment_phase": "",
        }
    location_kind = str(graph_alignment_row.get("location_kind", "")).strip()
    requires_further_drilldown = graph_alignment_row.get("requires_further_drilldown")
    operator_ref_valid = graph_alignment_row.get("_operator_ref_valid")
    support_status = "intermediate_alignment_retained"
    if operator_ref_valid is False:
        support_status = "invalid_operator_span_reference"
    elif location_kind == "operator_call" and requires_further_drilldown is False:
        support_status = "final_operator_call"
    return {
        "graph_alignment_present": True,
        "graph_alignment_support_status": support_status,
        "graph_alignment_location_kind": location_kind,
        "graph_alignment_requires_further_drilldown": requires_further_drilldown,
        "graph_alignment_candidate_code_location": str(graph_alignment_row.get("code_location", "")).strip(),
        "graph_alignment_graph_operator_span_id": str(graph_alignment_row.get("graph_operator_span_id", "")).strip(),
        "graph_alignment_operator_evidence_kind": str(graph_alignment_row.get("operator_evidence_kind", "")).strip(),
        "graph_alignment_operator_ref_valid": operator_ref_valid if isinstance(operator_ref_valid, bool) else None,
        "graph_alignment_phase": str(graph_alignment_row.get("phase", "")).strip(),
    }


def build_row(
    span: dict[str, Any],
    external_span_mapping_row: dict[str, Any],
    stack_candidate: dict[str, Any],
    graph_hint: dict[str, Any],
    graph_alignment_row: dict[str, Any],
    is_graph_candidate: bool,
) -> dict[str, Any]:
    mapping_basis: list[str] = []
    evidence_sources: list[str] = []
    code_location = ""
    confidence = "low"
    mapped_region = ""
    owner_class = "unknown"
    phase = "unknown"
    graph_alignment_support = build_graph_alignment_support(graph_alignment_row, is_graph_candidate)

    if not span.get("exclude_from_code_mapping", False):
        if external_span_mapping_row and not is_graph_candidate:
            code_location = str(external_span_mapping_row.get("code_location", "")).strip()
            confidence = str(external_span_mapping_row.get("confidence", "high"))
            owner_class = str(external_span_mapping_row.get("owner_class", "runtime_control"))
            mapping_basis.extend(external_span_mapping_row.get("mapping_basis", []))
            evidence_sources.extend(external_span_mapping_row.get("evidence_sources", []))
            if external_span_mapping_row.get("code_line_candidates") or external_span_mapping_row.get("file_function_candidates"):
                mapping_basis.append("external_span_mapping")
                evidence_sources.append("external_span_mapping.json")
        elif graph_alignment_row:
            if str(graph_alignment_row.get("location_kind", "")).strip() == "operator_call":
                code_location = str(graph_alignment_row.get("code_location", "")).strip()
                confidence = str(graph_alignment_row.get("confidence", "high"))
                mapped_region = str(graph_alignment_row.get("mapped_region", ""))
                owner_class = str(graph_alignment_row.get("owner_class", "model_forward"))
                phase = str(graph_alignment_row.get("phase", phase))
                mapping_basis.extend(graph_alignment_row.get("mapping_basis", []))
                evidence_sources.extend(graph_alignment_row.get("evidence_sources", []))
            else:
                notes = [
                    "graph span alignment 尚未达到 operator_call 精度；Step6 不会把 phase marker / replay entry 写成正式 graph code mapping。"
                ]
                graph_alignment_row = {**graph_alignment_row, "_mapping_notes": notes}
        if not code_location and stack_candidate and not is_graph_candidate:
            best_line_candidate = next(
                (
                    candidate
                    for candidate in stack_candidate.get("code_line_candidates", [])
                    if str(candidate.get("code_location", "")).strip()
                ),
                {},
            )
            if best_line_candidate:
                code_location = str(best_line_candidate.get("code_location", "")).strip()
                confidence = str(best_line_candidate.get("confidence", "medium")).strip() or "medium"
                owner_class = "runtime_control"
                mapping_basis.extend(best_line_candidate.get("basis", []))
                mapping_basis.append("call_stack_line_candidate")
                evidence_sources.append("operator_details.csv")
            else:
                primary_file_function = stack_candidate.get("primary_file_function") or {}
                path = str(primary_file_function.get("repo_relative_path", "")).strip()
                line = int(primary_file_function.get("entry_line", 0) or 0)
                if path and line > 0:
                    code_location = f"{path}:{line}"
                    confidence = "low"
                    owner_class = "runtime_control"
                    mapping_basis.append("call_stack_primary_file_function")
                    mapping_basis.append("function_entry_fallback")
                    evidence_sources.append("operator_details.csv")

    if code_location and not validate_code_location(code_location):
        code_location = ""
        confidence = "low"

    notes = list(graph_alignment_row.get("_mapping_notes", [])) if isinstance(graph_alignment_row, dict) else []
    if is_graph_candidate and not graph_alignment_row:
        notes.append("graph span 缺少可消费的逐 span graph alignment，Step6 未再退回 phase-level graph hint。")
    if (
        graph_alignment_support["graph_alignment_present"]
        and graph_alignment_support["graph_alignment_support_status"] != "final_operator_call"
        and graph_alignment_support["graph_alignment_candidate_code_location"]
    ):
        notes.append(
            "已保留 graph alignment 候选 code_location 作为辅助调试信息，但未将其写入正式 Step6 code mapping。"
        )

    return {
        "span_id": span["span_id"],
        "stream_id": span["stream_id"],
        "span_name": span["span_name"],
        "phase": phase,
        "owner_class": owner_class,
        "code_location": code_location,
        "mapped_region": mapped_region,
        "confidence": confidence,
        "mapping_basis": mapping_basis,
        "evidence_sources": evidence_sources,
        "task_ids": span.get("task_ids", []),
        "op_row_ids": span.get("op_row_ids", []),
        "trace_event_ref": span.get("trace_event_ref", {}),
        "exclude_from_code_mapping": span.get("exclude_from_code_mapping", False),
        "notes": notes,
        **graph_alignment_support,
    }


def main() -> int:
    args = build_parser().parse_args()
    workspace_dir = Path(args.workspace_dir)
    state = load_state(workspace_dir)
    stack_evidence_path = resolve_stack_evidence_path(state)
    stack_evidence = load_json(stack_evidence_path)
    external_span_mapping = load_external_span_mapping(Path(state["artifacts"].get("external_span_mapping_path", "")))
    graph_plan = load_graph_execution_plan(Path(state["artifacts"].get("graph_execution_plan_path", "")))
    graph_alignment = load_graph_span_alignment(Path(state["artifacts"].get("graph_span_alignment_path", "")))
    graph_operator_spans = load_graph_operator_spans(Path(state["artifacts"].get("graph_operator_spans_path", "")))
    graph_mapping_targets = load_graph_mapping_targets(Path(state["artifacts"].get("graph_mapping_targets_path", "")))

    rows = []
    total_span_count = 0
    semantic_span_count = 0
    excluded_span_count = 0
    mapped_span_count = 0
    unresolved_semantic_span_count = 0
    low_confidence_span_count = 0
    graph_candidate_span_count = 0
    graph_final_operator_call_count = 0
    graph_intermediate_alignment_retained_count = 0
    graph_missing_alignment_count = 0
    graph_invalid_operator_ref_count = 0

    stack_rows = stack_evidence.get("rows", [])
    stack_index = build_stack_search_index(stack_rows)
    external_mapping_by_span = {
        str(row.get("span_id", "")): row for row in external_span_mapping.get("rows", []) if str(row.get("span_id", "")).strip()
    }
    graph_alignment_items = extract_graph_alignment_items(graph_alignment)
    graph_alignment_by_span = {
        str(row.get("span_id", "")).strip(): row
        for row in graph_alignment_items
        if str(row.get("span_id", "")).strip()
    }
    graph_operator_span_ids = {
        str(row.get("graph_operator_span_id", "")).strip()
        for row in graph_operator_spans.get("rows", [])
        if isinstance(row, dict) and str(row.get("graph_operator_span_id", "")).strip()
    }
    graph_mapping_target_span_ids = {
        str(row.get("span_id", "")).strip()
        for row in graph_mapping_targets.get("rows", [])
        if isinstance(row, dict) and str(row.get("span_id", "")).strip()
    }
    classified_path = Path(state["artifacts"]["classified_spans_path"])
    for stream in iter_classified_streams(classified_path):
        for span in stream.get("spans", []):
            total_span_count += 1
            if span.get("exclude_from_code_mapping"):
                excluded_span_count += 1
            else:
                semantic_span_count += 1
            external_span_mapping_row = select_external_span_mapping(span, external_mapping_by_span)
            stack_candidate = {} if span.get("exclude_from_code_mapping") else select_stack_candidate(span, stack_index)
            graph_hint = select_graph_hint(span, graph_plan)
            graph_alignment_row = graph_alignment_by_span.get(span["span_id"], {})
            if graph_alignment_row:
                graph_operator_span_id = str(graph_alignment_row.get("graph_operator_span_id", "")).strip()
                if graph_operator_span_id and graph_operator_span_id not in graph_operator_span_ids:
                    graph_alignment_row = {
                        **graph_alignment_row,
                        "_operator_ref_valid": False,
                        "_mapping_notes": [
                            f"graph_operator_span_id={graph_operator_span_id} 无法在 graph_operator_spans.json 中回溯，已拒绝写入正式 graph code mapping。"
                        ],
                        "location_kind": "graph_replay_entry",
                    }
                else:
                    graph_alignment_row = {**graph_alignment_row, "_operator_ref_valid": True}
            is_graph_candidate = bool(
                graph_alignment_row
                or str(span.get("span_id", "")) in graph_mapping_target_span_ids
            )
            row = build_row(
                span,
                external_span_mapping_row,
                stack_candidate,
                graph_hint,
                graph_alignment_row,
                is_graph_candidate,
            )
            if is_graph_candidate:
                graph_candidate_span_count += 1
            support_status = str(row.get("graph_alignment_support_status", "")).strip()
            if support_status == "final_operator_call":
                graph_final_operator_call_count += 1
            elif support_status == "intermediate_alignment_retained":
                graph_intermediate_alignment_retained_count += 1
            elif support_status == "missing":
                graph_missing_alignment_count += 1
            elif support_status == "invalid_operator_span_reference":
                graph_invalid_operator_ref_count += 1
            if row["code_location"]:
                mapped_span_count += 1
                if row["confidence"] == "low":
                    low_confidence_span_count += 1
            elif not row["exclude_from_code_mapping"]:
                unresolved_semantic_span_count += 1
            rows.append(row)

    coverage = {
        "total_span_count": total_span_count,
        "semantic_span_count": semantic_span_count,
        "excluded_span_count": excluded_span_count,
        "mapped_span_count": mapped_span_count,
        "unresolved_semantic_span_count": unresolved_semantic_span_count,
        "low_confidence_span_count": low_confidence_span_count,
        "graph_candidate_span_count": graph_candidate_span_count,
        "graph_final_operator_call_count": graph_final_operator_call_count,
        "graph_intermediate_alignment_retained_count": graph_intermediate_alignment_retained_count,
        "graph_missing_alignment_count": graph_missing_alignment_count,
        "graph_invalid_operator_ref_count": graph_invalid_operator_ref_count,
    }
    output = {
        "schema_version": "span_code_mapping_v1",
        "rows": rows,
        "coverage": coverage,
    }
    output_path = workspace_dir / "artifacts" / "mapping" / "span_code_mapping.json"
    dump_json(output_path, output)
    ensure(output_path.exists() and output_path.is_file(), f"span_code_mapping.json 未落盘: {output_path}")
    persisted_output = load_json(output_path)
    ensure(isinstance(persisted_output.get("rows"), list), "span_code_mapping.json 缺少 rows。")
    ensure(persisted_output.get("coverage") == coverage, "span_code_mapping.json coverage 与内存结果不一致。")
    state["artifacts"]["span_code_mapping_path"] = str(output_path)
    state["artifacts"]["step6_stack_evidence_path"] = str(stack_evidence_path)
    state["flags"]["span_mapping_done"] = True
    state["flags"]["has_low_confidence_mappings"] = low_confidence_span_count > 0
    state["flags"]["has_unresolved_semantic_spans"] = unresolved_semantic_span_count > 0
    save_state(workspace_dir, state)
    persisted_state = load_state(workspace_dir)
    ensure(
        str(persisted_state["artifacts"].get("span_code_mapping_path", "")).strip() == str(output_path),
        "state.artifacts.span_code_mapping_path 未正确回写。",
    )
    ensure(bool(persisted_state["flags"].get("span_mapping_done")), "state.flags.span_mapping_done 未正确回写。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
