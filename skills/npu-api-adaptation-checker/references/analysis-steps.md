# Analysis Steps

## Step 1: Detect Changed Parameters via Git

Use git to detect parameter changes from **two sources**: local uncommitted changes and recent community commits.

### 1a. Local Uncommitted Changes

```bash
git diff -- python/sglang/srt/server_args.py
git diff --cached -- python/sglang/srt/server_args.py
```

### 1b. Recent Community Commits

Find when the NPU support features doc was last updated, then check all ServerArgs changes since that commit:

```bash
# Find the last commit that modified the NPU support features doc (prefer new docs, fallback to old)
LAST_NPU_COMMIT=$(git log -1 --format="%H" -- docs_new/docs/hardware-platforms/ascend-npus/ascend_npu_support_features.mdx || git log -1 --format="%H" -- docs/platforms/ascend/ascend_npu_support_features.md)

# Check ServerArgs changes since that commit
git diff "${LAST_NPU_COMMIT}"..HEAD -- python/sglang/srt/server_args.py
```

Alternatively, check changes over a specific time range (e.g., last 30 days):

```bash
# Cross-platform date calculation:
# Linux: SINCE_DATE=$(date -d '30 days ago' +%Y-%m-%d)
# macOS: SINCE_DATE=$(date -v-30d +%Y-%m-%d)
# Use git's built-in relative date syntax instead (works on all platforms):
git diff $(git log -1 --format="%H" --before="2025-01-01")..HEAD -- python/sglang/srt/server_args.py

# Or simply use --since with git log (most reliable cross-platform approach):
git log --oneline --since="30 days ago" -- python/sglang/srt/server_args.py
```

Or list individual commits that touched ServerArgs files:

```bash
git log --oneline --since="30 days ago" -- python/sglang/srt/server_args.py
```

### 1c. Parse Diff Output

From all diff outputs, identify:
1. **Newly added fields**: Lines starting with `+` that define new dataclass fields (pattern: `+    field_name: type = ...`)
2. **Removed fields**: Lines starting with `-` that remove dataclass fields (pattern: `-    field_name: type = ...`)
3. **Changed defaults**: Lines where a field's default value was modified

**Important**: Filter out non-field changes (imports, methods, comments, whitespace). Only focus on dataclass field definitions.

Tag each detected change with its source:
- **Local**: from uncommitted `git diff` / `git diff --cached`
- **Community**: from `git diff <commit>..HEAD` or time-range diff

## Step 2: Extract All ServerArgs Fields

Read the ServerArgs file and extract all fields from the `ServerArgs` dataclass:

1. `python/sglang/srt/server_args.py` — ServerArgs definition

Each field is a server argument. Group them by category (the comments in the dataclass indicate categories like "Model and tokenizer", "HTTP server", "Quantization and data type", etc.).

## Step 3: Extract NPU-Supported Arguments

**Document priority**: Read the new NPU support features document first (`docs_new/docs/hardware-platforms/ascend-npus/ascend_npu_support_features.mdx`). If it does not exist or lacks necessary tables, fall back to the old doc (`docs/platforms/ascend/ascend_npu_support_features.md`).

Parse the markdown tables. The "Server supported" column indicates the adaptation status:
- **A2, A3** — Fully supported on both Atlas 800I A2 and A3
- **Planned** — Planned but not yet implemented
- **Special For GPU** — GPU-specific, not applicable to NPU
- **Experimental** — Experimental support on NPU

**Note**: The NPU support features doc covers parameters defined in `python/sglang/srt/server_args.py`. Any parameter not listed in the doc should be marked as "Not in NPU documentation" until manually assessed.

## Step 4: Compare and Identify Differences

Compare the full list of ServerArgs fields against the parsed NPU support features to identify:
1. **New API parameters**: Fields in ServerArgs that are NOT listed in the NPU support features doc
2. **Removed API parameters**: Arguments in the NPU support features doc that no longer exist in any ServerArgs
3. **Status changes**: Arguments whose NPU support status has changed

Cross-reference with Step 1 results to distinguish:
- **Local Change**: from uncommitted git diff — the user's own modifications
- **Community Change**: from recent commits since NPU doc was last updated — new/deprecated parameters from the community
- **Previously Missing**: already in the codebase but never documented in the NPU doc

## Step 5: Categorize New Parameters

For each new parameter not in the NPU doc, determine its likely NPU adaptation status:
- If the parameter is GPU-specific (e.g., CUDA graph, NVIDIA-specific quantization), mark as "Special For GPU"
- If the parameter is platform-agnostic (e.g., scheduling, logging), mark as "Likely A2, A3"
- If the parameter involves NPU-specific backends, check `_handle_npu_backends()` in the corresponding ServerArgs file for NPU-specific handling

**Note**: All NPU adaptation code for server arguments should be placed in `_handle_npu_backends()` (in `python/sglang/srt/server_args.py`) or `set_default_server_args()` (in `python/sglang/srt/hardware_backend/npu/utils.py`).

## Step 6: Auto-Adapt Simple Parameters

For each new parameter, classify it into one of three adaptation categories and take the corresponding action:

### Category 1: Auto-Adaptable (direct code generation)

These parameters can be adapted by adding a single assignment in `set_default_server_args()` (in `python/sglang/srt/hardware_backend/npu/utils.py`) or `_handle_npu_backends()` (in `python/sglang/srt/server_args.py`). Patterns include:

| Pattern | Example | Generated Code |
|---------|---------|----------------|
| Force-disable CUDA-exclusive feature | `--enable-nccl-nvls`, `--enable-symm-mem`, `--enable-mscclpp` | `args.<param_name> = False` or `args.<param_name> = True` (disable flag) |
| Force-set backend to NPU value | `--fp8-gemm-runner-backend` | `args.<param_name> = "<npu_value>"` |
| Force-disable incompatible feature | `--disable-custom-all-reduce` | `args.<param_name> = True` |
| Set NPU-specific default | `--page-size` | `if args.<param_name> is None: args.<param_name> = <npu_default>` |

**Action**: Directly generate the adaptation code and add it to the appropriate function. Use `set_default_server_args()` for simple defaults and `_handle_npu_backends()` for logic that requires conditional checks.

**Naming heuristics for auto-detection**:
- Parameter name contains `cuda`, `nccl`, `nvls`, `triton`, `flashinfer`, `cutlass`, `flashmla` → likely Category 1 (force-disable or force-set)
- Parameter name contains `gpu`, `nvidia` → likely Category 1 (force-disable)
- Parameter is a boolean `enable_*` flag for a GPU-only feature → Category 1 (set to `False`)
- Parameter is a boolean `disable_*` flag for a GPU-only feature → Category 1 (set to `True`)

### Category 2: Conditional (generate template with TODO)

These parameters need runtime conditions (hardware model, model architecture, other parameter values). Patterns include:

| Pattern | Example | Generated Code |
|---------|---------|----------------|
| Depends on NPU memory size | `--chunked-prefill-size` | `if args.<param> is None: args.<param> = <value_based_on_npu_mem>` |
| Depends on model architecture | `--hicache-mem-layout` | `if args.use_mla_backend(): args.<param> = ...` |
| Depends on tp_size | `--cuda-graph-max-bs` | `if args.tp_size < 4: args.<param> = ...` |

**Action**: Generate code template with `# TODO: verify on NPU` comment. The user must verify the correct values.

### Category 3: Not Auto-Adaptable (kernel dependency)

These parameters require NPU kernel implementations that may not exist. Patterns include:

| Pattern | Example |
|---------|---------|
| Requires specific GPU kernel | `--nsa-prefill-backend=flashmla_sparse` |
| Third-party GPU-only library | `--quantization-param-path`, `--modelopt-*` |
| GPU hardware feature | `--enable-nccl-nvls`, `--fp4-gemm-runner-backend` |

**Action**: Mark as "Special For GPU" in the NPU support features doc. Do not generate adaptation code.

### Auto-Adaptation Code Generation

For Category 1 and 2 parameters, generate the code to be added to `set_default_server_args()`:

```python
# In python/sglang/srt/hardware_backend/npu/utils.py, function set_default_server_args()

# --- Auto-adapted parameters (generated by npu-api-adaptation-checker) ---
# Category 1: Auto-adaptable
args.<param_name> = <value>  # <reason>

# Category 2: Conditional (needs verification)
# TODO: verify on NPU — <reason for conditional>
if args.<param_name> is None:
    args.<param_name> = <suggested_value>
```

Or for parameters that belong in `_handle_npu_backends()` (when the logic involves conditional checks beyond simple defaults):

```python
# In python/sglang/srt/server_args.py, function _handle_npu_backends()

# --- Auto-adapted parameters (generated by npu-api-adaptation-checker) ---
if self.device == "npu":
    self.<param_name> = <value>  # <reason>
```

**Important**: After generating adaptation code, also update the NPU support features doc to add the new parameter with its status. Prefer updating `docs_new/docs/hardware-platforms/ascend-npus/ascend_npu_support_features.mdx`; fall back to `docs/platforms/ascend/ascend_npu_support_features.md` if the new doc does not exist.

## Step 7: Check NPU Code Paths

For new parameters, search the codebase for NPU-specific handling:
- Search for `is_npu()` checks related to the parameter
- Search in `python/sglang/srt/hardware_backend/npu/` for NPU-specific implementations
- Check `python/sglang/srt/platforms/` for platform-specific default handling