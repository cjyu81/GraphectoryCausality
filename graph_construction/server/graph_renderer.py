"""
server/graph_renderer.py

Converts a NetworkX MultiDiGraph into a self-contained HTML string
by filling in the graph_template.html with inlined CSS/JS and graph data.

The public surface is a single function:

    html = render_graph_html(G, filter_cd, assets_dir)
"""

import json
import os
from pathlib import Path
from typing import Any

import networkx as nx


FONT_FAMILY = os.environ.get("GRAPH_FONT", "DejaVu Sans, Arial, sans-serif")

PHASE_COLORS = {
    "localization": "#C5B3F0",
    "patch":        "#FCC9B0",
    "validation":   "#A8E6F0",
    "general":      "#CFE0F6",
}


# ── Public API ──────────────────────────────────────────────────────────────

def render_graph_html(G: nx.MultiDiGraph, filter_cd: bool,
                      assets_dir: Path) -> str:
    """Return a complete, self-contained HTML string for the graph.

    Args:
        G:           Built NetworkX graph.
        filter_cd:   Whether cd-filtering was applied (shown in the UI).
        assets_dir:  Directory containing graph_template.html, styles.css,
                     and graph_renderer.js.
    """
    nodes_data = _prepare_nodes(G)
    edges_data = _prepare_edges(G)

    meta = {
        "instance_name":     G.graph.get("instance_name", "Unknown"),
        "resolution_status": G.graph.get("resolution_status", "unknown"),
        "difficulty":        str(G.graph.get("debug_difficulty", "unknown")),
        "node_count":        str(len(nodes_data)),
        "edge_count":        str(len(edges_data)),
        "metadata_comment":  f"Mode: {'cd filtered (▲ hat)' if filter_cd else 'cd as node'}",
    }

    template = _load(assets_dir / "graph_template.html")
    css      = _load(assets_dir / "styles.css").replace("{{FONT_FAMILY}}", FONT_FAMILY)
    js       = _load(assets_dir / "graph_renderer.js")

    html = template

    # Metadata substitutions
    html = html.replace("{{INSTANCE_NAME}}",     _esc(meta["instance_name"]))
    html = html.replace("{{RESOLUTION_STATUS}}", meta["resolution_status"])
    html = html.replace("{{DIFFICULTY}}",        _esc(meta["difficulty"]))
    html = html.replace("{{NODE_COUNT}}",        meta["node_count"])
    html = html.replace("{{EDGE_COUNT}}",        meta["edge_count"])
    html = html.replace("{{METADATA_COMMENT}}",  _esc(meta["metadata_comment"]))

    # Data substitutions
    html = html.replace("{{NODES_DATA}}",   json.dumps(nodes_data))
    html = html.replace("{{EDGES_DATA}}",   json.dumps(edges_data))
    html = html.replace("{{PHASE_COLORS}}", json.dumps(PHASE_COLORS))

    # Inline CSS and JS so the response is fully self-contained
    html = html.replace(
        '<link rel="stylesheet" href="styles.css">',
        f'<style>{css}</style>',
    )
    html = html.replace(
        '<script src="graph_renderer.js"></script>',
        f'<script>{js}</script>',
    )

    return html


# ── Node preparation ────────────────────────────────────────────────────────

def _prepare_nodes(G: nx.MultiDiGraph) -> list[dict[str, Any]]:
    nodes = []
    for node_id, data in G.nodes(data=True):
        colors        = _node_colors(data)
        primary_color = colors[0] if colors else PHASE_COLORS["general"]

        args        = data.get("args", {}) or {}
        edit_status = args.get("edit_status", "") if isinstance(args, dict) else ""
        has_failure = bool(edit_status and str(edit_status).startswith("failure"))
        has_cd      = bool(data.get("has_cd", False))

        nodes.append({
            "id":          node_id,
            "label":       _make_label(data),
            "tooltip":     _make_tooltip(data),
            "color":       primary_color,
            "colors":      colors,
            "has_failure": has_failure,
            "has_cd":      has_cd,
        })
    return nodes


def _node_colors(data: dict) -> list[str]:
    """Return ordered list of phase colour hexes for a node."""
    phases = data.get("phases") or ["general"]
    order  = ["localization", "patch", "validation", "general"]
    seen, result = set(), []
    for ph in order:
        if ph in phases and ph not in seen:
            seen.add(ph); result.append(ph)
    for ph in phases:
        if ph not in seen:
            seen.add(ph); result.append(ph)
    return [PHASE_COLORS.get(ph, PHASE_COLORS["general"]) for ph in result]


def _make_label(data: dict) -> str:
    """Multi-line display label for a node."""
    lines = []

    base = (data.get("command") or data.get("subcommand") or
            data.get("label") or "").strip()

    args = data.get("args", {}) or {}
    if isinstance(args, dict):
        status = args.get("edit_status")
        if status == "success":
            base += " ✓"
        elif status and str(status).startswith("failure"):
            base += " ✗"

    lines.append(base if base else data.get("label", ""))

    step_indices = data.get("step_indices", [])
    if step_indices:
        if len(step_indices) <= 3:
            lines.append(f"steps: {','.join(map(str, step_indices))}")
        else:
            lines.append(f"steps: {step_indices[0]}..{step_indices[-1]} ({len(step_indices)})")

    if isinstance(args, dict):
        path = args.get("path")
        if path:
            p = str(path).replace("\\", "/")
            parts = p.split("/")
            lines.append(("…/" + "/".join(parts[-2:])) if len(parts) > 2 else p[-30:])

        vr = args.get("view_range")
        if isinstance(vr, (list, tuple)) and len(vr) == 2:
            lines.append(f"L{vr[0]}-{vr[1]}")

    return "\\n".join(lines)


def _make_tooltip(data: dict) -> str:
    """HTML tooltip content for a node."""
    parts = [f"<strong>{_esc(data.get('label', ''))}</strong>"]

    if data.get("tool"):
        parts.append(f"Tool: {_esc(data['tool'])}")
    if data.get("subcommand"):
        parts.append(f"Subcommand: {_esc(data['subcommand'])}")

    phases = data.get("phases", ["general"])
    parts.append(f"Phases: {', '.join(set(phases))}")

    step_indices    = data.get("step_indices", [])
    thought_lengths = data.get("thought_lengths", [])

    if step_indices:
        parts.append(f"Step: {', '.join(map(str, step_indices))}")
    if thought_lengths:
        parts.append(
            f"Thought len: {thought_lengths[0]}" if len(thought_lengths) == 1
            else f"Thought lengths: {', '.join(map(str, thought_lengths))}"
        )

    args = data.get("args", {}) or {}
    if isinstance(args, dict) and args:
        parts.append("<br><strong>Arguments:</strong>")
        for k, v in args.items():
            v_str = str(v)
            display = v_str if len(v_str) <= 100 else v_str[:100] + "…"
            parts.append(f"  • {k}: {_esc(display)}")

    return "<br>".join(parts)


# ── Edge preparation ────────────────────────────────────────────────────────

def _prepare_edges(G: nx.MultiDiGraph) -> list[dict[str, Any]]:
    edges = []

    for u, v, _k, d in G.edges(keys=True, data=True):
        etype            = d.get("type", "exec")
        edge_label       = str(d.get("label", ""))
        is_first_in_step = bool(d.get("is_first_in_step", False))

        thought_length   = 0
        is_multi_node    = False

        if etype == "exec":
            u_steps    = set(G.nodes[u].get("step_indices", []))
            v_steps    = set(G.nodes[v].get("step_indices", []))
            u_thoughts = G.nodes[u].get("thought_lengths", [])

            common = u_steps & v_steps
            if common:
                is_multi_node  = True
                thought_length = 0
            elif is_first_in_step and u_thoughts:
                thought_length = u_thoughts[-1]

        edges.append({
            "from":             u,
            "to":               v,
            "type":             etype,
            "label":            edge_label if etype == "exec" else "",
            "thought_length":   thought_length,
            "is_multi_node_step": is_multi_node,
            "is_first_in_step": is_first_in_step,
        })

    return edges


# ── Helpers ─────────────────────────────────────────────────────────────────

def _load(path: Path) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _esc(s: str) -> str:
    return (str(s) if s else "").replace("&", "&amp;").replace("<", "&lt;") \
                                 .replace(">", "&gt;").replace('"', "&quot;")
