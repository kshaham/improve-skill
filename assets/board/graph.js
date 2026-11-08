"use strict";
// Knowledge view: the cross-run knowledge graph, its curation, and the
// "what the graph knows" section of the task dialog.
const GRAPH_TYPES = [
  ["insight", "Knowledge", "#7355db"],
  ["file", "Files", "#2f7fb8"],
  ["task", "Tasks", "#2d8b66"],
  ["area", "Areas", "#b37627"],
  ["gate", "Gates", "#0f8a8a"],
  ["blocker", "Blockers", "#c2410c"],
  ["run", "Runs", "#8a8f9c"],
];
const GRAPH_DEFAULT_TYPES = ["insight", "file", "area", "gate", "blocker"];
const GRAPH_LIMIT = 220,
  GRAPH_LABELS = 36,
  GRAPH_WARMUP_STEPS = 140,
  GRAPH_SVG = "http://www.w3.org/2000/svg";
const KIND_LABELS = {
  lesson: "Lesson",
  pitfall: "Pitfall",
  technique: "Technique",
  convention: "Convention",
  decision: "Decision",
  fact: "Fact",
};
const STATUS_COLORS = { done: "#2d8b66", rejected: "#9a9ead", blocked: "#c2410c" };
let graphData = null,
  graphRevision = null,
  graphKey = "",
  graphLoading = false,
  graphSelected = null,
  graphFocus = null,
  graphFitPending = true,
  graphFrame = null,
  graphDetailLoad = 0,
  graphNoteRetry = null,
  graphPendingTask = null,
  graphKeepStatus = false,
  taskKnowledgeKey = "",
  graphTypes = new Set(GRAPH_DEFAULT_TYPES);
const graphLayout = new GraphLayout(),
  graphViewport = { scale: 1, x: 0, y: 0 },
  graphElements = { nodes: new Map(), edges: [] };

function graphColor(type) {
  return GRAPH_TYPES.find((row) => row[0] === type)?.[2] || "#747987";
}

function graphTypeLabel(node) {
  if (node.type === "insight") return KIND_LABELS[node.kind] || "Knowledge";
  return GRAPH_TYPES.find((row) => row[0] === node.type)?.[1].replace(/s$/, "") || node.type;
}

function svg(tag, attributes = {}) {
  const node = document.createElementNS(GRAPH_SVG, tag);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
  return node;
}

function shortLabel(text, limit = 30) {
  text = String(text ?? "");
  if (text.includes("/") && text.length > limit) text = "…/" + text.split("/").slice(-2).join("/");
  return text.length > limit ? text.slice(0, limit - 1) + "…" : text;
}

function graphStatus(message, error = false) {
  $("graph-status").textContent = message;
  $("graph-status").classList.toggle("form-error", error);
}

// The result of something the user did outlives the reload it triggers (or one already in
// flight); only a scope change the user asks for afterwards replaces it.
function graphActionStatus(message, error = false) {
  graphStatus(message, error);
  graphKeepStatus = true;
}

function userScopeChange() {
  graphKeepStatus = false;
}

function relativeTime(value) {
  const time = Date.parse(value);
  if (!Number.isFinite(time)) return "Never";
  const minutes = Math.round((Date.now() - time) / 60000);
  if (minutes < 1) return "Just now";
  if (minutes < 60) return minutes + " min ago";
  if (minutes < 1440) return Math.round(minutes / 60) + " h ago";
  return new Date(time).toLocaleDateString();
}

function renderGraphBadge() {
  const summary = snapshot?.graph;
  $("graph-count").textContent = countText(summary?.insights || 0);
  $("graph-count").title = summary ? `${summary.nodes} nodes from ${summary.runs} runs` : "";
}

function graphQueryKey() {
  return JSON.stringify({
    focus: graphFocus,
    q: $("graph-query").value.trim(),
    types: [...graphTypes].sort(),
    archived: $("graph-archived").checked,
  });
}

function renderGraphView() {
  const summary = snapshot?.graph;
  $("graph-total").textContent = summary
    ? `${countText(summary.nodes)} · ${countText(summary.edges)}`
    : "0";
  $("graph-insights").textContent = countText(summary?.insights || 0);
  $("graph-runs").textContent = countText(summary?.runs || 0);
  $("graph-refined").textContent = relativeTime(summary?.last_refine);
  $("visible-count").textContent = graphData
    ? `${countText(graphData.nodes.length)} of ${countText(graphData.total_nodes)} nodes shown`
    : "Loading knowledge…";
  if (graphQueryKey() !== graphKey || (summary && summary.revision !== graphRevision))
    loadGraph();
  else if (graphData && !graphFrame) animateGraph();
  openPendingGraphTask();
}

async function loadGraph() {
  if (graphLoading) return;
  graphLoading = true;
  const key = graphQueryKey(),
    query = JSON.parse(key);
  const params = new URLSearchParams({ limit: String(GRAPH_LIMIT) });
  if (query.focus) {
    params.set("focus", query.focus);
    params.set("depth", "2");
  } else {
    params.set("types", query.types.join(","));
    if (query.q) params.set("q", query.q);
  }
  if (query.archived) params.set("archived", "1");
  try {
    const data = await boardJSON("/api/graph?" + params);
    if (graphQueryKey() !== key) return;
    const changedScope = key !== graphKey;
    graphData = data;
    graphKey = key;
    graphRevision = data.revision;
    if (changedScope) graphFitPending = true;
    drawGraph();
    renderGraphLists();
    // A refresh after a save keeps that confirmation; only a new scope is described,
    // unless the scope changed only to explain a missing node.
    if (changedScope && graphKeepStatus) graphKeepStatus = false;
    else if (changedScope)
      graphStatus(
      data.focus
        ? `Showing the neighborhood of “${shortLabel(data.nodes.find((node) => node.id === data.focus)?.label, 60)}”.`
        : query.q
          ? `${data.nodes.length} nodes match “${query.q}” or link to a match.`
          : data.truncated
            ? `Showing the ${data.nodes.length} strongest nodes. Search or select a node to see more.`
            : "",
    );
    if (graphSelected) loadNodeDetail(graphSelected, false);
  } catch (error) {
    if (error.message === "that node is no longer in the graph") {
      graphFocus = null;
      graphSelected = null;
      graphKeepStatus = true;
      graphStatus("That node was merged or removed during refinement. Showing the strongest nodes.");
    } else graphStatus("The knowledge graph could not load. " + error.message, true);
  } finally {
    graphLoading = false;
    if (activeView === "graph" && graphQueryKey() !== graphKey && !$("graph-status").classList.contains("form-error"))
      setTimeout(renderGraphView, 0);
  }
}

function drawGraph() {
  const canvas = $("graph-canvas");
  const nodes = graphData.nodes;
  $("graph-empty").hidden = nodes.length !== 0;
  graphLayout.update(nodes, graphData.edges);
  if (graphLayout.alpha === 1)
    for (let index = 0; index < GRAPH_WARMUP_STEPS; index++) graphLayout.step();
  const labelled = new Set(
    [...nodes]
      .filter((node) => !["task", "run"].includes(node.type))
      .sort((a, b) => (b.score || 0) - (a.score || 0))
      .slice(0, GRAPH_LABELS)
      .map((node) => node.id),
  );
  const viewport = svg("g", { class: "graph-viewport" }),
    edgeLayer = svg("g", { class: "graph-edges" }),
    nodeLayer = svg("g", { class: "graph-nodes" });
  graphElements.nodes = new Map();
  graphElements.edges = graphData.edges.map((edge) => {
    const line = svg("line", {
      class: "graph-edge rel-" + edge.rel + (edge.inferred ? " inferred" : ""),
      "stroke-width": String(Math.min(4, 0.8 + 0.5 * (edge.weight || 1))),
    });
    line.dataset.src = edge.src;
    line.dataset.dst = edge.dst;
    edgeLayer.append(line);
    return { edge, line };
  });
  for (const node of nodes) {
    const group = svg("g", {
      class:
        "graph-node type-" +
        node.type +
        (node.archived ? " archived" : "") +
        (node.pinned ? " pinned" : "") +
        (node.stale ? " stale" : "") +
        (labelled.has(node.id) ? " labelled" : ""),
      tabindex: "0",
      role: "button",
      "aria-label": `${graphTypeLabel(node)}: ${node.label}`,
    });
    group.dataset.id = node.id;
    const radius = 4 + 9 * Math.min(1.2, node.score || 0.2) + (node.type === "insight" ? 2 : 0);
    const circle = svg("circle", { r: radius.toFixed(1), fill: graphColor(node.type) });
    if (node.type === "task" && STATUS_COLORS[node.status])
      circle.setAttribute("stroke", STATUS_COLORS[node.status]);
    const label = svg("text", { x: (radius + 4).toFixed(1), y: "4" });
    label.textContent = shortLabel(node.label);
    const title = svg("title");
    title.textContent = `${graphTypeLabel(node)}: ${node.label}`;
    group.append(circle, label, title);
    nodeLayer.append(group);
    graphElements.nodes.set(node.id, group);
  }
  viewport.append(edgeLayer, nodeLayer);
  canvas.replaceChildren(viewport);
  renderLegend();
  markSelection();
  if (graphFitPending) fitGraph();
  positionGraph();
  animateGraph();
}

function renderLegend() {
  const present = new Set(graphData.nodes.map((node) => node.type));
  $("graph-legend").replaceChildren(
    ...GRAPH_TYPES.filter(([type]) => present.has(type)).map(([type, label, color]) => {
      const item = element("span", "", label);
      item.style.setProperty("--swatch", color);
      return item;
    }),
  );
}

function positionGraph() {
  const viewport = $("graph-canvas").firstChild;
  if (!viewport) return;
  viewport.setAttribute(
    "transform",
    `translate(${graphViewport.x.toFixed(1)} ${graphViewport.y.toFixed(1)}) scale(${graphViewport.scale.toFixed(3)})`,
  );
  for (const [id, group] of graphElements.nodes) {
    const point = graphLayout.points.get(id);
    if (point) group.setAttribute("transform", `translate(${point.x.toFixed(1)} ${point.y.toFixed(1)})`);
  }
  for (const { edge, line } of graphElements.edges) {
    const a = graphLayout.points.get(edge.src),
      b = graphLayout.points.get(edge.dst);
    if (!a || !b) continue;
    line.setAttribute("x1", a.x.toFixed(1));
    line.setAttribute("y1", a.y.toFixed(1));
    line.setAttribute("x2", b.x.toFixed(1));
    line.setAttribute("y2", b.y.toFixed(1));
  }
}

function animateGraph() {
  if (graphFrame || activeView !== "graph") return;
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) {
    for (let index = 0; index < 400 && graphLayout.step(); index++);
    positionGraph();
    return;
  }
  const tick = () => {
    graphFrame = null;
    if (activeView !== "graph" || document.hidden) return;
    const moving = graphLayout.step() && graphLayout.step();
    positionGraph();
    if (moving) graphFrame = requestAnimationFrame(tick);
  };
  graphFrame = requestAnimationFrame(tick);
}

function fitGraph() {
  const bounds = graphLayout.bounds(),
    canvas = $("graph-canvas");
  const width = canvas.clientWidth || 800,
    height = canvas.clientHeight || 520;
  graphFitPending = !bounds || !canvas.clientWidth;
  if (!bounds) return;
  // Labels extend to the right of each node; leave room for them.
  const spanX = Math.max(80, bounds.maxX - bounds.minX + 260),
    spanY = Math.max(80, bounds.maxY - bounds.minY + 60);
  graphViewport.scale = Math.max(0.2, Math.min(2.2, Math.min(width / spanX, height / spanY)));
  graphViewport.x = width / 2 - ((bounds.minX + bounds.maxX) / 2 + 70) * graphViewport.scale;
  graphViewport.y = height / 2 - ((bounds.minY + bounds.maxY) / 2) * graphViewport.scale;
  positionGraph();
}

function zoomGraph(factor, centerX, centerY) {
  const canvas = $("graph-canvas");
  centerX ??= canvas.clientWidth / 2;
  centerY ??= canvas.clientHeight / 2;
  const scale = Math.max(0.15, Math.min(4, graphViewport.scale * factor));
  graphViewport.x = centerX - ((centerX - graphViewport.x) * scale) / graphViewport.scale;
  graphViewport.y = centerY - ((centerY - graphViewport.y) * scale) / graphViewport.scale;
  graphViewport.scale = scale;
  positionGraph();
}

function markSelection() {
  const linked = new Set();
  if (graphSelected && graphData)
    for (const edge of graphData.edges) {
      if (edge.src === graphSelected) linked.add(edge.dst);
      if (edge.dst === graphSelected) linked.add(edge.src);
    }
  for (const [id, group] of graphElements.nodes) {
    group.classList.toggle("selected", id === graphSelected);
    group.classList.toggle("linked", linked.has(id));
    group.classList.toggle("dimmed", Boolean(graphSelected) && id !== graphSelected && !linked.has(id));
  }
  for (const { edge, line } of graphElements.edges)
    line.classList.toggle(
      "highlight",
      Boolean(graphSelected) && (edge.src === graphSelected || edge.dst === graphSelected),
    );
}

function selectGraphNode(id, { focus = false, keepStatus = false } = {}) {
  if (!keepStatus) userScopeChange();
  graphSelected = id;
  if (focus || (graphData && !graphData.nodes.some((node) => node.id === id))) {
    graphFocus = id;
    graphFitPending = true;
  }
  markSelection();
  loadNodeDetail(id, true);
  writeViewLocation();
  if (activeView === "graph") renderGraphView();
}

async function loadNodeDetail(id, announce) {
  const load = ++graphDetailLoad;
  try {
    const detail = await boardJSON("/api/graph/node?id=" + encodeURIComponent(id));
    if (load !== graphDetailLoad || graphSelected !== id) return;
    renderNodeDetail(detail);
  } catch (error) {
    if (load !== graphDetailLoad) return;
    graphSelected = null;
    markSelection();
    $("graph-detail").replaceChildren(
      element("p", "focus-hint", announce ? "That node is no longer in the graph. It may have been merged during refinement." : "Select a node to see what it is connected to."),
    );
    writeViewLocation();
  }
}

function detailFact(list, label, value) {
  if (value == null || value === "" || value === false) return;
  list.append(element("dt", "", label), element("dd", "", value));
}

function renderNodeDetail({ node, links }) {
  const panel = $("graph-detail");
  const header = element("div", "graph-detail-top");
  const chip = element("span", "graph-chip", graphTypeLabel(node));
  chip.style.setProperty("--swatch", graphColor(node.type));
  header.append(chip);
  if (node.pinned) header.append(element("span", "graph-chip pinned-chip", "Pinned"));
  if (node.source === "user") header.append(element("span", "graph-chip", "From you"));
  if (node.archived) header.append(element("span", "graph-chip muted-chip", "Archived"));
  if (node.stale) header.append(element("span", "graph-chip muted-chip", "File no longer tracked"));
  const title = element("h3", "", node.label);
  const parts = [header, title];
  if (node.summary && node.summary !== node.label) parts.push(element("p", "graph-summary", node.summary));
  if (node.outcome_note) parts.push(element("p", "graph-summary", node.outcome_note));
  const facts = element("dl", "graph-facts");
  const stats = node.stats;
  if (stats && stats.attempts)
    detailFact(facts, "Verified", `${stats.done} of ${stats.attempts} attempts (${Math.round(stats.success * 100)}% smoothed)`);
  if (stats && (stats.open || 0)) detailFact(facts, "Open tasks", stats.open);
  detailFact(facts, "Status", node.status && (labels[node.status] || node.status));
  detailFact(facts, "Area", node.area);
  if (node.verdict) detailFact(facts, "Verdict", node.verdict === "proven" ? "Proven by outcomes" : "Contested: outcomes are mixed");
  if (node.restored)
    detailFact(facts, "Restored", `${relativeTime(node.restored.at)} (${node.restored.reason})`);
  if (node.type === "insight") {
    detailFact(facts, "Seen", `${node.reinforced || 1}×`);
    detailFact(facts, "Helped tasks", node.cited_done);
    detailFact(facts, "Cited by failed tasks", node.cited_failed);
    detailFact(facts, "Marked useful", node.useful);
    detailFact(facts, "Marked wrong", node.wrong);
  }
  detailFact(facts, "Gate cost", node.seconds != null ? node.seconds + " s" : null);
  detailFact(facts, "Used by tasks", node.uses);
  detailFact(facts, "Commit", node.commit);
  detailFact(facts, "Run outcome", node.outcome);
  detailFact(facts, "Archived because", node.archived);
  // Pins and user knowledge boost the score past 1; show it as a bounded relevance.
  detailFact(facts, "Relevance", node.score != null ? Math.min(100, Math.round(node.score * 100)) + "%" : null);
  detailFact(facts, "Id", node.id);
  parts.push(facts);
  if (Array.isArray(node.merged_from) && node.merged_from.length) {
    const merged = element("details", "graph-merged");
    merged.append(element("summary", "", `Merged from ${node.merged_from.length} similar notes`));
    merged.append(...node.merged_from.map((text) => element("p", "", text)));
    parts.push(merged);
  }
  const actions = element("div", "graph-actions");
  const action = (label, handler, className = "") => {
    const button = element("button", className, label);
    button.type = "button";
    button.addEventListener("click", handler);
    actions.append(button);
  };
  if (node.type === "insight") {
    action("Useful", () => rateGraphNode(node.id, "useful"));
    action("Wrong", () => rateGraphNode(node.id, "wrong"), "danger");
    action(node.pinned ? "Unpin" : "Pin", () => rateGraphNode(node.id, node.pinned ? "unpin" : "pin"));
    action(node.archived ? "Restore" : "Archive", () => rateGraphNode(node.id, node.archived ? "restore" : "archive"));
  }
  if (node.type === "task") action("Open task", () => openGraphTask(node), "primary");
  if (node.type === "file" || node.type === "area")
    action("Show tasks", () => showGraphTasks(node));
  action(graphFocus === node.id ? "Show strongest" : "Focus neighborhood", () =>
    graphFocus === node.id ? resetGraphScope() : selectGraphNode(node.id, { focus: true }),
  );
  parts.push(actions);
  const groups = new Map();
  for (const link of links) {
    const key = link.rel.replace(/_/g, " ");
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(link);
  }
  for (const [rel, rows] of groups) {
    const section = element("section", "graph-links");
    section.append(element("h4", "", `${rel} · ${rows.length}`));
    const list = element("ul");
    for (const row of rows.slice(0, 12)) {
      const button = element("button", "graph-link" + (row.archived ? " archived" : ""));
      button.type = "button";
      const dot = element("i");
      dot.style.background = graphColor(row.type);
      button.append(dot, element("span", "", shortLabel(row.label, 60)));
      if (row.status) button.append(element("small", "", labels[row.status] || row.status));
      if (row.inferred) {
        button.append(element("small", "inferred-tag", `inferred ${Math.round((row.confidence || 0) * 100)}%`));
        button.title = `Inferred by dreaming (${row.rule}). Evidence: ${row.evidence || "none recorded"}`;
      }
      button.addEventListener("click", () => selectGraphNode(row.id));
      const item = element("li");
      item.append(button);
      if (row.inferred) {
        const reject = element("button", "link-button reject-link", "Not related");
        reject.type = "button";
        reject.title = "Remove this inferred link; dreaming will not infer it again";
        const [src, dst] = row.direction === "out" ? [node.id, row.id] : [row.id, node.id];
        reject.addEventListener("click", () => rejectGraphLink(src, row.rel, dst, node.id));
        item.append(reject);
      }
      list.append(item);
    }
    section.append(list);
    if (rows.length > 12) section.append(element("p", "focus-hint", `${rows.length - 12} more. Focus this node to see them all.`));
    parts.push(section);
  }
  panel.replaceChildren(...parts);
}

function renderGraphLists() {
  const insights = graphData.nodes
    .filter((node) => node.type === "insight" && !node.archived)
    .sort((a, b) => (b.score || 0) - (a.score || 0))
    .slice(0, 8);
  const button = (text, id) => {
    const item = element("li");
    const control = element("button", "link-button", text);
    control.type = "button";
    control.addEventListener("click", () => selectGraphNode(id));
    item.append(control);
    return item;
  };
  $("graph-top").replaceChildren(
    ...(insights.length
      ? insights.map((node) => button(`${KIND_LABELS[node.kind] || "Fact"}: ${node.summary || node.label}`, node.id))
      : [element("li", "focus-hint", "No knowledge recorded yet.")]),
  );
  $("graph-hotspots").replaceChildren(
    ...(graphData.hotspots?.length
      ? graphData.hotspots.map((row) => button(`${row.file} · ${row.done}/${row.attempts} verified`, row.id))
      : [element("li", "focus-hint", "No file has two or more finished attempts yet.")]),
  );
  $("graph-refines").replaceChildren(
    ...(graphData.refine?.length
      ? [...graphData.refine].reverse().map((row) =>
          element(
            "li",
            "",
            `${relativeTime(row.at)}: ${row.nodes} nodes, ${row.merged} merged, ${row.faded} faded, ${row.stale_files} stale files, ${row.pruned_tasks + row.pruned_files} pruned`,
          ),
        )
      : [element("li", "focus-hint", "Refinement runs when a run ends, or use Refine now.")]),
  );
  $("graph-dreams").replaceChildren(
    ...(graphData.dreams?.length
      ? [...graphData.dreams].reverse().map((row) =>
          element(
            "li",
            "",
            `${relativeTime(row.at)} (dream ${row.tick}): ${row.inferred.new} new links, ${row.inferred.dropped} dropped, ` +
              `${row.archived} archived, ${row.forgotten} forgotten, ${row.inferred_total} inferred in total`,
          ),
        )
      : [element("li", "focus-hint", "Dreams run every few hours during a run and when it ends, or use Dream now.")]),
  );
  $("graph-proposals").replaceChildren(
    ...(graphData.proposals?.length
      ? graphData.proposals.map((row) => {
          const item = element("li", "", `${row.members.length} overlapping notes (${row.kinds.join(", ")}) `);
          const view = element("button", "link-button", "View");
          view.type = "button";
          view.addEventListener("click", () => selectGraphNode(row.members[0], { focus: true }));
          item.append(view);
          return item;
        })
      : [element("li", "focus-hint", "No overlapping knowledge to consolidate.")]),
  );
  loadForgotten();
}

let forgottenLimit = 8,
  forgottenKey = "";
async function loadForgotten(force = false) {
  const key = `${graphRevision}|${forgottenLimit}`;
  if (!force && key === forgottenKey) return;
  forgottenKey = key;
  try {
    const result = await boardJSON(`/api/graph/history?limit=${forgottenLimit}`);
    $("graph-forgotten-count").textContent = countText(result.total);
    $("graph-forgotten-more").hidden = result.total <= result.rows.length;
    $("graph-forgotten").replaceChildren(
      ...(result.rows.length
        ? result.rows.map((row) => {
            const item = element("li", "forgotten-row");
            const text = element("span", "", `${graphTypeLabel(row)}: ${shortLabel(row.summary || row.label, 80)}`);
            text.title = `${row.op}: ${row.reason || "no reason recorded"}`;
            const why = element("small", "", `${row.op} · ${shortLabel(row.reason || "", 70)}`);
            const restore = element("button", "link-button", "Restore");
            restore.type = "button";
            restore.dataset.id = row.id;
            restore.addEventListener("click", () => restoreGraphNode(row.id));
            item.append(text, why, restore);
            return item;
          })
        : [element("li", "focus-hint", "Nothing has been forgotten.")]),
    );
  } catch (error) {
    forgottenKey = "";
    $("graph-forgotten").replaceChildren(element("li", "form-error", "Could not read the history. " + error.message));
  }
}

async function restoreGraphNode(id) {
  try {
    const result = await graphPost({ action: "restore", id });
    graphActionStatus(result.message);
    graphRevision = null;
    forgottenKey = "";
    selectGraphNode(result.node.id, { focus: true, keepStatus: true });
  } catch (error) {
    graphStatus(error.message, true);
  }
}

async function rejectGraphLink(src, rel, dst, selected) {
  try {
    const result = await graphPost({ action: "reject_link", src, rel, dst });
    graphActionStatus(result.message);
    graphRevision = null;
    loadNodeDetail(selected, true);
    renderGraphView();
  } catch (error) {
    graphStatus(error.message, true);
  }
}

function resetGraphScope() {
  userScopeChange();
  graphFocus = null;
  graphFitPending = true;
  $("graph-query").value = "";
  renderGraphView();
}

async function graphPost(data) {
  return boardAction("/api/graph", { request_id: crypto.randomUUID(), ...data });
}

async function rateGraphNode(id, verdict) {
  try {
    const result = await graphPost({ action: "rate", id, verdict });
    graphActionStatus(result.message);
    graphRevision = null;
    loadNodeDetail(id, true);
    renderGraphView();
  } catch (error) {
    graphStatus(error.message, true);
  }
}

function boardRunFor(node) {
  return snapshot.runs.find((run) => run.control_id === node.run_id);
}

function openGraphTask(node) {
  const run = boardRunFor(node);
  if (!run) {
    graphStatus("That run's task ledger is no longer on this computer; the graph keeps its summary.", true);
    return;
  }
  graphPendingTask = run.id + ":" + node.task_id;
  if (!["all", run.id].includes($("run").value)) {
    $("run").value = "all";
    $("run").dispatchEvent(new Event("change"));
    graphStatus("Loading all runs to open that task…");
  } else openPendingGraphTask();
}

function openPendingGraphTask() {
  if (!graphPendingTask || !snapshot) return;
  const task = snapshot.tasks.find((item) => item.key === graphPendingTask);
  if (!task && snapshot.selected_run !== "all" && $("run").value === "all") return;
  graphPendingTask = null;
  if (task) openTask(task);
  else graphStatus("That task is not in the saved ledgers any more.", true);
}

function showGraphTasks(node) {
  $("search").value = node.type === "file" ? node.label : "";
  if (node.type === "area" && [...$("area").options].some((option) => option.value === node.label))
    $("area").value = node.label;
  changeFilters();
  selectView("history");
}

function openGraphNode(id) {
  if ($("task-dialog").open) $("task-dialog").close();
  graphSelected = id;
  graphFocus = id;
  graphFitPending = true;
  selectView("graph");
  loadNodeDetail(id, true);
}

function taskFilePaths(task) {
  const values = [];
  for (const key of ["paths", "owned_paths"]) {
    const value = task[key];
    if (Array.isArray(value)) values.push(...value);
    else if (typeof value === "string") values.push(value);
  }
  if (typeof task.file === "string") values.push(task.file);
  return [...new Set(values.filter((value) => typeof value === "string" && value.trim()))].slice(0, 20);
}

// Called from openTask, which re-runs on every refresh while the dialog is open.
async function renderTaskKnowledge(task) {
  const summary = snapshot?.graph;
  const key = task.key + "|" + (summary?.revision ?? "none");
  if (key === taskKnowledgeKey) return;
  const section = $("task-knowledge");
  if (!taskKnowledgeKey.startsWith(task.key + "|")) section.hidden = true;
  taskKnowledgeKey = key;
  if (!summary?.nodes) {
    section.hidden = true;
    return;
  }
  const params = new URLSearchParams({ area: task.area, text: task.title.slice(0, 200) });
  for (const file of taskFilePaths(task)) params.append("file", file);
  const runId = snapshot.runs.find((run) => run.id === task.run_id)?.control_id;
  if (runId) params.set("exclude", `task:${runId}:${task.id}`);
  try {
    const result = await boardJSON("/api/graph/context?" + params);
    if (selectedTask !== task.key || taskKnowledgeKey !== key) return;
    section.hidden = false;
    $("task-knowledge-body").replaceChildren(...taskKnowledgeParts(result));
  } catch (error) {
    if (selectedTask !== task.key) return;
    section.hidden = false;
    $("task-knowledge-body").replaceChildren(element("p", "form-error", "Could not read the knowledge graph. " + error.message));
    taskKnowledgeKey = "";
  }
}

function taskKnowledgeParts(result) {
  const parts = [];
  const list = (title, rows, render) => {
    if (!rows.length) return;
    const block = element("div", "knowledge-block");
    const items = element("ul");
    for (const row of rows) {
      const item = element("li");
      const view = element("button", "link-button", render(row));
      view.type = "button";
      view.title = "View in the knowledge graph";
      view.addEventListener("click", () => openGraphNode(row.id));
      item.append(view);
      items.append(item);
    }
    block.append(element("h4", "", title), items);
    parts.push(block);
  };
  for (const row of result.files)
    parts.push(
      element(
        "p",
        "dialog-context",
        `${row.file}: ${row.done} of ${row.attempts} earlier attempts verified` +
          (row.blocked ? `, ${row.blocked} blocked` : "") +
          (row.stale ? " (no longer tracked)" : ""),
      ),
    );
  list("Knowledge to apply", result.insights, (row) => `${KIND_LABELS[row.kind] || "Fact"}: ${row.summary || row.label}`);
  list("Earlier attempts nearby", result.past_tasks.slice(0, 5), (row) =>
    `${labels[row.status] || row.status || "Task"}: ${row.label}` + (row.outcome_note ? ` (${shortLabel(row.outcome_note, 90)})` : ""),
  );
  list("Blockers seen here", result.blockers, (row) => row.label);
  list("Usually changed together", result.co_changed, (row) => row.label);
  if (!parts.length)
    parts.push(element("p", "focus-hint", "Nothing recorded yet for this task's files or area. The skill will add what it learns."));
  return parts;
}

function exportGraph() {
  if (!graphData) return;
  const url = URL.createObjectURL(
    new Blob([JSON.stringify({ repo: snapshot.repo, exported_at: new Date().toISOString(), ...graphData }, null, 2)], {
      type: "application/json",
    }),
  );
  const link = element("a");
  link.href = url;
  link.download = "improve-knowledge-graph.json";
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function renderGraphTypes() {
  $("graph-types").replaceChildren(
    ...GRAPH_TYPES.map(([type, label, color]) => {
      const chip = element("button", "graph-type", label);
      chip.type = "button";
      chip.style.setProperty("--swatch", color);
      chip.setAttribute("aria-pressed", String(graphTypes.has(type)));
      chip.addEventListener("click", () => {
        if (graphTypes.has(type) && graphTypes.size > 1) graphTypes.delete(type);
        else graphTypes.add(type);
        userScopeChange();
        graphFocus = null;
        renderGraphTypes();
        renderGraphView();
      });
      return chip;
    }),
  );
}

function initGraph() {
  renderGraphTypes();
  $("graph-tab").addEventListener("click", () => selectView("graph"));
  let searchTimer = null;
  $("graph-query").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      userScopeChange();
      graphFocus = null;
      renderGraphView();
    }, 250);
  });
  $("graph-archived").addEventListener("change", () => {
    userScopeChange();
    renderGraphView();
  });
  $("graph-reset").addEventListener("click", resetGraphScope);
  $("graph-zoom-in").addEventListener("click", () => zoomGraph(1.25));
  $("graph-zoom-out").addEventListener("click", () => zoomGraph(0.8));
  $("graph-fit").addEventListener("click", fitGraph);
  $("graph-forgotten-more").addEventListener("click", () => {
    forgottenLimit = Math.min(100, forgottenLimit + 20);
    loadForgotten(true);
  });
  $("graph-dream").addEventListener("click", async () => {
    $("graph-dream").disabled = true;
    try {
      graphActionStatus((await graphPost({ action: "dream" })).message);
      graphRevision = null;
      forgottenKey = "";
      renderGraphView();
    } catch (error) {
      graphStatus(error.message, true);
    } finally {
      $("graph-dream").disabled = false;
    }
  });
  $("graph-refine").addEventListener("click", async () => {
    $("graph-refine").disabled = true;
    try {
      graphActionStatus((await graphPost({ action: "refine" })).message);
      graphRevision = null;
      renderGraphView();
    } catch (error) {
      graphStatus(error.message, true);
    } finally {
      $("graph-refine").disabled = false;
    }
  });
  $("graph-add").addEventListener("click", () => {
    $("graph-note-error").hidden = true;
    const node = graphData?.nodes.find((item) => item.id === graphSelected);
    if (node && ["file", "area"].includes(node.type) && !$("graph-note-about").value)
      $("graph-note-about").value = node.type === "file" ? node.label : node.id;
    $("graph-note-dialog").showModal();
    $("graph-note-text").focus();
  });
  for (const id of ["graph-note-text", "graph-note-kind", "graph-note-about"])
    $(id).addEventListener("input", () => (graphNoteRetry = null));
  $("graph-note-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = $("graph-note-text").value.trim();
    if (text.length < 8) {
      $("graph-note-error").hidden = false;
      $("graph-note-error").textContent = "Write at least a short sentence (8 characters).";
      return;
    }
    // Keep one id per draft so a lost response cannot record it twice.
    graphNoteRetry ??= crypto.randomUUID();
    $("graph-note-submit").disabled = true;
    try {
      const about = $("graph-note-about")
        .value.split(",")
        .map((value) => value.trim())
        .filter(Boolean)
        .map((value) => (/^(file|area|gate|insight|blocker|task|run):/.test(value) ? value : "file:" + value));
      const result = await boardAction("/api/graph", {
        action: "note",
        request_id: graphNoteRetry,
        text,
        kind: $("graph-note-kind").value,
        about,
      });
      graphNoteRetry = null;
      $("graph-note-text").value = $("graph-note-about").value = "";
      $("graph-note-dialog").close();
      graphActionStatus(result.message);
      graphRevision = null;
      selectGraphNode(result.node.id, { keepStatus: true });
    } catch (error) {
      $("graph-note-error").hidden = false;
      $("graph-note-error").textContent = error.message;
    } finally {
      $("graph-note-submit").disabled = false;
    }
  });

  const canvas = $("graph-canvas");
  let drag = null;
  canvas.addEventListener("pointerdown", (event) => {
    const group = event.target.closest?.(".graph-node");
    const point = group && graphLayout.points.get(group.dataset.id);
    drag = {
      id: group?.dataset.id,
      point,
      startX: event.clientX,
      startY: event.clientY,
      originX: graphViewport.x,
      originY: graphViewport.y,
      moved: false,
    };
    if (point) point.fixed = true;
    canvas.setPointerCapture(event.pointerId);
  });
  canvas.addEventListener("pointermove", (event) => {
    if (!drag) return;
    const dx = event.clientX - drag.startX,
      dy = event.clientY - drag.startY;
    if (Math.abs(dx) + Math.abs(dy) > 4) drag.moved = true;
    if (!drag.moved) return;
    if (drag.point) {
      const box = canvas.getBoundingClientRect();
      drag.point.x = (event.clientX - box.left - graphViewport.x) / graphViewport.scale;
      drag.point.y = (event.clientY - box.top - graphViewport.y) / graphViewport.scale;
      graphLayout.reheat(0.15);
      animateGraph();
    } else {
      graphViewport.x = drag.originX + dx;
      graphViewport.y = drag.originY + dy;
    }
    positionGraph();
  });
  const release = () => {
    if (!drag) return;
    if (drag.point) drag.point.fixed = false;
    if (!drag.moved && drag.id) selectGraphNode(drag.id);
    drag = null;
  };
  canvas.addEventListener("pointerup", release);
  canvas.addEventListener("pointercancel", () => {
    if (drag?.point) drag.point.fixed = false;
    drag = null;
  });
  canvas.addEventListener(
    "wheel",
    (event) => {
      event.preventDefault();
      const box = canvas.getBoundingClientRect();
      zoomGraph(event.deltaY < 0 ? 1.12 : 0.89, event.clientX - box.left, event.clientY - box.top);
    },
    { passive: false },
  );
  canvas.addEventListener("keydown", (event) => {
    const group = event.target.closest?.(".graph-node");
    if (group && (event.key === "Enter" || event.key === " ")) {
      event.preventDefault();
      selectGraphNode(group.dataset.id);
    }
  });
  new ResizeObserver(() => {
    if (activeView === "graph" && graphFitPending) fitGraph();
  }).observe(canvas);
}
