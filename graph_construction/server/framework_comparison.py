"""Cross-framework comparison helpers for trajectory analysis."""

from __future__ import annotations

import re
import sys
import zlib
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from server.bayesian_analysis import PHASES, analyze_feature_effects
from server.graph_builder import scan_trajectories


POSTERIOR_SAMPLE_COUNT = 4096
SHRINKAGE_PRIOR_STRENGTH = 8.0
STATUS_OUTCOME_KEYS = ("resolved", "unresolved")
FAMILY_ROW_LIMIT = 14


def compare_frameworks(
    datasets: list[dict[str, Any]],
    cmd_parser,
    *,
    status_filter: str = "all",
    feature_type: str = "all",
    min_support: int = 4,
    max_features: int = 24,
    baseline_key: str | None = None,
    focus_key: str | None = None,
    instance_filters: dict[str, set[str]] | None = None,
) -> dict[str, Any]:
    """Compare multiple framework datasets using shared Bayesian summaries."""
    frameworks = []
    framework_records: dict[str, list[dict[str, Any]]] = {}

    for dataset in datasets:
        allowed_instances = (instance_filters or {}).get(dataset["key"])
        records = _load_framework_records(
            dataset,
            status_filter=status_filter,
            instance_filter=allowed_instances,
        )
        analysis = analyze_feature_effects(
            dataset["trajs"],
            dataset.get("eval_report_path"),
            dataset["agent_type"],
            cmd_parser,
            status_filter=status_filter,
            feature_type=feature_type,
            min_support=min_support,
            max_features=max_features,
            instance_filter=allowed_instances,
        )
        framework_records[dataset["key"]] = records
        frameworks.append(_framework_payload(dataset, analysis, records))

    pairwise = _build_pairwise_payload(
        frameworks,
        framework_records=framework_records,
        baseline_key=baseline_key,
        focus_key=focus_key,
        status_filter=status_filter,
    )
    return {
        "summary": {
            "dataset_count": len(frameworks),
            "status_filter": status_filter,
            "feature_type": feature_type,
            "min_support": min_support,
            "max_features": max_features,
            "framework_labels": [framework["label"] for framework in frameworks],
        },
        "frameworks": frameworks,
        "pairwise": pairwise,
    }


def _load_framework_records(
    dataset: dict[str, Any],
    *,
    status_filter: str,
    instance_filter: set[str] | None = None,
) -> list[dict[str, Any]]:
    metadata = scan_trajectories(
        dataset["trajs"],
        dataset.get("eval_report_path"),
        agent_type=dataset["agent_type"],
    )
    if instance_filter is not None:
        metadata = [meta for meta in metadata if meta.get("instance_id") in instance_filter]
    if status_filter != "all":
        metadata = [meta for meta in metadata if meta.get("status") == status_filter]

    rows = []
    for meta in metadata:
        instance_id = meta.get("instance_id", "")
        status = meta.get("status", "none")
        rows.append({
            "instance_id": instance_id,
            "family": _task_family(instance_id),
            "status": status,
            "difficulty": meta.get("difficulty", "unknown"),
            "step_count": int(meta.get("step_count", 0) or 0),
            "resolved": (
                1
                if status == "resolved"
                else 0
                if status == "unresolved"
                else None
            ),
        })
    return rows


def _framework_payload(
    dataset: dict[str, Any],
    analysis: dict[str, Any],
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    usage = analysis.get("command_usage", {})
    usage_summary = usage.get("summary", {})

    trajectory_count = len(records)
    step_count = sum(record.get("step_count", 0) for record in records)
    labeled = sum(1 for record in records if record.get("resolved") is not None)
    resolved = sum(1 for record in records if record.get("resolved") == 1)
    unresolved = sum(1 for record in records if record.get("resolved") == 0)
    resolve_rate = round(resolved / labeled, 4) if labeled else None
    unresolved_rate = round(unresolved / labeled, 4) if labeled else None
    avg_steps = round(step_count / trajectory_count, 2) if trajectory_count else 0.0

    families = {record["family"] for record in records}
    labeled_families = {
        record["family"]
        for record in records
        if record.get("resolved") is not None
    }

    return {
        "key": dataset["key"],
        "label": dataset["label"],
        "agent_type": dataset["agent_type"],
        "summary": {
            "trajectory_count": trajectory_count,
            "step_count": step_count,
            "avg_steps": avg_steps,
            "labeled_trajectories": labeled,
            "resolved_trajectories": resolved,
            "unresolved_trajectories": unresolved,
            "resolve_rate": resolve_rate,
            "unresolved_rate": unresolved_rate,
            "phase_share": usage_summary.get("baseline_phase", _zero_phase_share()),
            "unique_tools": usage_summary.get("unique_tools", 0),
            "unique_commands": usage_summary.get("unique_commands", 0),
            "family_count": len(families),
            "labeled_family_count": len(labeled_families),
        },
        "top_features": analysis.get("features", []),
        "top_tools": usage.get("top_tools", []),
        "top_commands": usage.get("top_commands", []),
    }


def _build_pairwise_payload(
    frameworks: list[dict[str, Any]],
    *,
    framework_records: dict[str, list[dict[str, Any]]],
    baseline_key: str | None,
    focus_key: str | None,
    status_filter: str,
) -> dict[str, Any] | None:
    if len(frameworks) < 2:
        return None

    framework_map = {framework["key"]: framework for framework in frameworks}
    baseline = framework_map.get(baseline_key) if baseline_key else frameworks[0]
    if baseline is None:
        baseline = frameworks[0]

    default_focus = next(
        (framework for framework in frameworks if framework["key"] != baseline["key"]),
        frameworks[0],
    )
    focus = framework_map.get(focus_key) if focus_key else default_focus
    if focus is None or focus["key"] == baseline["key"]:
        focus = default_focus

    baseline_summary = baseline["summary"]
    focus_summary = focus["summary"]

    return {
        "baseline_key": baseline["key"],
        "baseline_label": baseline["label"],
        "focus_key": focus["key"],
        "focus_label": focus["label"],
        "summary_delta": {
            "resolve_rate": _delta(
                focus_summary.get("resolve_rate"),
                baseline_summary.get("resolve_rate"),
            ),
            "unresolved_rate": _delta(
                focus_summary.get("unresolved_rate"),
                baseline_summary.get("unresolved_rate"),
            ),
            "avg_steps": _delta(
                focus_summary.get("avg_steps"),
                baseline_summary.get("avg_steps"),
            ),
            "trajectory_count": (
                focus_summary.get("trajectory_count", 0)
                - baseline_summary.get("trajectory_count", 0)
            ),
            "step_count": (
                focus_summary.get("step_count", 0)
                - baseline_summary.get("step_count", 0)
            ),
        },
        "phase_deltas": {
            phase: _delta(
                focus_summary.get("phase_share", {}).get(phase, 0.0),
                baseline_summary.get("phase_share", {}).get(phase, 0.0),
            )
            for phase in PHASES
        },
        "tool_deltas": _usage_deltas(
            baseline.get("top_tools", []),
            focus.get("top_tools", []),
        ),
        "command_deltas": _usage_deltas(
            baseline.get("top_commands", []),
            focus.get("top_commands", []),
        ),
        "feature_deltas": _feature_deltas(
            baseline.get("top_features", []),
            focus.get("top_features", []),
        ),
        "causal": _build_causal_payload(
            baseline=baseline,
            focus=focus,
            baseline_records=framework_records.get(baseline["key"], []),
            focus_records=framework_records.get(focus["key"], []),
            status_filter=status_filter,
        ),
    }


def _build_causal_payload(
    *,
    baseline: dict[str, Any],
    focus: dict[str, Any],
    baseline_records: list[dict[str, Any]],
    focus_records: list[dict[str, Any]],
    status_filter: str,
) -> dict[str, Any]:
    baseline_map = {record["instance_id"]: record for record in baseline_records}
    focus_map = {record["instance_id"]: record for record in focus_records}

    baseline_ids = set(baseline_map)
    focus_ids = set(focus_map)
    shared_ids = sorted(baseline_ids & focus_ids)
    baseline_only_ids = sorted(baseline_ids - focus_ids)
    focus_only_ids = sorted(focus_ids - baseline_ids)

    shared_pairs = [(baseline_map[instance_id], focus_map[instance_id]) for instance_id in shared_ids]
    shared_labeled = [
        pair
        for pair in shared_pairs
        if pair[0].get("resolved") is not None and pair[1].get("resolved") is not None
    ]

    notes = []
    if status_filter != "all":
        notes.append(
            "Outcome-facing cards are only fully meaningful in the all-trajectories view, "
            "because filtering to a single status conditions on the outcome."
        )
    if not shared_ids:
        notes.append(
            "The selected frameworks do not share any instance IDs under the current filter."
        )

    causal = {
        "coverage": {
            "baseline_total": len(baseline_ids),
            "focus_total": len(focus_ids),
            "shared_total": len(shared_ids),
            "baseline_only_total": len(baseline_only_ids),
            "focus_only_total": len(focus_only_ids),
            "baseline_overlap_share": (
                round(len(shared_ids) / len(baseline_ids), 4) if baseline_ids else None
            ),
            "focus_overlap_share": (
                round(len(shared_ids) / len(focus_ids), 4) if focus_ids else None
            ),
            "shared_labeled_total": len(shared_labeled),
            "shared_family_count": len({pair[0]["family"] for pair in shared_pairs}),
            "labeled_shared_family_count": len({pair[0]["family"] for pair in shared_labeled}),
        },
        "shared_steps": _paired_mean_summary(
            baseline_values=[pair[0]["step_count"] for pair in shared_pairs],
            focus_values=[pair[1]["step_count"] for pair in shared_pairs],
            label="shared_step_count",
        ),
        "matched_outcome": None,
        "discordant_share": None,
        "post_stratified": None,
        "family_strata": [],
        "shrinkage_families": [],
        "coverage_families": _coverage_family_rows(
            baseline_map=baseline_map,
            focus_map=focus_map,
            shared_ids=shared_ids,
            baseline_only_ids=baseline_only_ids,
            focus_only_ids=focus_only_ids,
        ),
        "notes": notes,
    }

    if status_filter != "all" or not shared_labeled:
        if status_filter == "all" and not shared_labeled:
            notes.append(
                "Shared-task outcome cards are unavailable because there are no shared labeled trajectories."
            )
        return causal

    baseline_success = sum(int(pair[0]["resolved"]) for pair in shared_labeled)
    focus_success = sum(int(pair[1]["resolved"]) for pair in shared_labeled)
    total_shared_labeled = len(shared_labeled)

    matched_outcome, _ = _posterior_rate_difference(
        focus_success=focus_success,
        focus_failure=total_shared_labeled - focus_success,
        baseline_success=baseline_success,
        baseline_failure=total_shared_labeled - baseline_success,
        label="matched_resolve_delta",
    )
    matched_outcome["shared_task_count"] = total_shared_labeled
    causal["matched_outcome"] = matched_outcome

    focus_wins = sum(
        1
        for baseline_row, focus_row in shared_labeled
        if baseline_row["resolved"] == 0 and focus_row["resolved"] == 1
    )
    baseline_wins = sum(
        1
        for baseline_row, focus_row in shared_labeled
        if baseline_row["resolved"] == 1 and focus_row["resolved"] == 0
    )
    if focus_wins + baseline_wins:
        discordant_share = _beta_rate_summary(
            successes=focus_wins,
            failures=baseline_wins,
            label="discordant_focus_wins",
        )
        discordant_share.pop("samples", None)
        discordant_share.update({
            "focus_wins": focus_wins,
            "baseline_wins": baseline_wins,
            "discordant_total": focus_wins + baseline_wins,
        })
        causal["discordant_share"] = discordant_share
    else:
        notes.append(
            "Both frameworks have identical resolve outcomes on the shared labeled tasks, "
            "so there are no discordant wins to summarize."
        )

    post_stratified, family_rows = _post_stratified_family_effects(shared_labeled)
    causal["post_stratified"] = post_stratified
    causal["family_strata"] = family_rows
    causal["shrinkage_families"] = _family_shrinkage_rows(shared_labeled)
    return causal


def _post_stratified_family_effects(
    shared_labeled: list[tuple[dict[str, Any], dict[str, Any]]],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if not shared_labeled:
        return None, []

    grouped: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    for baseline_row, focus_row in shared_labeled:
        grouped[baseline_row["family"]].append((baseline_row, focus_row))

    total = len(shared_labeled)
    blended_samples = np.zeros(POSTERIOR_SAMPLE_COUNT)
    rows = []

    for family, pairs in grouped.items():
        family_total = len(pairs)
        baseline_success = sum(int(baseline_row["resolved"]) for baseline_row, _ in pairs)
        focus_success = sum(int(focus_row["resolved"]) for _, focus_row in pairs)
        summary, delta_samples = _posterior_rate_difference(
            focus_success=focus_success,
            focus_failure=family_total - focus_success,
            baseline_success=baseline_success,
            baseline_failure=family_total - baseline_success,
            label=f"family_delta::{family}",
        )
        weight = family_total / total
        blended_samples += weight * delta_samples
        rows.append({
            "family": family,
            "shared_count": family_total,
            "weight": round(weight, 4),
            **summary,
        })

    low, high = np.quantile(blended_samples, [0.05, 0.95])
    summary = {
        "delta_mean": round(float(np.mean(blended_samples)), 4),
        "delta_ci90": [round(float(low), 4), round(float(high), 4)],
        "prob_focus_better": round(float(np.mean(blended_samples > 0)), 4),
        "family_count": len(grouped),
        "shared_task_count": total,
        "weighting": "Shared-task family post-stratification",
    }

    rows.sort(
        key=lambda item: (
            -abs(item.get("delta_mean") or 0.0),
            -item.get("shared_count", 0),
            item["family"],
        )
    )
    return summary, rows[:FAMILY_ROW_LIMIT]


def _family_shrinkage_rows(
    shared_labeled: list[tuple[dict[str, Any], dict[str, Any]]],
) -> list[dict[str, Any]]:
    if not shared_labeled:
        return []

    grouped: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    for baseline_row, focus_row in shared_labeled:
        grouped[baseline_row["family"]].append((baseline_row, focus_row))

    pooled_success = sum(
        int(baseline_row["resolved"]) + int(focus_row["resolved"])
        for baseline_row, focus_row in shared_labeled
    )
    pooled_total = len(shared_labeled) * 2
    pooled_rate = pooled_success / pooled_total if pooled_total else 0.5
    alpha_prior = 1.0 + pooled_rate * SHRINKAGE_PRIOR_STRENGTH
    beta_prior = 1.0 + (1.0 - pooled_rate) * SHRINKAGE_PRIOR_STRENGTH

    rows = []
    for family, pairs in grouped.items():
        family_total = len(pairs)
        baseline_success = sum(int(baseline_row["resolved"]) for baseline_row, _ in pairs)
        focus_success = sum(int(focus_row["resolved"]) for _, focus_row in pairs)

        baseline_summary = _beta_rate_summary(
            successes=baseline_success,
            failures=family_total - baseline_success,
            label=f"shrink_base::{family}",
            alpha_prior=alpha_prior,
            beta_prior=beta_prior,
        )
        focus_summary = _beta_rate_summary(
            successes=focus_success,
            failures=family_total - focus_success,
            label=f"shrink_focus::{family}",
            alpha_prior=alpha_prior,
            beta_prior=beta_prior,
        )
        delta_summary, _ = _posterior_rate_difference(
            focus_success=focus_success,
            focus_failure=family_total - focus_success,
            baseline_success=baseline_success,
            baseline_failure=family_total - baseline_success,
            label=f"shrink_delta::{family}",
            alpha_prior=alpha_prior,
            beta_prior=beta_prior,
        )

        baseline_raw = baseline_success / family_total if family_total else 0.0
        focus_raw = focus_success / family_total if family_total else 0.0

        rows.append({
            "family": family,
            "shared_count": family_total,
            "baseline_raw_rate": round(baseline_raw, 4),
            "focus_raw_rate": round(focus_raw, 4),
            "baseline_rate_mean": baseline_summary["rate_mean"],
            "baseline_rate_ci90": baseline_summary["rate_ci90"],
            "focus_rate_mean": focus_summary["rate_mean"],
            "focus_rate_ci90": focus_summary["rate_ci90"],
            "delta_mean": delta_summary["delta_mean"],
            "delta_ci90": delta_summary["delta_ci90"],
            "prob_focus_better": delta_summary["prob_focus_better"],
            "raw_delta": round(focus_raw - baseline_raw, 4),
        })

    rows.sort(
        key=lambda item: (
            -abs(item.get("delta_mean") or 0.0),
            -item.get("shared_count", 0),
            item["family"],
        )
    )
    return rows[:FAMILY_ROW_LIMIT]


def _coverage_family_rows(
    *,
    baseline_map: dict[str, dict[str, Any]],
    focus_map: dict[str, dict[str, Any]],
    shared_ids: list[str],
    baseline_only_ids: list[str],
    focus_only_ids: list[str],
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "family": "",
        "shared_count": 0,
        "baseline_only_count": 0,
        "focus_only_count": 0,
    })

    for instance_id in shared_ids:
        family = baseline_map[instance_id]["family"]
        grouped[family]["family"] = family
        grouped[family]["shared_count"] += 1

    for instance_id in baseline_only_ids:
        family = baseline_map[instance_id]["family"]
        grouped[family]["family"] = family
        grouped[family]["baseline_only_count"] += 1

    for instance_id in focus_only_ids:
        family = focus_map[instance_id]["family"]
        grouped[family]["family"] = family
        grouped[family]["focus_only_count"] += 1

    rows = []
    for family, item in grouped.items():
        total = (
            item["shared_count"]
            + item["baseline_only_count"]
            + item["focus_only_count"]
        )
        rows.append({
            **item,
            "family": family,
            "total_count": total,
            "imbalance": abs(item["focus_only_count"] - item["baseline_only_count"]),
            "overlap_share": round(item["shared_count"] / total, 4) if total else 0.0,
        })

    rows.sort(
        key=lambda item: (
            -item["total_count"],
            -item["imbalance"],
            item["family"],
        )
    )
    return rows[:FAMILY_ROW_LIMIT]


def _paired_mean_summary(
    *,
    baseline_values: list[int],
    focus_values: list[int],
    label: str,
) -> dict[str, Any] | None:
    if not baseline_values or not focus_values or len(baseline_values) != len(focus_values):
        return None

    baseline_arr = np.asarray(baseline_values, dtype=float)
    focus_arr = np.asarray(focus_values, dtype=float)
    delta_arr = focus_arr - baseline_arr

    if delta_arr.size == 1:
        low = high = float(delta_arr[0])
        prob_focus_smaller = 1.0 if delta_arr[0] < 0 else 0.0
    else:
        rng = np.random.default_rng(_stable_seed(label, delta_arr.size, baseline_arr.sum(), focus_arr.sum()))
        sample_idx = rng.integers(0, delta_arr.size, size=(POSTERIOR_SAMPLE_COUNT, delta_arr.size))
        sampled_means = delta_arr[sample_idx].mean(axis=1)
        low, high = np.quantile(sampled_means, [0.05, 0.95])
        prob_focus_smaller = float(np.mean(sampled_means < 0))

    return {
        "baseline_mean": round(float(np.mean(baseline_arr)), 2),
        "focus_mean": round(float(np.mean(focus_arr)), 2),
        "delta_mean": round(float(np.mean(delta_arr)), 2),
        "delta_ci90": [round(float(low), 2), round(float(high), 2)],
        "prob_focus_smaller": round(prob_focus_smaller, 4),
        "shared_task_count": int(delta_arr.size),
    }


def _posterior_rate_difference(
    *,
    focus_success: int,
    focus_failure: int,
    baseline_success: int,
    baseline_failure: int,
    label: str,
    alpha_prior: float = 1.0,
    beta_prior: float = 1.0,
) -> tuple[dict[str, Any], np.ndarray]:
    focus_summary = _beta_rate_summary(
        successes=focus_success,
        failures=focus_failure,
        label=f"{label}::focus",
        alpha_prior=alpha_prior,
        beta_prior=beta_prior,
    )
    baseline_summary = _beta_rate_summary(
        successes=baseline_success,
        failures=baseline_failure,
        label=f"{label}::baseline",
        alpha_prior=alpha_prior,
        beta_prior=beta_prior,
    )
    delta_samples = focus_summary["samples"] - baseline_summary["samples"]
    low, high = np.quantile(delta_samples, [0.05, 0.95])

    return {
        "focus_rate_mean": focus_summary["rate_mean"],
        "focus_rate_ci90": focus_summary["rate_ci90"],
        "baseline_rate_mean": baseline_summary["rate_mean"],
        "baseline_rate_ci90": baseline_summary["rate_ci90"],
        "delta_mean": round(float(np.mean(delta_samples)), 4),
        "delta_ci90": [round(float(low), 4), round(float(high), 4)],
        "prob_focus_better": round(float(np.mean(delta_samples > 0)), 4),
    }, delta_samples


def _beta_rate_summary(
    *,
    successes: int,
    failures: int,
    label: str,
    alpha_prior: float = 1.0,
    beta_prior: float = 1.0,
) -> dict[str, Any]:
    alpha = successes + alpha_prior
    beta = failures + beta_prior
    rng = np.random.default_rng(_stable_seed(label, successes, failures, alpha_prior, beta_prior))
    samples = rng.beta(alpha, beta, size=POSTERIOR_SAMPLE_COUNT)
    low, high = np.quantile(samples, [0.05, 0.95])
    mean = alpha / (alpha + beta)
    return {
        "rate_mean": round(float(mean), 4),
        "rate_ci90": [round(float(low), 4), round(float(high), 4)],
        "samples": samples,
    }


def _usage_deltas(
    baseline_items: list[dict[str, Any]],
    focus_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    baseline_map = {item["label"]: item for item in baseline_items}
    focus_map = {item["label"]: item for item in focus_items}
    labels = set(baseline_map) | set(focus_map)
    rows = []
    for label in labels:
        baseline_item = baseline_map.get(label)
        focus_item = focus_map.get(label)
        baseline_rate = _nested_mean(baseline_item, "trajectory_rate")
        focus_rate = _nested_mean(focus_item, "trajectory_rate")
        rows.append({
            "label": label,
            "baseline_rate": baseline_rate,
            "focus_rate": focus_rate,
            "delta": _delta(focus_rate, baseline_rate),
            "baseline_support": baseline_item.get("trajectory_support", 0) if baseline_item else 0,
            "focus_support": focus_item.get("trajectory_support", 0) if focus_item else 0,
            "baseline_status_gap": baseline_item.get("status_gap") if baseline_item else None,
            "focus_status_gap": focus_item.get("status_gap") if focus_item else None,
            "baseline_phase": _phase_signature(baseline_item),
            "focus_phase": _phase_signature(focus_item),
        })

    rows.sort(
        key=lambda item: (
            -abs(item["delta"] or 0.0),
            -(item["focus_support"] + item["baseline_support"]),
            item["label"],
        )
    )
    return rows[:12]


def _feature_deltas(
    baseline_items: list[dict[str, Any]],
    focus_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    baseline_map = {item["feature"]: item for item in baseline_items}
    focus_map = {item["feature"]: item for item in focus_items}
    labels = set(baseline_map) | set(focus_map)
    rows = []
    for label in labels:
        baseline_item = baseline_map.get(label)
        focus_item = focus_map.get(label)
        baseline_share = baseline_item.get("trajectory_share", 0.0) if baseline_item else 0.0
        focus_share = focus_item.get("trajectory_share", 0.0) if focus_item else 0.0
        baseline_lift = _nested_lift(baseline_item, "outcome")
        focus_lift = _nested_lift(focus_item, "outcome")
        rows.append({
            "feature": label,
            "label": label,
            "kind": (
                focus_item.get("kind")
                if focus_item
                else baseline_item.get("kind")
                if baseline_item
                else "token"
            ),
            "baseline_share": round(baseline_share, 4),
            "focus_share": round(focus_share, 4),
            "delta_share": _delta(focus_share, baseline_share),
            "baseline_outcome_lift": baseline_lift,
            "focus_outcome_lift": focus_lift,
            "delta_outcome_lift": _delta(focus_lift, baseline_lift),
            "baseline_support": baseline_item.get("trajectory_support", 0) if baseline_item else 0,
            "focus_support": focus_item.get("trajectory_support", 0) if focus_item else 0,
        })

    rows.sort(
        key=lambda item: (
            -(abs(item["delta_share"] or 0.0) + 0.6 * abs(item["delta_outcome_lift"] or 0.0)),
            -(item["focus_support"] + item["baseline_support"]),
            item["feature"],
        )
    )
    return rows[:12]


def _task_family(instance_id: str) -> str:
    match = re.match(r"^(.*)-\d+$", instance_id or "")
    if match:
        return match.group(1)
    return instance_id or "unknown"


def _phase_signature(item: dict[str, Any] | None) -> dict[str, Any] | None:
    if not item:
        return None
    phase = item.get("phase") or {}
    return {
        "dominant_phase": phase.get("dominant_phase"),
        "dominant_delta": phase.get("dominant_delta"),
    }


def _nested_mean(item: dict[str, Any] | None, key: str) -> float | None:
    if not item:
        return None
    value = item.get(key)
    if not isinstance(value, dict):
        return None
    mean = value.get("mean")
    return round(mean, 4) if isinstance(mean, (int, float)) else None


def _nested_lift(item: dict[str, Any] | None, key: str) -> float | None:
    if not item:
        return None
    value = item.get(key)
    if not isinstance(value, dict):
        return None
    lift = value.get("lift_mean")
    return round(lift, 4) if isinstance(lift, (int, float)) else None


def _delta(focus: float | None, baseline: float | None) -> float | None:
    if focus is None or baseline is None:
        return None
    return round(focus - baseline, 4)


def _stable_seed(*parts: Any) -> int:
    blob = "|".join(str(part) for part in parts).encode("utf-8", errors="replace")
    return zlib.adler32(blob) or 1


def _zero_phase_share() -> dict[str, float]:
    return {phase: 0.0 for phase in PHASES}
