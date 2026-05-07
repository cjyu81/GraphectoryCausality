"""
HTTP request handler for the trajectory browser and framework comparison UI.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from server.bayesian_analysis import analyze_feature_effects
from server.framework_comparison import compare_frameworks
from server.graph_builder import build_graph, load_trajectory, scan_trajectories
from server.graph_renderer import render_graph_html

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

_MIME: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
}

_OVERLAP_THRESHOLD = 0.05
_MAX_SANKEY_TRAJECTORIES = 160
_MAX_BAYES_TRAJECTORIES = 120
_MAX_COMPARE_TRAJECTORIES_PER_DATASET = 100


@dataclass(frozen=True)
class DatasetConfig:
    key: str
    label: str
    trajs: Path
    eval_report_path: str | None
    agent_type: str

    def to_public_dict(self, *, is_primary: bool) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "trajs": str(self.trajs),
            "eval_report": self.eval_report_path or "",
            "agent_type": self.agent_type,
            "is_primary": is_primary,
        }

    def to_runtime_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "trajs": self.trajs,
            "eval_report_path": self.eval_report_path,
            "agent_type": self.agent_type,
        }


class GraphHandler(BaseHTTPRequestHandler):
    """Thread-safe HTTP handler with multi-framework dataset support."""

    graphs_dir: Path = None
    agent_type: str = "sa"
    eval_report_path: str = None
    cmd_parser = None
    assets_dir: Path = None

    _cache_lock: threading.RLock = threading.RLock()
    _datasets: list[DatasetConfig] = []
    _primary_dataset_key: str | None = None
    _graphs_cache: dict[str, list[dict[str, Any]]] = {}
    _render_cache: dict[tuple[Any, ...], str] = {}
    _sankey_cache: dict[str, dict[str, Any]] = {}
    _bayes_cache: dict[tuple[Any, ...], dict[str, Any]] = {}
    _compare_cache: dict[tuple[Any, ...], dict[str, Any]] = {}

    def log_message(self, fmt, *args):
        status = args[1] if len(args) > 1 else "?"
        logger.info("%s %s -> %s", self.command, self.path, status)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        try:
            if path in ("/", "/index.html"):
                self._send_file(STATIC_DIR / "index.html")
            elif path.startswith("/static/"):
                self._send_file(STATIC_DIR / path[len("/static/"):])
            elif path == "/api/graphs":
                self._api_graphs(dataset_key=params.get("dataset", [""])[0] or None)
            elif path == "/api/graph":
                instance_id = params.get("id", [""])[0]
                if not instance_id:
                    self._error(400, "Missing required query parameter: id")
                    return
                self._api_graph(
                    instance_id=instance_id,
                    dataset_key=params.get("dataset", [""])[0] or None,
                    filter_cd=_bool_param(params, "filter_cd", default=False),
                    thought_quotes=_bool_param(params, "thought_quotes", default=True),
                    node_verbosity=_bool_param(params, "node_verbosity", default=True),
                    show_observation=_bool_param(params, "show_observation", default=False),
                    unique_think=_bool_param(params, "unique_think", default=True),
                )
            elif path == "/api/sankey":
                self._api_sankey(dataset_key=params.get("dataset", [""])[0] or None)
            elif path == "/api/bayes":
                self._api_bayes(
                    dataset_key=params.get("dataset", [""])[0] or None,
                    status_filter=params.get("status", ["all"])[0],
                    feature_type=params.get("feature_type", ["all"])[0],
                    min_support=_int_param(params, "min_support", default=4, minimum=1, maximum=200),
                    max_features=_int_param(params, "max_features", default=40, minimum=5, maximum=200),
                )
            elif path == "/api/compare":
                self._api_compare(
                    status_filter=params.get("status", ["all"])[0],
                    feature_type=params.get("feature_type", ["all"])[0],
                    min_support=_int_param(params, "min_support", default=4, minimum=1, maximum=200),
                    max_features=_int_param(params, "max_features", default=24, minimum=5, maximum=200),
                    baseline_key=params.get("baseline", [""])[0] or None,
                    focus_key=params.get("focus", [""])[0] or None,
                )
            elif path == "/api/config":
                self._api_get_config()
            else:
                self._error(404, "Not found")
        except Exception as exc:
            logger.exception("[handler] Unhandled error for GET %s", self.path)
            self._error(500, str(exc))

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        try:
            if path == "/api/config":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                data = json.loads(body)
                self._api_post_config(data)
            else:
                self._error(404, "Not found")
        except Exception as exc:
            logger.exception("[handler] Unhandled error for POST %s", self.path)
            self._error(500, str(exc))

    @classmethod
    def _ensure_dataset_state_locked(cls):
        if cls._datasets:
            if not cls._primary_dataset_key or not any(
                dataset.key == cls._primary_dataset_key for dataset in cls._datasets
            ):
                cls._primary_dataset_key = cls._datasets[0].key
            cls._sync_primary_legacy_fields_locked()
            return

        if not cls.graphs_dir:
            return

        trajs = cls.graphs_dir if isinstance(cls.graphs_dir, Path) else Path(cls.graphs_dir)
        agent_type = cls.agent_type or _infer_agent_type(trajs) or "sa"
        dataset = DatasetConfig(
            key="primary",
            label=_default_dataset_label(trajs, agent_type),
            trajs=trajs,
            eval_report_path=cls.eval_report_path,
            agent_type=agent_type,
        )
        cls._datasets = [dataset]
        cls._primary_dataset_key = dataset.key
        cls._sync_primary_legacy_fields_locked()

    @classmethod
    def _sync_primary_legacy_fields_locked(cls):
        dataset = None
        if cls._datasets:
            target_key = cls._primary_dataset_key or cls._datasets[0].key
            dataset = next((item for item in cls._datasets if item.key == target_key), cls._datasets[0])
        if dataset is None:
            cls.graphs_dir = None
            cls.agent_type = "sa"
            cls.eval_report_path = None
            return
        cls.graphs_dir = dataset.trajs
        cls.agent_type = dataset.agent_type
        cls.eval_report_path = dataset.eval_report_path

    @classmethod
    def _get_dataset_locked(cls, dataset_key: str | None = None) -> DatasetConfig | None:
        cls._ensure_dataset_state_locked()
        if not cls._datasets:
            return None
        target_key = dataset_key or cls._primary_dataset_key or cls._datasets[0].key
        for dataset in cls._datasets:
            if dataset.key == target_key:
                return dataset
        return None

    @classmethod
    def _dataset_public_list_locked(cls) -> list[dict[str, Any]]:
        cls._ensure_dataset_state_locked()
        return [
            dataset.to_public_dict(is_primary=dataset.key == cls._primary_dataset_key)
            for dataset in cls._datasets
        ]

    @classmethod
    def _dataset_runtime_list_locked(cls) -> list[dict[str, Any]]:
        cls._ensure_dataset_state_locked()
        return [dataset.to_runtime_dict() for dataset in cls._datasets]

    @classmethod
    def _flush_caches_locked(cls):
        cls._graphs_cache = {}
        cls._render_cache = {}
        cls._sankey_cache = {}
        cls._bayes_cache = {}
        cls._compare_cache = {}

    def _api_get_config(self):
        with self._cache_lock:
            GraphHandler._ensure_dataset_state_locked()
            primary = GraphHandler._get_dataset_locked()
            datasets = GraphHandler._dataset_public_list_locked()

        self._respond_json({
            "trajs": str(primary.trajs) if primary else "",
            "eval_report": primary.eval_report_path if primary and primary.eval_report_path else "",
            "agent_type": primary.agent_type if primary else self.agent_type,
            "primary_dataset_key": primary.key if primary else "",
            "datasets": datasets,
        })

    def _api_post_config(self, data: dict[str, Any]):
        raw_datasets = data.get("datasets")
        if isinstance(raw_datasets, list):
            dataset_payloads = raw_datasets
        else:
            dataset_payloads = [{
                "key": data.get("key") or "primary",
                "label": data.get("label") or "",
                "trajs": data.get("trajs") or "",
                "eval_report": data.get("eval_report") or "",
            }]

        validated: list[DatasetConfig] = []
        used_keys: set[str] = set()
        for index, payload in enumerate(dataset_payloads):
            if not isinstance(payload, dict):
                self._error(400, f"Framework entry {index + 1} must be an object.")
                return
            dataset, error = _validate_dataset_payload(payload, index=index, used_keys=used_keys)
            if error:
                self._error(400, error)
                return
            validated.append(dataset)

        if not validated:
            self._error(400, "At least one framework dataset is required.")
            return

        primary_key = (data.get("primary_dataset_key") or "").strip() or validated[0].key
        if primary_key not in {dataset.key for dataset in validated}:
            self._error(400, f"Unknown primary dataset key: {primary_key}")
            return

        with self._cache_lock:
            GraphHandler._datasets = validated
            GraphHandler._primary_dataset_key = primary_key
            GraphHandler._sync_primary_legacy_fields_locked()
            GraphHandler._flush_caches_locked()
            primary = GraphHandler._get_dataset_locked()
            datasets = GraphHandler._dataset_public_list_locked()

        self._respond_json({
            "ok": True,
            "trajs": str(primary.trajs) if primary else "",
            "eval_report": primary.eval_report_path if primary and primary.eval_report_path else "",
            "agent_type": primary.agent_type if primary else self.agent_type,
            "primary_dataset_key": primary.key if primary else "",
            "datasets": datasets,
        })

    def _api_graphs(self, *, dataset_key: str | None):
        with self._cache_lock:
            dataset = GraphHandler._get_dataset_locked(dataset_key)
            if dataset is None:
                self._respond_json([])
                return
            cached = GraphHandler._graphs_cache.get(dataset.key)
        if cached is not None:
            self._respond_json(cached)
            return

        graphs = scan_trajectories(
            dataset.trajs,
            dataset.eval_report_path,
            agent_type=dataset.agent_type,
        )

        with self._cache_lock:
            GraphHandler._graphs_cache[dataset.key] = graphs

        self._respond_json(graphs)

    def _api_graph(
        self,
        *,
        instance_id: str,
        dataset_key: str | None,
        filter_cd: bool,
        thought_quotes: bool,
        node_verbosity: bool,
        show_observation: bool,
        unique_think: bool,
    ):
        with self._cache_lock:
            dataset = GraphHandler._get_dataset_locked(dataset_key)
            if dataset is None:
                self._error(400, "No dataset is configured.")
                return

        cache_key = (
            dataset.key,
            instance_id,
            filter_cd,
            thought_quotes,
            node_verbosity,
            show_observation,
            unique_think,
        )

        with self._cache_lock:
            cached = GraphHandler._render_cache.get(cache_key)
        if cached is not None:
            self._respond(200, "text/html; charset=utf-8", cached.encode("utf-8"))
            return

        traj_data = load_trajectory(dataset.trajs, instance_id, agent_type=dataset.agent_type)
        graph = build_graph(
            traj_data=traj_data,
            instance_id=instance_id,
            eval_report_path=dataset.eval_report_path,
            cmd_parser=self.cmd_parser,
            graphs_dir=dataset.trajs,
            filter_cd=filter_cd,
            agent_type=dataset.agent_type,
            unique_think=unique_think,
        )
        html = render_graph_html(
            graph,
            filter_cd,
            thought_quotes,
            node_verbosity,
            show_observation,
            self.assets_dir,
        )

        with self._cache_lock:
            GraphHandler._render_cache[cache_key] = html

        self._respond(200, "text/html; charset=utf-8", html.encode("utf-8"))

    def _api_sankey(self, *, dataset_key: str | None):
        with self._cache_lock:
            dataset = GraphHandler._get_dataset_locked(dataset_key)
            if dataset is None:
                self._respond_json({"trajectories": []})
                return
            cached = GraphHandler._sankey_cache.get(dataset.key)
        if cached is not None:
            self._respond_json(cached)
            return

        graphs = self._get_graphs_for_dataset(dataset)
        selected_graphs, subset_notice = _limit_graph_subset(
            graphs,
            limit=_MAX_SANKEY_TRAJECTORIES,
            label=dataset.label,
        )
        trajectories = []
        for meta in selected_graphs:
            instance_id = meta["instance_id"]
            try:
                traj_data = load_trajectory(dataset.trajs, instance_id, agent_type=dataset.agent_type)
                phases = _extract_phase_sequence(traj_data, dataset.agent_type, self.cmd_parser)
            except Exception as exc:
                logger.warning("[sankey] Skipping %s in %s: %s", instance_id, dataset.label, exc)
                phases = []
            trajectories.append({
                "instance_id": instance_id,
                "status": meta.get("status", "none"),
                "phases": phases,
            })

        result = {
            "trajectories": trajectories,
            "summary": {
                "trajectory_count": len(trajectories),
                "subset_notice": subset_notice,
                "truncated": bool(subset_notice),
            },
        }
        with self._cache_lock:
            GraphHandler._sankey_cache[dataset.key] = result
        self._respond_json(result)

    def _api_bayes(
        self,
        *,
        dataset_key: str | None,
        status_filter: str,
        feature_type: str,
        min_support: int,
        max_features: int,
    ):
        with self._cache_lock:
            dataset = GraphHandler._get_dataset_locked(dataset_key)
            if dataset is None:
                self._respond_json({
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
                    "command_usage": {
                        "summary": {
                            "trajectory_count": 0,
                            "step_count": 0,
                            "unique_tools": 0,
                            "unique_commands": 0,
                        },
                        "top_tools": [],
                        "top_commands": [],
                    },
                })
                return
            cache_key = (dataset.key, status_filter, feature_type, min_support, max_features)
            cached = GraphHandler._bayes_cache.get(cache_key)
        if cached is not None:
            self._respond_json(cached)
            return

        graphs = self._get_graphs_for_dataset(dataset)
        selected_graphs, subset_notice = _limit_graph_subset(
            graphs,
            limit=_MAX_BAYES_TRAJECTORIES,
            label=dataset.label,
        )
        selected_ids = {meta["instance_id"] for meta in selected_graphs}

        result = analyze_feature_effects(
            dataset.trajs,
            dataset.eval_report_path,
            dataset.agent_type,
            self.cmd_parser,
            status_filter=status_filter,
            feature_type=feature_type,
            min_support=min_support,
            max_features=max_features,
            instance_filter=selected_ids,
        )
        result.setdefault("summary", {})
        result["summary"]["subset_notice"] = subset_notice
        result["summary"]["truncated"] = bool(subset_notice)
        result["summary"]["input_trajectory_count"] = len(graphs)
        result["summary"]["used_trajectory_count"] = len(selected_graphs)

        with self._cache_lock:
            GraphHandler._bayes_cache[cache_key] = result

        self._respond_json(result)

    def _api_compare(
        self,
        *,
        status_filter: str,
        feature_type: str,
        min_support: int,
        max_features: int,
        baseline_key: str | None,
        focus_key: str | None,
    ):
        with self._cache_lock:
            datasets = GraphHandler._dataset_runtime_list_locked()
            cache_key = (
                tuple((dataset["key"], str(dataset["trajs"]), dataset["eval_report_path"] or "") for dataset in datasets),
                status_filter,
                feature_type,
                min_support,
                max_features,
                baseline_key or "",
                focus_key or "",
            )
            cached = GraphHandler._compare_cache.get(cache_key)
        if cached is not None:
            self._respond_json(cached)
            return

        instance_filters: dict[str, set[str]] = {}
        subset_notes: list[str] = []
        for dataset in datasets:
            graphs = self._get_graphs_for_dataset(DatasetConfig(
                key=dataset["key"],
                label=dataset["label"],
                trajs=dataset["trajs"],
                eval_report_path=dataset["eval_report_path"],
                agent_type=dataset["agent_type"],
            ))
            selected_graphs, subset_notice = _limit_graph_subset(
                graphs,
                limit=_MAX_COMPARE_TRAJECTORIES_PER_DATASET,
                label=dataset["label"],
            )
            instance_filters[dataset["key"]] = {meta["instance_id"] for meta in selected_graphs}
            if subset_notice:
                subset_notes.append(subset_notice)

        result = compare_frameworks(
            datasets,
            self.cmd_parser,
            status_filter=status_filter,
            feature_type=feature_type,
            min_support=min_support,
            max_features=max_features,
            baseline_key=baseline_key,
            focus_key=focus_key,
            instance_filters=instance_filters,
        )
        result.setdefault("summary", {})
        result["summary"]["subset_notice"] = " ".join(subset_notes).strip() or None
        result["summary"]["truncated"] = bool(subset_notes)

        with self._cache_lock:
            GraphHandler._compare_cache[cache_key] = result

        self._respond_json(result)

    def _get_graphs_for_dataset(self, dataset: DatasetConfig) -> list[dict[str, Any]]:
        with self._cache_lock:
            cached = GraphHandler._graphs_cache.get(dataset.key)
        if cached is not None:
            return cached

        graphs = scan_trajectories(
            dataset.trajs,
            dataset.eval_report_path,
            agent_type=dataset.agent_type,
        )
        with self._cache_lock:
            GraphHandler._graphs_cache[dataset.key] = graphs
        return graphs

    def _send_file(self, path: Path):
        if not path.exists():
            self._error(404, f"File not found: {path.name}")
            return
        content_type = _MIME.get(path.suffix.lower(), "application/octet-stream")
        self._respond(200, content_type, path.read_bytes())

    def _respond_json(self, data: Any):
        body = json.dumps(data).encode("utf-8")
        self._respond(200, "application/json; charset=utf-8", body)

    def _respond(self, status: int, content_type: str, body: bytes):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", len(body))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str):
        self._respond(
            status,
            "application/json; charset=utf-8",
            json.dumps({"error": message}).encode("utf-8"),
        )


def _extract_phase_sequence(traj_data: dict[str, Any], agent_type: str, cmd_parser) -> list[str]:
    """Return a phase string per step for Sankey aggregation."""
    try:
        from mapPhase import get_phase
    except ImportError:
        def get_phase(*_args, **_kwargs):
            return "general"

    phases: list[str] = []

    if agent_type == "oh":
        prev_phases_list: list[str] = []
        for step in traj_data.get("history", []):
            obs_type = step.get("observation")
            if obs_type in ("system", "message") or obs_type is None:
                continue

            tool_call_meta = step.get("tool_call_metadata", {})
            model_response = tool_call_meta.get("model_response", {})
            choices = model_response.get("choices", [])

            step_phase = "general"
            for choice in choices:
                message = choice.get("message", {})
                for tool_call in (message.get("tool_calls") or []):
                    fn = tool_call.get("function", {})
                    tool_name = fn.get("name", "")
                    args_raw = fn.get("arguments", "{}")
                    try:
                        args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                    except Exception:
                        args = {}
                    subcommand = args.pop("command", None) if isinstance(args, dict) else None
                    step_phase = get_phase(tool_name, subcommand, "", args or {}, prev_phases_list, {})
                    break
                if step_phase != "general":
                    break

            phases.append(step_phase)
            prev_phases_list.append(step_phase)
        return phases

    if agent_type == "msa":
        prev_phases_list = []
        messages = traj_data.get("messages", [])

        if traj_data.get("trajectory_format") == "mini-swe-agent-1":
            index = 2
            while index < len(messages):
                message = messages[index]
                if message.get("role") != "assistant":
                    index += 1
                    continue
                content = message.get("content", "")
                if not isinstance(content, str) or not content.strip():
                    index += 2
                    continue
                match = re.search(r"```bash\s*(.*?)```", content, re.DOTALL)
                action = match.group(1).strip() if match else ""
                step_phase = "general"
                if action and cmd_parser:
                    commands = cmd_parser.parse(action)
                    if commands:
                        head = commands[0]
                        step_phase = get_phase(
                            head.get("tool", ""),
                            head.get("subcommand", ""),
                            head.get("command", ""),
                            head.get("args", {}),
                            prev_phases_list,
                            head.get("flags", {}),
                        )
                phases.append(step_phase)
                prev_phases_list.append(step_phase)
                index += 2
            return phases

        index = 2
        while index < len(messages):
            message = messages[index]
            if not isinstance(message.get("output"), list):
                index += 1
                continue
            step_phase = "general"
            for block in message["output"]:
                if isinstance(block, dict) and block.get("type") == "function_call":
                    try:
                        args_json = json.loads(block.get("arguments", "{}"))
                    except Exception:
                        args_json = {}
                    cmd_str = args_json.get("command", "")
                    if cmd_str and cmd_parser:
                        commands = cmd_parser.parse(cmd_str)
                        if commands:
                            head = commands[0]
                            step_phase = get_phase(
                                head.get("tool", ""),
                                head.get("subcommand", ""),
                                head.get("command", ""),
                                head.get("args", {}),
                                prev_phases_list,
                                head.get("flags", {}),
                            )
                    break
            phases.append(step_phase)
            prev_phases_list.append(step_phase)
            index += 2
        return phases

    prev_phases_list = []
    for step in traj_data.get("trajectory", []):
        action_str = step.get("action", "")
        step_phase = "general"
        if action_str.strip() and cmd_parser:
            commands = cmd_parser.parse(action_str)
            if commands:
                head = commands[0]
                step_phase = get_phase(
                    head.get("tool", ""),
                    head.get("subcommand", ""),
                    head.get("command", ""),
                    head.get("args", {}),
                    prev_phases_list,
                    head.get("flags", {}),
                )
        phases.append(step_phase)
        prev_phases_list.append(step_phase)
    return phases


def _validate_dataset_payload(
    payload: dict[str, Any],
    *,
    index: int,
    used_keys: set[str],
) -> tuple[DatasetConfig | None, str | None]:
    raw_trajs = str(payload.get("trajs") or "").strip()
    raw_report = str(payload.get("eval_report") or "").strip()
    raw_label = str(payload.get("label") or "").strip()
    raw_key = str(payload.get("key") or "").strip()

    if not raw_trajs:
        return None, f"Framework {index + 1}: trajectories path is required."

    trajs = Path(raw_trajs)
    report = Path(raw_report) if raw_report else None

    if not trajs.exists():
        return None, f"Framework {index + 1}: trajectories path not found: {trajs}"
    if report is not None and not report.exists():
        return None, f"Framework {index + 1}: eval report not found: {report}"

    agent_type = _infer_agent_type(trajs)
    if agent_type is None:
        return None, (
            f"Framework {index + 1}: trajectories path must be a directory "
            f"(SWE-agent or mini-swe-agent) or a .jsonl file (OpenHands): {trajs}"
        )

    report_ids: set[str] = set()
    if report is not None:
        try:
            with open(report, encoding="utf-8", errors="replace") as handle:
                report_data = json.load(handle)
        except (json.JSONDecodeError, OSError) as exc:
            return None, f"Framework {index + 1}: could not read eval report as JSON: {exc}"

        report_ids = set(report_data.get("resolved_ids", []) + report_data.get("unresolved_ids", []))
        error = _check_overlap(trajs, agent_type, report_ids)
        if error:
            return None, f"Framework {index + 1}: {error}"

    label = raw_label or _default_dataset_label(trajs, agent_type)
    key = _unique_dataset_key(raw_key or label or f"framework-{index + 1}", used_keys)
    used_keys.add(key)

    return DatasetConfig(
        key=key,
        label=label,
        trajs=trajs,
        eval_report_path=str(report) if report else None,
        agent_type=agent_type,
    ), None


def _infer_agent_type(trajs: Path) -> str | None:
    if trajs.is_file() and trajs.suffix == ".jsonl":
        return "oh"
    if trajs.is_dir():
        return "msa" if any(trajs.rglob("*.traj.json")) else "sa"
    return None


def _default_dataset_label(trajs: Path, agent_type: str) -> str:
    base = trajs.stem if trajs.is_file() else trajs.name
    label = {
        "sa": "SWE-agent",
        "msa": "mini-SWE-agent",
        "oh": "OpenHands",
    }.get(agent_type, agent_type.upper())
    return f"{base} ({label})"


def _unique_dataset_key(text: str, used_keys: set[str]) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "framework"
    candidate = slug
    suffix = 2
    while candidate in used_keys:
        candidate = f"{slug}-{suffix}"
        suffix += 1
    return candidate


def _traj_instance_ids(trajs: Path, agent_type: str) -> set[str]:
    ids: set[str] = set()
    if agent_type == "oh":
        try:
            with open(trajs, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    instance_id = entry.get("instance_id")
                    if instance_id:
                        ids.add(instance_id)
        except OSError:
            pass
    elif agent_type == "msa":
        for traj_file in trajs.rglob("*.traj.json"):
            ids.add(traj_file.name[:-len(".traj.json")])
    else:
        for traj_file in trajs.rglob("*.traj"):
            ids.add(traj_file.stem)
    return ids


def _check_overlap(trajs: Path, agent_type: str, report_ids: set[str]) -> Optional[str]:
    if not report_ids:
        return None

    traj_ids = _traj_instance_ids(trajs, agent_type)
    if not traj_ids:
        return "No trajectory instances found at the given path."

    overlap = traj_ids & report_ids
    if not overlap:
        sample_traj = sorted(traj_ids)[:3]
        sample_report = sorted(report_ids)[:3]
        return (
            "The trajectories and eval report appear to be mismatched - no instance IDs overlap.\n"
            f"  Trajectory IDs (sample): {sample_traj}\n"
            f"  Report IDs (sample):     {sample_report}"
        )

    ratio = len(overlap) / max(len(traj_ids), len(report_ids))
    if ratio < _OVERLAP_THRESHOLD:
        return (
            "Very few instance IDs overlap between the trajectories and eval report "
            f"({len(overlap)} of {len(traj_ids)} trajectories matched). "
            "Check that both paths refer to the same evaluation run."
        )

    return None


def _limit_graph_subset(
    graphs: list[dict[str, Any]],
    *,
    limit: int,
    label: str,
) -> tuple[list[dict[str, Any]], str | None]:
    if limit <= 0 or len(graphs) <= limit:
        return graphs, None

    by_status: dict[str, list[dict[str, Any]]] = {}
    for meta in graphs:
        status = meta.get("status", "none")
        by_status.setdefault(status, []).append(meta)

    ordered_statuses = sorted(by_status, key=lambda status: (-len(by_status[status]), status))
    selected: list[dict[str, Any]] = []
    quotas: dict[str, int] = {}
    remaining = limit
    statuses_left = len(ordered_statuses)

    for status in ordered_statuses:
        bucket = by_status[status]
        quota = min(len(bucket), max(1, remaining // max(statuses_left, 1)))
        quotas[status] = quota
        remaining -= quota
        statuses_left -= 1

    if remaining > 0:
        for status in ordered_statuses:
            bucket = by_status[status]
            extra_room = len(bucket) - quotas[status]
            if extra_room <= 0:
                continue
            take = min(extra_room, remaining)
            quotas[status] += take
            remaining -= take
            if remaining <= 0:
                break

    for status in ordered_statuses:
        bucket = sorted(
            by_status[status],
            key=lambda item: (
                -(item.get("step_count", 0) or 0),
                item.get("instance_id", ""),
            ),
        )
        selected.extend(bucket[:quotas[status]])

    selected.sort(key=lambda item: item.get("instance_id", ""))
    notice = (
        f"Used a subset of {len(selected)} trajectories from {label} "
        f"(of {len(graphs)}) to keep analysis under the 5-minute limit."
    )
    return selected, notice


def _bool_param(params: dict[str, list[str]], key: str, *, default: bool) -> bool:
    raw = params.get(key, [None])[0]
    if raw is None:
        return default
    return raw.lower() == "true"


def _int_param(
    params: dict[str, list[str]],
    key: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = params.get(key, [None])[0]
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, min(maximum, value))
