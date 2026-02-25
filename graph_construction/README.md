# Graphectory

A live-updating browser for SWE-agent trajectory graphs. Point it at a directory of `.traj` files and an evaluation report, open `http://localhost:8000`, and browse every run as an interactive graph — no pre-processing or file generation required.

---

## Quick Start

```bash
python graph_construction/live_graph_server.py \
    --graphs_dir "C:\Users\charl\Documents\Homework Holder\bbbbb\traj\rest" \
    --eval_report "C:\Users\charl\Documents\Homework Holder\bbbbb\swe-bench_verified__test__summary_devstral.json"
```

Then open **http://localhost:8000** in your browser.

---

## Command-Line Arguments

| Argument | Required | Description |
|---|---|---|
| `--graphs_dir` | ✓ | Root directory that contains trajectory subdirectories. Searched recursively for `*.traj` files. |
| `--eval_report` | ✓ | Path to the SWE-bench evaluation summary JSON. Used to mark each instance as resolved, unresolved, or unsubmitted. |
| `--assets_dir` | | Directory containing `graph_template.html`, `styles.css`, and `graph_renderer.js`. Defaults to the same directory as `live_graph_server.py`. Only needed if you have customised those files in a separate location. |
| `--port` | | Port to serve on. Defaults to `8000`. |

### What the paths point to

**`--graphs_dir`** is the parent of one or more trajectory run directories. Each run lives in its own subdirectory and contains at minimum a `.traj` file (the raw agent execution log in JSON). An optional `.json` metadata file alongside the `.traj` can carry pre-computed resolution status and difficulty information. The server walks the entire directory tree recursively, so subdirectory depth does not matter.

**`--eval_report`** is the JSON summary produced by the SWE-bench evaluation harness. It must contain `"resolved_ids"` and `"unresolved_ids"` arrays of instance ID strings. Any instance found in `--graphs_dir` but absent from both arrays is marked as `"unsubmitted"`.

---

## The Browser UI

The left panel lists every trajectory found under `--graphs_dir`. Each entry shows:

- **Instance ID** — the filename stem of the `.traj` file.
- **Status badge** — resolved (green), unresolved (red), or unsubmitted (yellow), sourced from `--eval_report`.
- **Step count** — number of agent steps in the trajectory.
- **Difficulty** — pulled from the SWE-bench dataset if the `datasets` package is installed, otherwise omitted.

A search box at the top filters the list by instance ID substring in real time.

Clicking any entry loads its graph into the right pane. The graph renders inside a sandboxed iframe; navigating to another instance swaps the iframe content without reloading the page.

### Toggles

Four switches sit above the instance list and take effect immediately whenever you change them — the currently-loaded graph is re-requested from the server with the new settings applied.

| Toggle | Default | Effect |
|---|---|---|
| **Verbose node labels** | On | Shows multi-line node labels: action name, step number, and file path or view range. When off, nodes show only the action verb. |
| **Exclude quotes in thought length** | On | Strips content inside backticks and quote characters before measuring thought length. This makes the arrowhead sizes reflect genuine reasoning rather than code or quoted text the agent copied. |
| **Filter cd (show ▲ hat)** | Off | When enabled, leading `cd` commands are stripped from multi-command steps and replaced with a small orange triangle (▲) hat on the node so working-directory changes remain visible without adding noise. |
| **Show observation indicators** | Off | When enabled, draws a coloured rounded square on the outgoing edge of each step to encode the length and outcome of that step's observation (tool response). |

---

## Reading the Graph

Each graph is a directed left-to-right flow of the agent's actions for one trajectory.

### Nodes

Every distinct action the agent took is a node. If the agent took the exact same action (same tool, same arguments) more than once across different steps, they are **merged into a single node** that lists all the step indices it appeared in. This deduplication reveals loops and repeated patterns quickly.

Node colour encodes the **phase** of the action:

| Colour | Phase | Meaning |
|---|---|---|
| Purple | Localization | Reading files, searching code, running tests before any patch has been applied |
| Orange | Patch | Creating or editing source files |
| Blue | Validation | Running tests or inspecting test files after a patch exists |
| Light blue | General | Everything else |

A node may show two colours as a horizontal gradient when it spans multiple phases across its repeated visits.

### Edges

Edges run left to right in step order.

| Style | Meaning |
|---|---|
| Grey solid, scaled arrowhead | Normal execution edge. The **arrowhead size** encodes how long the agent's thought was before taking the action — a larger arrowhead means more reasoning. |
| Red solid | Thought-continuation edge. The agent's thought for this step was identical to or a direct extension of the previous step's thought, suggesting the model reused cached reasoning. |
| Blue dashed | Intra-step edge. The agent issued multiple commands in one step (chained with `&&`); these edges connect the sub-actions within that step. |
| Green dashed | Hierarchy edge. Drawn between `str_replace_editor view` nodes when one viewed path is a subdirectory or line-range subset of another, revealing the agent's code-reading structure. |

### Observation Indicators (when enabled)

When **Show observation indicators** is on, a small rounded square appears near the source end of each inter-step edge (roughly 25% along the edge). It encodes the tool response from the **previous** step:

- **Size** — proportional to the length of the observation text. A tiny square means a short response; a large square means a long one (up to ~8000 characters).
- **Colour** — green for a successful outcome (tests passed, file edited successfully), red for a failure (traceback, assertion error, edit conflict), or muted blue-grey for a neutral response.

### Interacting with the Graph

- **Scroll** to zoom in and out.
- **Drag** on empty space to pan.
- **Click a node** to open the detail sidebar on the right.
- **Click empty space** or press the ✕ button to close the sidebar.

The sidebar shows the full **thought**, **action**, and **observation** text for the selected node. If the node was visited multiple times, tab buttons at the top let you page through each visit's step data individually.

The sidebar's left edge is draggable — pull it left to make the panel wider.

---

## How It Works

### Server

`live_graph_server.py` is the single entry point. It starts a standard Python `http.server.HTTPServer` and registers a custom handler (`server/handler.py`). All graph data is rendered on demand; nothing is written to disk.

**Routes:**

| Route | Response |
|---|---|
| `GET /` | The browser UI (`index.html` + `browser.css` + `browser.js`) |
| `GET /api/graphs` | JSON array of all discovered trajectories with status, difficulty, and step count |
| `GET /api/graph?id=…&…` | A fully self-contained HTML document for a single trajectory graph |

### Graph Construction (`server/graph_builder.py` + `buildGraph.py`)

When a graph is requested the server:

1. **Scans** the `--graphs_dir` tree for `*.traj` files and matches them against the eval report to assign resolution status.
2. **Loads** the requested `.traj` file (a JSON array of `{thought, action, observation}` step dicts).
3. **Parses** each step's action string using `CommandParser` (if available) or a lightweight fallback. The parser understands tool calls (`str_replace_editor view`, `str_replace_editor str_replace`, etc.) and bare shell commands including `&&`-chained sequences and Python heredocs.
4. **Builds a NetworkX `MultiDiGraph`** where each distinct action becomes a node (deduplicated by a hash of label + arguments + flags). Repeated identical actions accumulate step indices and thought lengths on the same node rather than creating duplicates.
5. **Classifies each action into a phase** (`mapPhase.py`) using a rule-based heuristic: read-only operations → localization; source edits → patch; test execution or test-file inspection after a patch → validation. Phase history flows forward so each action's classification can depend on what the agent has already done.
6. **Attaches execution edges** with thought length and step index metadata. Intra-step edges (from `&&`-chaining) are marked separately from inter-step edges.
7. **Detects thought continuation** — when the current step's thought is a substring of or identical to the previous step's thought — and marks the connecting edge accordingly.
8. **Adds hierarchy edges** between `str_replace_editor view` nodes based on file-path containment and line-range nesting, revealing the agent's file-reading structure.
9. **Attaches observation data** (length and success/failure outcome) to the last node of each step so it can be carried onto the next step's incoming edge.

### Rendering (`server/graph_renderer.py` + `graph_renderer.js`)

The Python renderer serialises the NetworkX graph into two JSON arrays — `nodesData` and `edgesData` — and inlines them into `graph_template.html` along with all CSS and JavaScript, producing a single self-contained HTML document that requires no network requests after delivery.

Inside the browser, `graph_renderer.js`:

1. **Runs the Dagre layout algorithm** to compute left-to-right node positions and smooth cubic-bezier edge routes.
2. **Draws everything as SVG** — nodes as rounded rectangles with gradient fills for multi-phase actions, edges as paths with per-width SVG arrowhead markers.
3. **Scales arrowheads** by pre-creating SVG `<marker>` elements at each distinct thought-length width so the marker geometry matches the line geometry correctly.
4. **Places observation squares** by walking the edge's waypoint polyline to the 25% arc-length position and drawing a `<rect>` element there.
5. **Implements zoom and pan** via CSS `transform: translate/scale` on the SVG element, using wheel events for zoom and mousedown+mousemove for drag-to-pan with click-vs-drag discrimination.
6. **Manages the detail sidebar** with a CSS `width` transition (never toggling `display`) to avoid layout reflow, and defers DOM cleanup until after the closing animation completes.

### File Layout

```
graph_construction/
├── live_graph_server.py     # Entry point — starts the HTTP server
├── buildGraph.py            # GraphBuilder base class, node hashing, status helpers
├── mapPhase.py              # Phase classifier (localization / patch / validation / general)
├── commandParser.py         # Shell + tool-call parser (bashlex-based)
├── visualizer.py            # Legacy visualizer (used by buildGraph base class)
└── server/
    ├── handler.py           # HTTP routing
    ├── graph_builder.py     # Graph construction, trajectory scanning, hierarchy edges
    ├── graph_renderer.py    # Python → JSON serialisation, HTML assembly
    └── static/
        ├── index.html       # Browser shell
        ├── browser.css      # Browser shell styles
        ├── browser.js       # Instance list, search, toggle wiring, iframe injection
        ├── graph_template.html  # Per-graph HTML scaffold
        ├── styles.css           # Graph page styles (dark sidebar, legend, controls)
        └── graph_renderer.js    # Dagre layout, SVG drawing, zoom/pan, sidebar logic
```

---

## Dependencies

```
networkx       # Graph data structure
dagre (JS)     # Automatic left-to-right graph layout (loaded from unpkg.com)
bashlex        # Shell command parsing (used by commandParser)
datasets       # Optional — HuggingFace datasets for difficulty lookup
```

All JavaScript dependencies are loaded from a CDN at graph render time. The server itself has no npm dependencies.