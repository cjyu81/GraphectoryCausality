"""Bayesian feature analysis for trajectory text and process patterns."""

from __future__ import annotations

import json
import math
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from server.graph_builder import load_trajectory, scan_trajectories


PHASES = ["localization", "patch", "validation", "general"]
STATUS_OUTCOME_KEYS = ("resolved", "unresolved")
TOKEN_RE = re.compile(r"[a-z][a-z0-9_+\-]{1,31}")
KEEP_SHORT = {"ls", "cd", "mv", "cp", "rm", "sh", "py", "go"}
STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "then",
    "than", "they", "them", "their", "there", "about", "would", "could",
    "should", "need", "needs", "have", "has", "had", "was", "were", "been",
    "being", "just", "also", "while", "where", "when", "what", "which",
    "your", "using", "used", "use", "will", "can", "cannot", "not", "all",
    "any", "but", "you", "are", "our", "out", "its", "it's", "let", "lets",
    "get", "got", "run", "runs", "running", "make", "made", "check",
    "checking", "look", "looking", "step", "steps", "issue", "problem",
    "file", "files", "code", "path", "paths", "here", "into", "over",
    "after", "before", "still", "more", "most", "only", "does", "doesn",
    "did", "done", "like", "likely", "likely", "via", "per",
}


def detect_observation_outcome(observation: str) -> str:
    """Return success/failure/neutral from an observation string."""
    if not observation:
        return "neutral"

    obs_lower = observation.lower()
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

    success_signs = [
        "success",
        "passed",
        "has been edited",
        "created successfully",
    ]
    if any(sign in obs_lower for sign in success_signs):
        return "success"

    return "neutral"


@dataclass
class StepRecord:
    phase: str
    text: str
    observation_outcome: str


def analyze_feature_effects(
    graphs_dir: Path,
    eval_report_path: str | None,
    agent_type: str,
    cmd_parser,
    *,
    status_filter: str = "all",
    feature_type: str = "all",
    min_support: int = 4,
    max_features: int = 40,
) -> dict[str, Any]:
    """Build a Bayesian feature summary across trajectories."""
    metadata = scan_trajectories(graphs_dir, eval_report_path, agent_type=agent_type)
    if status_filter != "all":
        metadata = [m for m in metadata if m.get("status") == status_filter]

    trajectory_rows = []
    for meta in metadata:
        instance_id = meta["instance_id"]
        try:
            traj_data = load_trajectory(graphs_dir, instance_id, agent_type=agent_type)
            steps = _extract_step_records(traj_data, agent_type, cmd_parser)
        except Exception:
            continue
        trajectory_rows.append({
            "instance_id": instance_id,
            "status": meta.get("status", "none"),
            "steps": steps,
        })

    total_trajectories = len(trajectory_rows)
    total_steps = sum(len(row["steps"]) for row in trajectory_rows)
    if not trajectory_rows:
        return {
            "summary": {
                "trajectory_count": 0,
                "step_count": 0,
                "status_filter": status_filter,
                "feature_type": feature_type,
                "min_support": min_support,
                "labeled_trajectories": 0,
                "resolved_trajectories": 0,
                "unresolved_trajectories": 0,
            },
            "features": [],
        }

    feature_stats: dict[str, dict[str, Any]] = defaultdict(_new_feature_stat)
    baseline_next_phase = Counter()
    baseline_observations = Counter()
    baseline_labeled = Counter()

    for row in trajectory_rows:
        steps = row["steps"]
        status = row["status"]
        present_in_traj: set[str] = set()

        if status in STATUS_OUTCOME_KEYS:
            baseline_labeled[status] += 1

        for index, step in enumerate(steps):
            features_here = _feature_set_for_step(step.text)
            next_phase = steps[index + 1].phase if index + 1 < len(steps) else None

            if next_phase:
                baseline_next_phase[next_phase] += 1
            baseline_observations[step.observation_outcome] += 1

            for feature in features_here:
                feature_stats[feature]["occurrence_count"] += 1
                feature_stats[feature]["observation_outcomes"][step.observation_outcome] += 1
                if next_phase:
                    feature_stats[feature]["next_phase_counts"][next_phase] += 1
                present_in_traj.add(feature)

        for feature in present_in_traj:
            feature_stats[feature]["trajectory_support"] += 1
            if status in STATUS_OUTCOME_KEYS:
                feature_stats[feature]["labeled_status_counts"][status] += 1

    eligible = []
    for feature, stat in feature_stats.items():
        kind = "sequence" if " " in feature else "token"
        if feature_type != "all" and kind != feature_type:
            continue
        if stat["trajectory_support"] < min_support:
            continue
        eligible.append((feature, stat))

    resolved_total = baseline_labeled["resolved"]
    unresolved_total = baseline_labeled["unresolved"]
    labeled_total = resolved_total + unresolved_total

    scored = []
    baseline_next_total = sum(baseline_next_phase.values())
    baseline_obs_success = baseline_observations["success"]
    baseline_obs_failure = baseline_observations["failure"]
    baseline_obs_total = baseline_obs_success + baseline_obs_failure
    baseline_next_posterior = _dirichlet_posterior_dict(baseline_next_phase, PHASES)

    for feature, stat in eligible:
        present_resolved = stat["labeled_status_counts"]["resolved"]
        present_unresolved = stat["labeled_status_counts"]["unresolved"]
        present_labeled = present_resolved + present_unresolved
        absent_resolved = max(0, resolved_total - present_resolved)
        absent_unresolved = max(0, unresolved_total - present_unresolved)
        absent_labeled = absent_resolved + absent_unresolved

        outcome = _bayes_binary_summary(
            present_success=present_resolved,
            present_failure=present_unresolved,
            absent_success=absent_resolved,
            absent_failure=absent_unresolved,
            label="resolved",
        )

        step_success = stat["observation_outcomes"]["success"]
        step_failure = stat["observation_outcomes"]["failure"]
        other_success = max(0, baseline_obs_success - step_success)
        other_failure = max(0, baseline_obs_failure - step_failure)
        observation = _bayes_binary_summary(
            present_success=step_success,
            present_failure=step_failure,
            absent_success=other_success,
            absent_failure=other_failure,
            label="successful observation",
        )

        phase_posterior = _dirichlet_posterior_dict(stat["next_phase_counts"], PHASES)
        dominant_phase = "general"
        dominant_delta = 0.0
        phase_deltas = {}
        for phase in PHASES:
            delta = phase_posterior[phase] - baseline_next_posterior[phase]
            phase_deltas[phase] = round(delta, 4)
            if abs(delta) > abs(dominant_delta):
                dominant_phase = phase
                dominant_delta = delta

        process_shift = math.sqrt(sum(delta * delta for delta in phase_deltas.values()))

        item = {
            "feature": feature,
            "kind": "sequence" if " " in feature else "token",
            "label": feature,
            "trajectory_support": stat["trajectory_support"],
            "occurrence_count": stat["occurrence_count"],
            "trajectory_share": round(stat["trajectory_support"] / max(total_trajectories, 1), 4),
            "labeled_trajectory_support": present_labeled,
            "outcome": outcome,
            "observation": observation,
            "process": {
                "next_phase_count": int(sum(stat["next_phase_counts"].values())),
                "dominant_phase": dominant_phase,
                "dominant_delta": round(dominant_delta, 4),
                "shift_magnitude": round(process_shift, 4),
                "posterior": phase_posterior,
                "baseline": baseline_next_posterior,
                "deltas": phase_deltas,
            },
        }
        crude_score = (
            abs(outcome["lift_mean"])
            + 0.7 * abs(observation["lift_mean"])
            + 0.9 * process_shift
        ) * math.log2(stat["trajectory_support"] + 1)
        scored.append((crude_score, item))

    scored.sort(key=lambda pair: (-pair[0], -pair[1]["trajectory_support"], pair[1]["feature"]))
    features = [item for _, item in scored[:max_features]]

    return {
        "summary": {
            "trajectory_count": total_trajectories,
            "step_count": total_steps,
            "status_filter": status_filter,
            "feature_type": feature_type,
            "min_support": min_support,
            "labeled_trajectories": labeled_total,
            "resolved_trajectories": resolved_total,
            "unresolved_trajectories": unresolved_total,
            "baseline_next_phase": baseline_next_posterior,
            "baseline_observation": {
                "success": baseline_obs_success,
                "failure": baseline_obs_failure,
                "total_non_neutral": baseline_obs_total,
            },
            "unique_features_considered": len(eligible),
            "next_phase_events": baseline_next_total,
        },
        "features": features,
    }


def _new_feature_stat() -> dict[str, Any]:
    return {
        "trajectory_support": 0,
        "occurrence_count": 0,
        "labeled_status_counts": Counter(),
        "observation_outcomes": Counter(),
        "next_phase_counts": Counter(),
    }


def _feature_set_for_step(text: str) -> set[str]:
    tokens = _tokenize(text)
    features = set(tokens)
    for left, right in zip(tokens, tokens[1:]):
        features.add(f"{left} {right}")
    return features


def _tokenize(text: str) -> list[str]:
    lowered = text.lower()
    lowered = re.sub(r"```.*?```", " ", lowered, flags=re.DOTALL)
    lowered = re.sub(r"https?://\S+", " ", lowered)
    raw_tokens = TOKEN_RE.findall(lowered)
    tokens = []
    for token in raw_tokens:
        if len(token) < 3 and token not in KEEP_SHORT:
            continue
        if token in STOPWORDS:
            continue
        if token.isdigit():
            continue
        tokens.append(token)
    return tokens


def _dirichlet_posterior_dict(counts: Counter, keys: list[str]) -> dict[str, float]:
    alpha_total = sum(counts.get(key, 0) + 1 for key in keys)
    return {
        key: round((counts.get(key, 0) + 1) / alpha_total, 4)
        for key in keys
    }


def _bayes_binary_summary(
    *,
    present_success: int,
    present_failure: int,
    absent_success: int,
    absent_failure: int,
    label: str,
) -> dict[str, Any]:
    present_total = present_success + present_failure
    absent_total = absent_success + absent_failure

    present_mean, present_ci = _beta_mean_ci(present_success, present_failure)
    absent_mean, absent_ci = _beta_mean_ci(absent_success, absent_failure)

    return {
        "label": label,
        "present_total": present_total,
        "absent_total": absent_total,
        "present_rate_mean": present_mean,
        "present_rate_ci90": present_ci,
        "absent_rate_mean": absent_mean,
        "absent_rate_ci90": absent_ci,
        "lift_mean": round(present_mean - absent_mean, 4),
    }


def _beta_mean_ci(successes: int, failures: int) -> tuple[float, list[float]]:
    alpha = successes + 1
    beta = failures + 1
    mean = alpha / (alpha + beta)
    samples = np.random.default_rng(alpha * 10007 + beta * 97).beta(alpha, beta, size=2048)
    low, high = np.quantile(samples, [0.05, 0.95])
    return round(mean, 4), [round(low, 4), round(high, 4)]


def _extract_step_records(traj_data: dict, agent_type: str, cmd_parser) -> list[StepRecord]:
    if agent_type == "oh":
        return _extract_oh_steps(traj_data, cmd_parser)
    if agent_type == "msa":
        return _extract_msa_steps(traj_data, cmd_parser)
    return _extract_sa_steps(traj_data, cmd_parser)


def _extract_sa_steps(traj_data: dict, cmd_parser) -> list[StepRecord]:
    steps = []
    prev_phases = []
    for step in traj_data.get("trajectory", []):
        thought = step.get("thought", "") or ""
        action = step.get("action", "") or ""
        observation = step.get("observation", "") or ""
        phase = _phase_from_action(action, prev_phases, cmd_parser)
        steps.append(StepRecord(
            phase=phase,
            text=f"{thought}\n{action}".strip(),
            observation_outcome=detect_observation_outcome(observation),
        ))
        prev_phases.append(phase)
    return steps


def _extract_oh_steps(traj_data: dict, cmd_parser) -> list[StepRecord]:
    steps = []
    prev_phases = []
    for step in traj_data.get("history", []):
        obs_type = step.get("observation")
        if obs_type in ("system", "message", None):
            continue

        tool_call_meta = step.get("tool_call_metadata", {})
        model_response = tool_call_meta.get("model_response", {})
        choices = model_response.get("choices", [])

        thought = ""
        action_parts = []
        phase = "general"

        for choice in choices:
            message = choice.get("message", {})
            content = message.get("content") or ""
            if isinstance(content, str):
                thought = f"{thought}\n{content}".strip()
            elif isinstance(content, list):
                text_blocks = [
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                ]
                thought = f"{thought}\n{'\n'.join(text_blocks)}".strip()

            for tool_call in (message.get("tool_calls") or []):
                fn = tool_call.get("function", {})
                name = fn.get("name", "")
                args_raw = fn.get("arguments", "{}")
                action_parts.append(f"{name} {args_raw}".strip())
                if phase == "general":
                    phase = _phase_from_tool_call(name, args_raw, prev_phases, cmd_parser)

        observation = step.get("content", "") or ""
        steps.append(StepRecord(
            phase=phase,
            text=f"{thought}\n{'\n'.join(action_parts)}".strip(),
            observation_outcome=detect_observation_outcome(observation),
        ))
        prev_phases.append(phase)
    return steps


def _extract_msa_steps(traj_data: dict, cmd_parser) -> list[StepRecord]:
    if traj_data.get("trajectory_format") == "mini-swe-agent-1":
        return _extract_msa_v1_steps(traj_data, cmd_parser)

    messages = traj_data.get("messages", [])
    steps = []
    prev_phases = []
    index = 2
    while index < len(messages):
        assistant = messages[index]
        user = messages[index + 1] if index + 1 < len(messages) else {}
        output_blocks = assistant.get("output")
        if not isinstance(output_blocks, list):
            index += 1
            continue

        thought_parts = []
        action_parts = []
        phase = "general"

        for block in output_blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "message":
                thought_parts.append(block.get("content", ""))
            elif block.get("type") == "function_call":
                args_raw = block.get("arguments", "{}")
                action_parts.append(f"{block.get('name', '')} {args_raw}".strip())
                if phase == "general":
                    try:
                        args_json = json.loads(args_raw)
                    except Exception:
                        args_json = {}
                    cmd_str = args_json.get("command", "")
                    phase = _phase_from_action(cmd_str, prev_phases, cmd_parser)

        observation = user.get("content", "") or ""
        steps.append(StepRecord(
            phase=phase,
            text=f"{'\n'.join(thought_parts)}\n{'\n'.join(action_parts)}".strip(),
            observation_outcome=detect_observation_outcome(observation),
        ))
        prev_phases.append(phase)
        index += 2
    return steps


def _extract_msa_v1_steps(traj_data: dict, cmd_parser) -> list[StepRecord]:
    messages = traj_data.get("messages", [])
    steps = []
    prev_phases = []
    index = 2
    while index < len(messages):
        assistant = messages[index]
        user = messages[index + 1] if index + 1 < len(messages) else {}
        content = assistant.get("content", "") or ""
        if assistant.get("role") != "assistant":
            index += 1
            continue

        match = re.search(r"```bash\s*(.*?)```", content, re.DOTALL)
        action = match.group(1).strip() if match else ""
        thought = re.sub(r"```bash\s*.*?```", "", content, flags=re.DOTALL).strip()
        phase = _phase_from_action(action, prev_phases, cmd_parser)

        steps.append(StepRecord(
            phase=phase,
            text=f"{thought}\n{action}".strip(),
            observation_outcome=detect_observation_outcome(user.get("content", "") or ""),
        ))
        prev_phases.append(phase)
        index += 2
    return steps


def _phase_from_action(action: str, prev_phases: list[str], cmd_parser) -> str:
    try:
        from mapPhase import get_phase
    except ImportError:
        return "general"

    if not action.strip() or not cmd_parser:
        return "general"

    parsed = cmd_parser.parse(action)
    if not parsed:
        return "general"

    head = parsed[0]
    return get_phase(
        head.get("tool", ""),
        head.get("subcommand", ""),
        head.get("command", ""),
        head.get("args", {}),
        prev_phases,
        head.get("flags", {}),
    )


def _phase_from_tool_call(name: str, args_raw: str, prev_phases: list[str], cmd_parser) -> str:
    try:
        args = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
    except Exception:
        args = {}

    if name == "execute_bash":
        return _phase_from_action(args.get("command", ""), prev_phases, cmd_parser)

    try:
        from mapPhase import get_phase
    except ImportError:
        return "general"

    args = dict(args)
    subcommand = args.pop("command", None)
    return get_phase(name, subcommand, "", args, prev_phases, {})
