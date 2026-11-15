"use strict";
// Other codebases. Every repository has its own board, knowledge graph, and wiki, and a board
// shows only its own. When improve is running in another codebase, or ran there more recently,
// the server says so in `boards`. The board at the usual address (port 8765) then sends its
// page there, to the same tab, and that board says where the reader came from. A reader who
// came back on purpose (the links here add ?stay=1) stays, and sees a notice instead.
const BOARD_STAY_KEY = "improve-board-stay";
const LOCAL_BOARD = /^http:\/\/127\.0\.0\.1:\d{1,5}$/;
let boardWorkspace = null,
  otherBoardsSignature = "";

function boardStay() {
  try {
    return sessionStorage.getItem(BOARD_STAY_KEY) === "1";
  } catch (error) {
    // Blocked storage only loses the choice to stay.
    void error;
    return false;
  }
}

function setBoardStay() {
  try {
    sessionStorage.setItem(BOARD_STAY_KEY, "1");
  } catch (error) {
    void error;
  }
}

// Only a board on this computer is ever opened, and only from a page opened on this computer:
// a phone that reaches this board through a tunnel cannot reach 127.0.0.1.
function localBoardUrl(url) {
  const local = ["127.0.0.1", "localhost"].includes(location.hostname);
  return local && typeof url === "string" && LOCAL_BOARD.test(url) ? url : null;
}

// The same tab on the other board: views are shared, task and node ids are not. `from` tells
// that board which codebase the reader was brought from.
function sameViewOn(url, from) {
  const query = from ? "?from=" + encodeURIComponent(from) : "";
  if (activeView === "board") return url + "/" + query;
  return url + "/" + query + "#view=" + activeView + (activeView === "graph" && wikiMode ? "&wiki=index" : "");
}

// A reader the usual address sent here is told so, with the way back.
function renderArrival(data, others) {
  const from = new URLSearchParams(location.search).get("from");
  const origin = from && others.find((row) => row.repo === from);
  $("arrived-board").hidden = !origin;
  if (!origin) return;
  $("arrived-board-name").textContent = data.repo;
  $("arrived-board-from").textContent = origin.repo;
  $("arrived-board-link").href = origin.url + "/?stay=1";
}

function renderBoards(data) {
  // This address now serves another codebase (its board took the port): start again from it.
  if (boardWorkspace && data.workspace_id && data.workspace_id !== boardWorkspace) {
    location.reload();
    return;
  }
  boardWorkspace = data.workspace_id || boardWorkspace;
  const boards = data.boards || {};
  const others = (boards.others || []).filter((row) => localBoardUrl(row.url));
  const signature = JSON.stringify(others);
  if (signature !== otherBoardsSignature) {
    otherBoardsSignature = signature;
    $("other-boards").hidden = !others.length;
    $("other-boards-list").replaceChildren(
      ...others.map((row) => {
        const link = element("a", "other-board", row.repo);
        link.href = row.url + "/?stay=1";
        link.dataset.live = String(!!row.live);
        link.title = row.live ? "improve is running here" : "Open this codebase's board";
        return link;
      }),
    );
  }
  renderArrival(data, others);
  const active = boards.active && localBoardUrl(boards.active.url) ? boards.active : null;
  $("active-board").hidden = !active;
  if (!active) return;
  $("active-board-name").textContent = active.repo;
  $("active-board-link").href = sameViewOn(active.url);
  // Only the board at the usual address sends its page on (the server decides `follow`).
  if (boards.follow && !boardStay()) location.replace(sameViewOn(active.url, data.repo));
}

function initBoards() {
  if (new URLSearchParams(location.search).has("stay")) setBoardStay();
}
