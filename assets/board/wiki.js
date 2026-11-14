"use strict";
// Knowledge view: the wiki. The skill rebuilds it from the knowledge graph only when an improve
// session finishes, so it is a settled digest: what the graph knows, what changed, and what its
// dreaming found. Every page is built from data with DOM elements, never from HTML strings.

const WIKI_FIXED_PAGES = [
  ["index", "Overview"],
  ["knowledge", "Knowledge"],
  ["changes", "Changes"],
  ["dreams", "Dreams"],
  ["sessions", "Sessions"],
];
let wikiData = null,
  wikiPage = "index",
  wikiLoading = false,
  wikiMode = false;

function applyKnowledgeMode(wiki) {
  wikiMode = wiki;
  $("knowledge-graph-mode").setAttribute("aria-pressed", String(!wiki));
  $("knowledge-wiki-mode").setAttribute("aria-pressed", String(wiki));
  $("graph-panel").hidden = wiki;
  $("wiki-panel").hidden = !wiki;
}

function setKnowledgeMode(wiki) {
  applyKnowledgeMode(wiki);
  if (wiki) loadWiki();
  else {
    graphFitPending = true;
    renderGraphView();
  }
}

// A link to a graph node always shows the graph; the caller renders it.
function showGraphMode() {
  if (!wikiMode) return;
  applyKnowledgeMode(false);
  graphFitPending = true;
}

async function loadWiki() {
  if (wikiLoading) return;
  wikiLoading = true;
  try {
    wikiData = await boardJSON("/api/wiki");
    renderWiki();
  } catch (error) {
    $("wiki-page").replaceChildren(element("p", "form-error", "The wiki could not load. " + error.message));
  } finally {
    wikiLoading = false;
  }
}

function wikiPages() {
  const pages = wikiData?.pages || {};
  const areas = (pages.index?.areas || []).map((row) => [row.page, row.name]);
  return [...WIKI_FIXED_PAGES, ...areas].filter(([name]) => pages[name]);
}

function renderWiki() {
  const pages = wikiData?.pages || {};
  if (!pages.index) {
    $("wiki-pages").replaceChildren();
    $("wiki-page").replaceChildren(
      element("h3", "", "No wiki yet"),
      element(
        "p",
        "focus-hint",
        "The wiki is rebuilt from the knowledge graph when an improve session finishes. It appears after the next session ends.",
      ),
    );
    return;
  }
  if (!pages[wikiPage]) wikiPage = "index";
  const list = element("ul", "wiki-page-list");
  for (const [name, title] of wikiPages()) {
    const item = element("li");
    const button = element("button", "link-button" + (name.startsWith("areas/") ? " wiki-area" : ""), title);
    button.type = "button";
    if (name === wikiPage) button.setAttribute("aria-current", "page");
    button.addEventListener("click", () => openWikiPage(name));
    item.append(button);
    list.append(item);
  }
  $("wiki-pages").replaceChildren(list);
  $("wiki-page").replaceChildren(...wikiPageContent(wikiPage, pages[wikiPage], pages));
}

function openWikiPage(name) {
  wikiPage = name;
  renderWiki();
  $("wiki-page").focus({ preventScroll: true });
}

function wikiNoteRows(rows) {
  if (!rows.length) return [element("p", "focus-hint", "None recorded.")];
  const list = element("ul", "wiki-notes");
  for (const row of rows) {
    const item = element("li");
    const tags = element("span", "wiki-tags");
    tags.append(element("span", "wiki-tag kind-" + (KIND_LABELS[row.kind] ? row.kind : "fact"), KIND_LABELS[row.kind] || "Fact"));
    if (row.verdict === "proven" || row.verdict === "contested")
      tags.append(element("span", "wiki-tag verdict-" + row.verdict, row.verdict === "proven" ? "Proven" : "Contested"));
    if (row.pinned) tags.append(element("span", "wiki-tag pinned", "Pinned"));
    item.append(tags, element("span", "wiki-text", row.text));
    const where = [...(row.areas || "").split(" ").filter(Boolean), ...(row.files || "").split(" ").filter(Boolean)];
    if (where.length) item.append(element("small", "wiki-where", where.join(" · ")));
    list.append(item);
  }
  return [list];
}

function wikiHotspots(rows) {
  if (!rows.length) return [element("p", "focus-hint", "None: no file keeps failing.")];
  const list = element("ul", "wiki-areas");
  for (const row of rows) {
    const item = element("li");
    item.append(element("span", "wiki-path", row.file), element("small", "", `${row.done}/${row.attempts} verified`));
    list.append(item);
  }
  return [list];
}

function wikiSection(title, children) {
  const section = element("section", "wiki-section");
  section.append(element("h4", "", title), ...children);
  return section;
}

function wikiPageContent(name, page, pages) {
  const heading = element("h3", "", page.title || name);
  if (name === "index") {
    const counts = page.counts || {};
    const facts = element("p", "wiki-stamp", `Rebuilt ${relativeTime(page.built_at)} when session ${page.run_id || "?"} finished · graph revision ${page.revision}, dream tick ${page.tick}`);
    const summary = element(
      "p",
      "",
      `${countText(counts.notes || 0)} live notes (${counts.proven || 0} proven, ${counts.contested || 0} contested, ${counts.archived || 0} archived) across ${counts.areas || 0} areas and ${countText(counts.files || 0)} files.`,
    );
    const last = page.last_session || {};
    const areas = element("ul", "wiki-areas");
    for (const row of page.areas || []) {
      const item = element("li");
      const button = element("button", "link-button", row.name);
      button.type = "button";
      button.addEventListener("click", () => openWikiPage(row.page));
      item.append(button, element("small", "", `${row.notes} notes · ${row.done}/${row.attempts} verified`));
      areas.append(item);
    }
    return [
      heading,
      facts,
      summary,
      wikiSection("Key knowledge", wikiNoteRows(page.key || [])),
      wikiSection("Areas", [page.areas?.length ? areas : element("p", "focus-hint", "None yet.")]),
      wikiSection("Files that resist change", wikiHotspots(page.hotspots || [])),
      wikiSection("Last session", [element("p", "", `${last.added || 0} added, ${last.removed || 0} removed, ${last.verdicts || 0} verdicts changed.`)]),
    ];
  }
  if (name === "knowledge")
    return [heading, ...(page.groups || []).map((group) => wikiSection((KIND_LABELS[group.kind] || "Fact") + "s", wikiNoteRows(group.notes)))];
  if (name === "changes")
    return [
      heading,
      ...(page.sessions || []).map((session) => {
        const rows = [
          ...session.added.map((row) => ["Added", row.text, "added"]),
          ...session.removed.map((row) => [`Removed: ${row.reason}`, row.text, "removed"]),
          ...session.verdicts.map((row) => [`Verdict ${row.from} → ${row.to}`, row.text, "verdict"]),
        ];
        const list = element("ul", "wiki-changes");
        for (const [label, text, kind] of rows) {
          const item = element("li", "change-" + kind);
          item.append(element("strong", "", label), element("span", "wiki-text", text));
          list.append(item);
        }
        const counts = session.counts || {};
        return wikiSection(`Session ${session.run_id || "?"} · ${relativeTime(session.built_at)}`, [
          element("p", "focus-hint", `${counts.added || 0} added, ${counts.removed || 0} removed, ${counts.verdicts || 0} verdicts changed${session.first ? " · first build" : ""}`),
          rows.length ? list : element("p", "focus-hint", "No changes."),
        ]);
      }),
    ];
  if (name === "sessions") {
    const list = element("ul", "wiki-changes");
    for (const row of page.runs || []) {
      const item = element("li", row.outcome === "completed" ? "change-added" : row.outcome.startsWith("halted") ? "change-verdict" : "");
      item.append(
        element("strong", "", `${row.run_id} · ${row.outcome}`),
        element("span", "wiki-text", `${row.done}/${row.tasks} done, ${row.rejected} rejected, ${row.blocked} blocked · ${row.notes} notes recorded`),
      );
      list.append(item);
    }
    return [
      heading,
      element("p", "", "Each recorded improve session, newest first: what it attempted and the knowledge it recorded."),
      page.runs?.length ? list : element("p", "focus-hint", "No session recorded yet."),
    ];
  }
  if (name === "dreams") {
    const inferred = page.inferred || {};
    const proposals = element("ul", "wiki-changes");
    for (const row of page.proposals || []) {
      const item = element("li");
      item.append(element("strong", "", `${row.members} notes`), element("span", "wiki-text", row.text || row.id));
      proposals.append(item);
    }
    return [
      heading,
      element("p", "wiki-stamp", `Latest dream: tick ${page.tick}${page.at ? " · " + relativeTime(page.at) : ""}`),
      element("p", "", `Inferred links: ${inferred.new || 0} new, ${inferred.kept || 0} kept, ${inferred.dropped || 0} dropped. Knowledge archived ${page.archived || 0}, forgotten ${page.forgotten || 0}. ${page.proven || 0} proven, ${page.contested || 0} contested.`),
      wikiSection("Consolidation proposals", [page.proposals?.length ? proposals : element("p", "focus-hint", "None.")]),
      wikiSection("Knowledge gaps", page.gaps?.length ? [element("ul", "wiki-gaps")].map((list) => (list.append(...page.gaps.map((gap) => element("li", "", gap))), list)) : [element("p", "focus-hint", "None.")]),
    ];
  }
  // An area page carries note ids; the rows live once, on the knowledge page.
  const rows = new Map((pages.knowledge?.groups || []).flatMap((group) => group.notes).map((row) => [row.id, row]));
  const files = element("ul", "wiki-files");
  for (const row of page.files || []) files.append(element("li", "", `${row.path} · ${row.attempts} attempts`));
  return [
    heading,
    element("p", "", `${page.done || 0}/${page.attempts || 0} attempts verified${typeof page.success === "number" ? ` · success ${Math.round(page.success * 100)}%` : ""}`),
    wikiSection("Knowledge", wikiNoteRows((page.knowledge || []).map((id) => rows.get(id)).filter(Boolean))),
    wikiSection("Files", [page.files?.length ? files : element("p", "focus-hint", "None recorded.")]),
  ];
}

function bindWiki() {
  $("knowledge-graph-mode").addEventListener("click", () => setKnowledgeMode(false));
  $("knowledge-wiki-mode").addEventListener("click", () => setKnowledgeMode(true));
}
