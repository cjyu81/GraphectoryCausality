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
from pathlib import Path

# Ensure parent directory is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from buildGraph import (
    GraphBuilder,
    build_hierarchical_edges,
    determine_resolution_status,
    check_edit_status,
)

# ── Test-outcome helpers (mirrors buildGraph.check_command_outcome) ─────────

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

    # Exception traceback
    for sig in EXCEPTION_SIGNS:
        if sig in obs:
            return "failure"

    # Structured pytest output
    if RE_PYTEST_FAIL.search(obs) or RE_PYTEST_ERROR.search(obs):
        return "failure"
    if RE_PYTEST_PASS.search(obs):
        return "success"
    if "FAILURES" in obs or "ERRORS" in obs or "INTERNALERROR" in obs:
        return "failure"

    return None   # indeterminate – don't mark the node


# ── Directory scanning ──────────────────────────────────────────────────────

def scan_trajectories(graphs_dir: Path,
                      eval_report_path: str | None = None) -> list[dict]:
    """Return a sorted list of trajectory metadata dicts.

    Each dict has: instance_id, status, difficulty, step_count.

    Resolution status is resolved from (in priority order):
      1. The eval_report JSON (resolved_ids / unresolved_ids lists).
      2. A sibling .json sidecar file produced by a previous graph build.
    """
    # Pre-load the eval report once for fast lookup
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

        # ── Resolution status ────────────────────────────────────────────
        if instance_id in resolved_set:
            status = "resolved"
        elif instance_id in unresolved_set:
            status = "unresolved"
        else:
            # Fall back to sidecar .json
            status = "unsubmitted"
            json_file = traj_file.with_suffix(".json")
            if json_file.exists():
                try:
                    with open(json_file) as f:
                        meta = json.load(f)
                    graph_meta = meta.get("graph", {})
                    s = graph_meta.get("resolution_status", "")
                    if s in ("resolved", "unresolved", "unsubmitted"):
                        status = s
                except Exception:
                    pass

        # ── Difficulty ───────────────────────────────────────────────────
        difficulty = "unknown"
        json_file = traj_file.with_suffix(".json")
        if json_file.exists():
            try:
                with open(json_file) as f:
                    meta = json.load(f)
                difficulty = meta.get("graph", {}).get("debug_difficulty", "unknown")
            except Exception:
                pass

        # ── Step count (from the .traj itself) ───────────────────────────
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
    """Build and return a NetworkX MultiDiGraph from *traj_data*.

    Args:
        traj_data:         Raw trajectory dict (must have a "trajectory" key).
        instance_id:       Used for metadata / resolution lookup.
        eval_report_path:  Path string for determine_resolution_status().
        cmd_parser:        CommandParser instance (may be None).
        filter_cd:         When True, strip leading ``cd`` commands from
                           compound actions and mark the node with has_cd.

    Returns:
        Fully-built nx.MultiDiGraph with graph-level metadata attached.
    """
    try:
        from mapPhase import get_phase
    except ImportError:
        def get_phase(*_args, **_kwargs):
            return "general"

    builder    = GraphBuilder()
    trajectory = traj_data.get("trajectory", [])

    for step_idx, step in enumerate(trajectory):
        action_str    = step.get("action", "")
        thought       = step.get("thought", "") or ""
        thought_length = len(thought)

        # ── Pure-think steps (blank action) ────────────────────────────
        if not action_str.strip():
            node_key = builder.add_or_update_node(
                node_label    = "think",
                args          = {"thought_len": thought_length},
                flags         = {},
                phase         = "general",
                step_idx      = step_idx,
                tool          = None,
                command       = None,
                subcommand    = None,
                thought_length = thought_length,
                has_cd        = False,
            )
            builder.add_execution_edge(node_key, step_idx, is_first_in_step=True)
            builder.update_previous_node(node_key)
            builder.add_phase("general")
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

        for parsed in parsed_commands:
            tool       = (parsed.get("tool")       or "").strip()
            subcommand = (parsed.get("subcommand") or "").strip()
            command    = (parsed.get("command")    or "").strip()
            args       = parsed.get("args",  {})
            flags      = parsed.get("flags", {})

            # node_label is the short canonical name for the node.
            # For tool-based actions: "tool: subcommand" (e.g. "str_replace_editor: view")
            # For bare shell commands: just the command verb (first token), not the full string
            if tool and subcommand:
                node_label = f"{tool}: {subcommand}"
            elif tool:
                node_label = tool
            elif command:
                # command may be the full shell action string from the fallback parser;
                # only use the first token (verb) so nodes stay compact.
                node_label = command.split()[0] if command.split() else command
            else:
                # Absolute last resort: first token of the raw action string
                node_label = action_str.strip().split()[0][:30] if action_str.strip() else "action"

            phase = get_phase(tool, subcommand, command, args, builder.prev_phases)

            # ── Determine outcome (edit success/failure + test pass/fail) ──
            observation = step.get("observation", "")
            outcome = check_command_outcome(
                command=command,
                observation=observation,
                tool=tool,
                subcommand=subcommand,
                args=args if isinstance(args, dict) else {},
            )

            # Persist edit_status for str_replace_editor nodes (used by renderer)
            edit_status = check_edit_status(tool, subcommand, args, observation)
            if edit_status and isinstance(args, dict):
                args["edit_status"] = edit_status

            # Store the outcome so the renderer can show ✓/✗ even for shell cmds
            if outcome and isinstance(args, dict):
                args.setdefault("command_outcome", outcome)

            node_key = builder.add_or_update_node(
                node_label    = node_label,
                args          = args,
                flags         = flags,
                phase         = phase,
                step_idx      = step_idx,
                tool          = tool,
                command       = command,
                subcommand    = subcommand,
                thought_length = thought_length,
                has_cd        = has_cd,
            )

            builder.add_execution_edge(node_key, step_idx,
                                       is_first_in_step=is_first_in_step)
            builder.update_previous_node(node_key)
            builder.add_phase(phase)

            # Only the first edge in a step carries the thought length
            is_first_in_step = False

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