from __future__ import annotations

import argparse
import re
import time
from pathlib import Path
from typing import Any

from workflow_common import dump_json, load_json, load_state, save_state


PRINTABLE_CHUNK_RE = re.compile(rb"[ -~]{20,}")
FRAME_RE = re.compile(r"(?P<path>[^:]+\.py)\((?P<line>\d+)\):\s*(?P<symbol>.+)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="构建 python tracer 索引。")
    parser.add_argument("--workspace-dir", required=True)
    return parser


def _extract_printable_chunks(path: Path) -> list[str]:
    if not path.exists():
        return []
    data = path.read_bytes()
    return [chunk.decode("ascii", "ignore").strip() for chunk in PRINTABLE_CHUNK_RE.findall(data)]


def _to_repo_relative(repo_root: Path, frame_path: str) -> str:
    raw = frame_path.replace("\\", "/").strip()
    if "sglang-main/" in raw:
        raw = raw.split("sglang-main/", 1)[1]

    normalized = raw.lstrip("./")
    repo_root_resolved = repo_root.resolve()
    candidates: list[str] = []

    def add_candidate(candidate: str) -> None:
        candidate = candidate.replace("\\", "/").lstrip("./")
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    if Path(normalized).is_absolute():
        try:
            absolute_candidate = Path(normalized).resolve().relative_to(repo_root_resolved)
            add_candidate(str(absolute_candidate).replace("\\", "/"))
        except Exception:
            add_candidate(normalized)
    else:
        add_candidate(normalized)
        if not normalized.startswith("python/"):
            add_candidate(f"python/{normalized}")

    for candidate in candidates:
        if (repo_root / candidate).exists():
            return candidate
    return candidates[0] if candidates else normalized


def build_python_tracer_index_for_workspace(workspace_dir: Path) -> dict[str, Any]:
    stage_start = time.perf_counter()
    state = load_state(workspace_dir)
    artifacts = state["artifacts"]
    repo_root = Path(state["inputs"]["code_repo_path"])
    raw_hash_path = str(artifacts.get("framework_python_tracer_hash_path", "") or "")
    raw_func_path = str(artifacts.get("framework_python_tracer_func_path", "") or "")
    if (not raw_hash_path or not raw_func_path) and (workspace_dir / "input" / "source_inventory.json").exists():
        source_inventory = load_json(workspace_dir / "input" / "source_inventory.json")
        raw_hash_path = raw_hash_path or str(source_inventory.get("framework_python_tracer_hash_path", "") or "")
        raw_func_path = raw_func_path or str(source_inventory.get("framework_python_tracer_func_path", "") or "")
        artifacts["framework_python_tracer_hash_path"] = raw_hash_path
        artifacts["framework_python_tracer_func_path"] = raw_func_path
    hash_path = Path(raw_hash_path) if raw_hash_path else None
    func_path = Path(raw_func_path) if raw_func_path else None

    frames: list[dict[str, Any]] = []
    frame_seen: set[tuple[str, int, str]] = set()
    warnings: list[str] = []

    for source_label, source_path in [("hash", hash_path), ("func", func_path)]:
        if source_path is None:
            continue
        print(
            f"[build_python_tracer_index] 开始读取 {source_label} tracer 文件: {source_path}",
            flush=True,
        )
        source_start = time.perf_counter()
        for chunk in _extract_printable_chunks(source_path):
            match = FRAME_RE.search(chunk)
            if not match:
                continue
            frame_path = match.group("path").strip()
            symbol = match.group("symbol").strip()
            line = int(match.group("line"))
            repo_relative_path = _to_repo_relative(repo_root, frame_path)
            candidate_repo_file = repo_root / repo_relative_path
            frame_class = "repo_python" if candidate_repo_file.exists() else "external_python"
            frame_key = (repo_relative_path, line, symbol)
            if frame_key in frame_seen:
                continue
            frame_seen.add(frame_key)
            frames.append(
                {
                    "frame_id": f"ptf_{len(frames):06d}",
                    "source": source_label,
                    "raw_path": frame_path,
                    "repo_relative_path": repo_relative_path,
                    "line": line,
                    "symbol": symbol,
                    "frame_class": frame_class,
                }
            )
        print(
            f"[build_python_tracer_index] 完成读取 {source_label} tracer 文件，耗时={time.perf_counter() - source_start:.2f}s",
            flush=True,
        )

    repo_frames = [frame for frame in frames if frame["frame_class"] == "repo_python"]
    parse_status = "passed"
    if (hash_path is None or not hash_path.exists()) and (func_path is None or not func_path.exists()):
        parse_status = "missing"
        warnings.append("未找到 torch.python_tracer_hash / torch.python_tracer_func，已生成占位索引。")
    elif not repo_frames:
        parse_status = "partial"
        warnings.append("已读取 tracer 原始文件，但未解析出 repo 内 frame。")

    output = {
        "schema_version": "python_tracer_index_v1",
        "status": parse_status,
        "sources": {
            "hash_path": str(hash_path) if hash_path else "",
            "func_path": str(func_path) if func_path else "",
            "hash_file_present": bool(hash_path and hash_path.exists()),
            "func_file_present": bool(func_path and func_path.exists()),
        },
        "frames": frames,
        "stats": {
            "total_frame_count": len(frames),
            "repo_frame_count": len(repo_frames),
        },
        "warnings": warnings,
    }

    output_path = workspace_dir / "artifacts" / "stacks" / "python_tracer_index.json"
    print("[build_python_tracer_index] 开始写出 python_tracer_index.json", flush=True)
    dump_json(output_path, output)
    artifacts["python_tracer_index_path"] = str(output_path)
    state["flags"]["python_tracer_index_built"] = True
    save_state(workspace_dir, state)
    print(
        f"[build_python_tracer_index] 完成，耗时={time.perf_counter() - stage_start:.2f}s "
        f"(total_frames={len(frames)}, repo_frames={len(repo_frames)}, status={parse_status})",
        flush=True,
    )
    return output


def main() -> int:
    args = build_parser().parse_args()
    build_python_tracer_index_for_workspace(Path(args.workspace_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
