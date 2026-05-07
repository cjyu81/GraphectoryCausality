#!/usr/bin/env python3
"""
live_graph_server.py

Entry point for the trajectory graph browser.
"""

import argparse
import logging
import re
import sys
from http.server import ThreadingHTTPServer
from pathlib import Path

# Allow sibling imports (buildGraph, mapPhase, commandParser, ...).
sys.path.insert(0, str(Path(__file__).parent))

from server.handler import DatasetConfig, GraphHandler

logger = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Live trajectory graph browser (on-demand rendering)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples
--------
  # SWE-agent - pass a directory of .traj files:
  python live_graph_server.py \
      --trajs path/to/trajectories \
      --eval_report report.json

  # OpenHands - pass the output.jsonl file directly:
  python live_graph_server.py \
      --trajs path/to/output.jsonl \
      --eval_report report.json

  # Preload multiple frameworks at startup:
  python live_graph_server.py \
      --framework "devstral-small" path/to/devstral/output.jsonl path/to/devstral/report.json \
      --framework "claude-sonnet-4" path/to/claude/output.jsonl path/to/claude/report.json \
      --primary_framework "claude-sonnet-4"

  # Start without paths and configure via the browser UI:
  python live_graph_server.py --port 8080

  # Custom assets directory:
  python live_graph_server.py \
      --trajs trajectories \
      --eval_report report.json \
      --assets_dir custom_templates
        """,
    )
    parser.add_argument(
        "--trajs",
        default=None,
        help="Directory of .traj files (SWE-agent) or path to output.jsonl (OpenHands). "
        "Can be omitted and set later from the browser UI.",
    )
    parser.add_argument(
        "--eval_report",
        default=None,
        help="Evaluation report JSON with 'resolved_ids' and 'unresolved_ids' arrays. "
        "Can be omitted and set later from the browser UI.",
    )
    parser.add_argument(
        "--framework",
        action="append",
        nargs=3,
        metavar=("LABEL", "TRAJS", "REPORT"),
        default=[],
        help="Repeatable framework preload entry: label, trajectories path, and eval report path. "
        "Use this to start the server with multiple frameworks already loaded.",
    )
    parser.add_argument(
        "--primary_framework",
        default=None,
        help="Framework label or generated key to use as the primary dataset at startup. "
        "Only applies when one or more --framework entries are provided.",
    )
    parser.add_argument(
        "--assets_dir",
        default=None,
        help="Directory containing graph_template.html, styles.css, and graph_renderer.js. "
        "Defaults to the directory containing this script.",
    )
    parser.add_argument("--port", type=int, default=8000)
    return parser.parse_args()


def _setup_cmd_parser():
    """Load a CommandParser with all available SWE-agent tool configs."""
    try:
        from commandParser import CommandParser
    except ImportError:
        logger.warning("commandParser not found - tool actions will use fallback parsing.")
        return None

    parser = CommandParser()

    tool_configs = [
        "data/SWE-agent/tools/edit_anthropic/config.yaml",
        "data/SWE-agent/tools/review_on_submit_m/config.yaml",
        "data/SWE-agent/tools/registry/config.yaml",
    ]
    loaded = []
    for cfg in tool_configs:
        cfg_path = Path(cfg)
        if cfg_path.exists():
            parser.load_tool_yaml_files([str(cfg_path)])
            loaded.append(cfg_path.name)

    if loaded:
        logger.info("Loaded tool configs: %s", ", ".join(loaded))
    else:
        logger.debug("No tool config files found; CommandParser using defaults.")

    return parser


def _infer_agent_type(trajs: Path) -> str | None:
    if trajs.is_file() and trajs.suffix == ".jsonl":
        return "oh"
    if trajs.is_dir():
        return "msa" if any(trajs.rglob("*.traj.json")) else "sa"
    return None


def _unique_dataset_key(text: str, used_keys: set[str]) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "framework"
    candidate = slug
    suffix = 2
    while candidate in used_keys:
        candidate = f"{slug}-{suffix}"
        suffix += 1
    return candidate


def _build_framework_datasets(framework_args: list[list[str]]) -> list[DatasetConfig]:
    datasets: list[DatasetConfig] = []
    used_keys: set[str] = set()

    for index, entry in enumerate(framework_args):
        label, trajs_raw, report_raw = entry
        trajs = Path(trajs_raw)
        report = Path(report_raw)

        if not trajs.exists():
            raise ValueError(f"--framework entry {index + 1}: trajectories path does not exist: {trajs}")
        if not report.exists():
            raise ValueError(f"--framework entry {index + 1}: eval report path does not exist: {report}")

        agent_type = _infer_agent_type(trajs)
        if agent_type is None:
            raise ValueError(
                f"--framework entry {index + 1}: trajectories path must be a directory "
                f"(SWE-agent or mini-swe-agent) or a .jsonl file (OpenHands): {trajs}"
            )

        key = _unique_dataset_key(label, used_keys)
        used_keys.add(key)

        datasets.append(
            DatasetConfig(
                key=key,
                label=label,
                trajs=trajs,
                eval_report_path=str(report),
                agent_type=agent_type,
            )
        )

    return datasets


def _resolve_primary_framework(datasets: list[DatasetConfig], requested: str | None) -> str | None:
    if not datasets:
        return None
    if not requested:
        return datasets[0].key

    normalized = requested.strip().lower()
    for dataset in datasets:
        if dataset.key.lower() == normalized or dataset.label.strip().lower() == normalized:
            return dataset.key

    available = ", ".join(f"{dataset.label} [{dataset.key}]" for dataset in datasets)
    raise ValueError(
        f"--primary_framework did not match any loaded framework: {requested}. "
        f"Available frameworks: {available}"
    )


def _dataset_label(dataset: DatasetConfig) -> str:
    return {
        "oh": "OpenHands (.jsonl)",
        "msa": "mini-swe-agent (directory)",
        "sa": "SWE-agent (directory)",
    }.get(dataset.agent_type, dataset.agent_type.upper())


def main() -> int:
    args = _parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )

    assets_dir = Path(args.assets_dir) if args.assets_dir else Path(__file__).parent
    if not assets_dir.exists():
        logger.error("--assets_dir does not exist: %s", assets_dir)
        return 1

    trajs: Path | None = None
    eval_report: Path | None = None
    agent_type = "sa"
    datasets: list[DatasetConfig] = []
    primary_dataset_key: str | None = None

    if args.framework:
        if args.trajs or args.eval_report:
            logger.error("Use either --framework entries or --trajs/--eval_report, not both.")
            return 1
        try:
            datasets = _build_framework_datasets(args.framework)
            primary_dataset_key = _resolve_primary_framework(datasets, args.primary_framework)
        except ValueError as exc:
            logger.error("%s", exc)
            return 1
    elif args.trajs:
        trajs = Path(args.trajs)
        if not trajs.exists():
            logger.error("--trajs path does not exist: %s", trajs)
            return 1

        agent_type = _infer_agent_type(trajs)
        if agent_type is None:
            logger.error(
                "--trajs must be a directory (SWE-agent or mini-swe-agent) "
                "or a .jsonl file (OpenHands): %s",
                trajs,
            )
            return 1

        if args.eval_report:
            eval_report = Path(args.eval_report)
            if not eval_report.exists():
                logger.error("--eval_report path does not exist: %s", eval_report)
                return 1

        dataset_key = _unique_dataset_key(trajs.stem if trajs.is_file() else trajs.name, set())
        datasets = [
            DatasetConfig(
                key=dataset_key,
                label=trajs.stem if trajs.is_file() else trajs.name,
                trajs=trajs,
                eval_report_path=str(eval_report) if eval_report else None,
                agent_type=agent_type,
            )
        ]
        primary_dataset_key = dataset_key

    GraphHandler.graphs_dir = trajs
    GraphHandler.agent_type = agent_type
    GraphHandler.eval_report_path = str(eval_report) if eval_report else None
    GraphHandler.cmd_parser = _setup_cmd_parser()
    GraphHandler.assets_dir = assets_dir

    with GraphHandler._cache_lock:
        GraphHandler._datasets = datasets
        GraphHandler._primary_dataset_key = primary_dataset_key
        GraphHandler._sync_primary_legacy_fields_locked()
        GraphHandler._flush_caches_locked()

    httpd = ThreadingHTTPServer(("", args.port), GraphHandler)

    print(f"\n{'-' * 60}")
    print("  Trajectory Graph Server")
    print(f"{'-' * 60}")
    if datasets:
        print("  Frameworks :")
        for dataset in datasets:
            primary_suffix = "  [primary]" if dataset.key == primary_dataset_key else ""
            print(f"    - {dataset.label} [{dataset.key}]{primary_suffix}")
            print(f"      Agent  : {dataset.agent_type.upper()} ({_dataset_label(dataset)})")
            print(f"      Trajs  : {dataset.trajs.absolute()}")
            if dataset.eval_report_path:
                print(f"      Report : {Path(dataset.eval_report_path).absolute()}")
            else:
                print("      Report : (none - status badges will not be shown)")
    else:
        print("  Data source: not set - configure via the browser UI")
    print(f"  Assets     : {assets_dir.absolute()}")
    print(f"  URL        : http://localhost:{args.port}")
    print(f"{'-' * 60}\n")
    print("  Press Ctrl+C to stop.\n")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
