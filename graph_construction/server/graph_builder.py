"""
server/graph_builder.py

Responsible for:
  - Scanning the trajectories directory for available instances
  - Loading individual .traj files
  - Building a NetworkX graph from a trajectory (with optional cd filtering)
"""

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

# Ensure parent directory is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from buildGraph import (
    GraphBuilder as _GraphBuilderBase,
    determine_resolution_status,
    check_edit_status,
)

import networkx as nx


# ── Thought-length helpers ──────────────────────────────────────────────────

def compute_thought_length_raw(thought: str) -> int:
    """Raw character count of thought text."""
    return len(thought or "")


def compute_thought_length_clean(thought: str) -> int:
    """Character count excluding text inside quotes/backticks.
    
    Strips content inside:
      - "..."  (double quotes)
      - '...'  (single quotes)
      - `...`  (single backtick)
      - ```...``` (triple backtick)
    """
    import re
    if not thought:
        return 0
    
    # Remove triple backticks first (greedy)
    s = re.sub(r'```.*?```', '', thought, flags=re.DOTALL)
    # Remove single backticks
    s = re.sub(r'`[^`]*`', '', s)
    # Remove double-quoted strings
    s = re.sub(r'"[^"]*"', '', s)
    # Remove single-quoted strings
    s = re.sub(r"'[^']*'", '', s)
    
    return len(s)


# ── Outcome detection helper ────────────────────────────────────────────────

def detect_observation_outcome(observation: str) -> str:
    """Return 'success', 'failure', or 'neutral' based on observation content."""
    if not observation:
        return "neutral"
    
    obs_lower = observation.lower()
    
    # Strong failure indicators
    failure_signs = [
        "traceback (most recent call last)",
        "error:",
        "exception:",
        "failed",
        "failure",
        "assertion",
        "syntaxerror",
        "nameerror",
        "typeerror",
    ]
    if any(sign in obs_lower for sign in failure_signs):
        return "failure"
    
    # Success indicators
    success_signs = [
        "success",
        "passed",
        "ok",
        "has been edited",
        "created successfully",
    ]
    if any(sign in obs_lower for sign in success_signs):
        return "success"
    
    return "neutral"


class GraphBuilder(_GraphBuilderBase):
    """Extends the base GraphBuilder to store thought_length on exec edges."""

    def add_execution_edge(self, node_key: str, step_idx: int,
                           is_first_in_step: bool = False,
                           thought_length_raw: int = 0,
                           thought_length_clean: int = 0):
        if self.previous_node is None:
            return
        self.G.add_edge(
            self.previous_node,
            node_key,
            label=str(step_idx),
            type="exec",
            is_first_in_step=is_first_in_step,
            thought_length_raw=thought_length_raw,
            thought_length_clean=thought_length_clean,
        )

# ── Test-outcome helpers ─────────────────────────────────────────────────────

TEST_COMMANDS = {"python", "python2", "python3", "pytest", "unittest", "nosetests", "tox"}
RE_PYTEST_FAIL  = re.compile(r"\b(\d+)\s+failed\b",  re.IGNORECASE)
RE_PYTEST_ERROR = re.compile(r"\b(\d+)\s+errors?\b", re.IGNORECASE)
RE_PYTEST_PASS  = re.compile(r"\b(\d+)\s+passed\b",  re.IGNORECASE)
EXCEPTION_SIGNS = ["Traceback (most recent call last):"]


def check_command_outcome(command: str, observation: str,
                          tool: str = None, subcommand: str = None,
                          args: dict = None) -> str | None:
    """Return 'success', 'failure', or None for a command + its observation."""
    obs = observation or ""

    # Edit-status from str_replace_editor takes priority
    if tool and subcommand:
        edit_status = check_edit_status(tool, subcommand, args or {}, observation)
        if edit_status and str(edit_status).startswith("failure"):
            return "failure"
        if edit_status == "success":
            return "success"

    for sig in EXCEPTION_SIGNS:
        if sig in obs:
            return "failure"

    if RE_PYTEST_FAIL.search(obs) or RE_PYTEST_ERROR.search(obs):
        return "failure"
    if RE_PYTEST_PASS.search(obs):
        return "success"
    if "FAILURES" in obs or "ERRORS" in obs or "INTERNALERROR" in obs:
        return "failure"

    return None


# ── Local hierarchy builder ──────────────────────────────────────────────────

def build_hierarchical_edges(G, localization_nodes: list) -> None:
    """Add 'hier' edges between str_replace_editor view nodes based on file-path
    containment and view-range nesting.

    Hierarchy rules
    ---------------
    1. Directory containment: if node A views a directory (or file) that is a
       prefix of the path viewed by node B, add A → B.
    2. Range nesting within the same file: if node A views a range [a1, a2] and
       node B views [b1, b2] with b1 >= a1 and b2 <= a2, add A → B.
    3. Whole-file view → ranged view of the same file: if node A has no range
       and node B views a range of the same file, add A → B.
    """
    path_nodes: list[tuple[str, list | None]] = []  # (node_key, view_range_or_None)

    for node_key in localization_nodes:
        data = G.nodes.get(node_key, {})
        args = data.get("args", {}) or {}
        if not isinstance(args, dict):
            continue
        path = args.get("path")
        if not path:
            continue
        vr = args.get("view_range")
        if isinstance(vr, (list, tuple)) and len(vr) == 2:
            try:
                vr = [int(vr[0]), int(vr[1])]
            except (TypeError, ValueError):
                vr = None
        else:
            vr = None
        path_nodes.append((node_key, str(path), vr))

    added: set[tuple] = set()

    def _add(src, dst):
        if src != dst and (src, dst) not in added:
            G.add_edge(src, dst, type="hier", label="")
            added.add((src, dst))

    # Group by normalised path for range comparisons
    by_path: dict[str, list] = defaultdict(list)
    for node_key, path, vr in path_nodes:
        by_path[path].append((node_key, vr))

    for path, entries in by_path.items():
        whole    = [(nk, vr) for nk, vr in entries if vr is None]
        ranged   = [(nk, vr) for nk, vr in entries if vr is not None]

        # Whole-file → ranged views of same file
        for w_nk, _ in whole:
            for r_nk, _ in ranged:
                _add(w_nk, r_nk)

        # Range nesting: outer range → inner range
        for i, (nk_a, vr_a) in enumerate(ranged):
            for j, (nk_b, vr_b) in enumerate(ranged):
                if i == j:
                    continue
                if vr_b[0] >= vr_a[0] and vr_b[1] <= vr_a[1]:
                    _add(nk_a, nk_b)

    # Directory/path prefix containment across different paths
    path_list = list(by_path.keys())
    for path_a in path_list:
        for path_b in path_list:
            if path_a == path_b:
                continue
            parts_a = [p for p in path_a.replace("\\", "/").split("/") if p]
            parts_b = [p for p in path_b.replace("\\", "/").split("/") if p]
            if (len(parts_a) < len(parts_b) and
                    parts_b[:len(parts_a)] == parts_a):
                # path_a is a parent dir of path_b
                for nk_a, _ in by_path[path_a]:
                    for nk_b, _ in by_path[path_b]:
                        _add(nk_a, nk_b)


# ── Directory scanning ──────────────────────────────────────────────────────

def scan_trajectories(graphs_dir: Path,
                      eval_report_path: str | None = None) -> list[dict]:
    """Return a sorted list of trajectory metadata dicts.

    Each dict has: instance_id, status, difficulty, step_count.
    """
    resolved_set:   set[str] = set()
    unresolved_set: set[str] = set()
    if eval_report_path:
        try:
            with open(eval_report_path) as f:
                report = json.load(f)
            resolved_set   = set(report.get("resolved_ids",   []))
            unresolved_set = set(report.get("unresolved_ids", []))
        except Exception:
            pass

    results = []

    for traj_file in sorted(graphs_dir.rglob("*.traj")):
        instance_id = traj_file.stem

        if instance_id in resolved_set:
            status = "resolved"
        elif instance_id in unresolved_set:
            status = "unresolved"
        else:
            status = "unsubmitted"
            json_file = traj_file.with_suffix(".json")
            if json_file.exists():
                try:
                    with open(json_file) as f:
                        meta = json.load(f)
                    s = meta.get("graph", {}).get("resolution_status", "")
                    if s in ("resolved", "unresolved", "unsubmitted"):
                        status = s
                except Exception:
                    pass

        difficulty = "unknown"
        json_file = traj_file.with_suffix(".json")
        if json_file.exists():
            try:
                with open(json_file) as f:
                    meta = json.load(f)
                difficulty = meta.get("graph", {}).get("debug_difficulty", "unknown")
            except Exception:
                pass

        step_count = 0
        try:
            with open(traj_file) as f:
                traj = json.load(f)
            step_count = len(traj.get("trajectory", []))
        except Exception:
            pass

        results.append({
            "instance_id": instance_id,
            "status":      status,
            "difficulty":  difficulty,
            "step_count":  step_count,
        })

    return results


# ── Trajectory loading ──────────────────────────────────────────────────────

def load_trajectory(graphs_dir: Path, instance_id: str) -> dict:
    """Load and return raw trajectory data for *instance_id*.

    Raises FileNotFoundError if the .traj file cannot be found.
    """
    for traj_file in graphs_dir.rglob(f"{instance_id}.traj"):
        with open(traj_file) as f:
            return json.load(f)

    raise FileNotFoundError(
        f"No .traj file found for '{instance_id}' under {graphs_dir}"
    )


# ── Graph construction ──────────────────────────────────────────────────────

def build_graph(traj_data: dict, instance_id: str,
                eval_report_path: str, cmd_parser,
                filter_cd: bool = True):
    """Build and return a NetworkX MultiDiGraph from *traj_data*."""
    try:
        from mapPhase import get_phase
    except ImportError:
        def get_phase(*_args, **_kwargs):
            return "general"

    builder    = GraphBuilder()
    trajectory = traj_data.get("trajectory", [])
    prev_phases_list: list[str] = []

    for step_idx, step in enumerate(trajectory):
        action_str     = step.get("action", "")
        thought        = step.get("thought", "") or ""
        observation    = step.get("observation", "") or ""
        
        # Compute both thought lengths (for user-controlled switch)
        thought_len_raw   = compute_thought_length_raw(thought)
        thought_len_clean = compute_thought_length_clean(thought)

        # ── Pure-think steps (blank action) ────────────────────────────
        if not action_str.strip():
            node_key = builder.add_or_update_node(
                node_label         = "think",
                args               = {"thought_len": thought_len_raw},
                flags              = {},
                phase              = "general",
                step_idx           = step_idx,
                tool               = None,
                command            = None,
                subcommand         = None,
                thought_length     = thought_len_raw,
                has_cd             = False,
            )
            # Store both lengths on the node itself for renderer access
            builder.G.nodes[node_key]["thought_len_raw"]   = thought_len_raw
            builder.G.nodes[node_key]["thought_len_clean"] = thought_len_clean
            
            # Think nodes are always last (and only) in their step
            builder.G.nodes[node_key]["observation_length"] = len(observation)
            builder.G.nodes[node_key]["observation_outcome"] = detect_observation_outcome(observation)
            
            builder.add_execution_edge(
                node_key, step_idx,
                is_first_in_step=True,
                thought_length_raw=thought_len_raw,
                thought_length_clean=thought_len_clean,
            )
            builder.update_previous_node(node_key)
            prev_phases_list.append("general")
            builder.prev_phases.add("general")
            continue

        # ── Parse action string ────────────────────────────────────────
        if cmd_parser is None:
            parsed_commands = _fallback_parse(action_str)
        else:
            parsed_commands = cmd_parser.parse(action_str)

        if not parsed_commands:
            continue

        # ── Optional cd filtering ──────────────────────────────────────
        has_cd = False
        if filter_cd and len(parsed_commands) > 1:
            first = parsed_commands[0]
            if (first.get("command") or "").strip().lower() == "cd":
                has_cd          = True
                parsed_commands = parsed_commands[1:]

        # ── Create nodes / edges ───────────────────────────────────────
        is_first_in_step = True
        node_keys_in_step = []

        for parsed in parsed_commands:
            tool       = (parsed.get("tool")       or "").strip()
            subcommand = (parsed.get("subcommand") or "").strip()
            command    = (parsed.get("command")    or "").strip()
            args       = parsed.get("args",  {})
            flags      = parsed.get("flags", {})

            if tool and subcommand:
                node_label = f"{tool}: {subcommand}"
            elif tool:
                node_label = tool
            elif command:
                node_label = command.split()[0] if command.split() else command
            else:
                node_label = action_str.strip().split()[0][:30] if action_str.strip() else "action"

            phase = get_phase(tool, subcommand, command, args, prev_phases_list)

            outcome = check_command_outcome(
                command=command, observation=observation,
                tool=tool, subcommand=subcommand,
                args=args if isinstance(args, dict) else {},
            )
            edit_status = check_edit_status(tool, subcommand, args, observation)
            if edit_status and isinstance(args, dict):
                args["edit_status"] = edit_status
            if outcome and isinstance(args, dict):
                args.setdefault("command_outcome", outcome)

            node_key = builder.add_or_update_node(
                node_label     = node_label,
                args           = args,
                flags          = flags,
                phase          = phase,
                step_idx       = step_idx,
                tool           = tool,
                command        = command,
                subcommand     = subcommand,
                thought_length = thought_len_raw,
                has_cd         = has_cd,
            )
            
            # Store both thought lengths on node
            builder.G.nodes[node_key]["thought_len_raw"]   = thought_len_raw
            builder.G.nodes[node_key]["thought_len_clean"] = thought_len_clean
            
            node_keys_in_step.append(node_key)

            # First edge in each step carries thought; subsequent intra-step edges carry 0
            builder.add_execution_edge(
                node_key, step_idx,
                is_first_in_step=is_first_in_step,
                thought_length_raw=thought_len_raw if is_first_in_step else 0,
                thought_length_clean=thought_len_clean if is_first_in_step else 0,
            )
            builder.update_previous_node(node_key)
            prev_phases_list.append(phase)
            builder.prev_phases.add(phase)

            is_first_in_step = False

        # ── Mark last node of this step with observation info ─────────
        if node_keys_in_step:
            last_node = node_keys_in_step[-1]
            builder.G.nodes[last_node]["observation_length"] = len(observation)
            builder.G.nodes[last_node]["observation_outcome"] = detect_observation_outcome(observation)

    # ── Post-processing ────────────────────────────────────────────────
    build_hierarchical_edges(builder.G, builder.localization_nodes)

    resolution_status = determine_resolution_status(instance_id, eval_report_path)
    builder.G.graph["resolution_status"] = resolution_status
    builder.G.graph["instance_name"]     = instance_id

    try:
        from buildGraph import difficulty_lookup
        builder.G.graph["debug_difficulty"] = difficulty_lookup.get(instance_id, "unknown")
    except Exception:
        builder.G.graph["debug_difficulty"] = "unknown"

    return builder.G


# ── Fallback parser ────────────────────────────────────────────────────────

def _fallback_parse(action_str: str) -> list[dict]:
    """Minimal parser used when CommandParser is unavailable.

    Splits on ``&&`` and returns one parsed-command dict per part.
    ``command`` holds the first token (verb) for node labelling;
    ``command_full`` holds the entire part for tooltip display.
    ``args`` is a dict with a ``_raw`` key carrying the remainder of the
    command (everything after the verb) so the tooltip can show context.
    """
    results = []
    for part in action_str.split("&&"):
        part = part.strip()
        if not part:
            continue
        tokens = part.split()
        verb   = tokens[0] if tokens else part
        rest   = " ".join(tokens[1:]) if len(tokens) > 1 else ""
        args   = {"_raw": rest} if rest else {}
        results.append({
            "command":    verb,         # short verb only – used for node_label
            "tool":       "",
            "subcommand": "",
            "args":       args,
            "flags":      {},
        })
    return results