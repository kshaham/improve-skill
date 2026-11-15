"use strict";
// Motion that depends on what changed. The board rebuilds a list whenever its data changes, so
// an entrance written in CSS alone would replay on every card. These helpers mark only the new
// cards and rows, the tasks whose status changed, and the figures whose value changed; board.css
// holds the animations and turns them off under reduced motion.
const ENTER_STAGGER_LIMIT = 10;
const motionSeen = new Map(); // list name -> keys that list has already shown
const motionStatus = new Map(); // task key -> the status last drawn on the board

// The positions in `keys` that `list` has not shown before; all of them are remembered.
function newEntries(list, keys) {
  const seen = motionSeen.get(list) || new Set();
  motionSeen.set(list, seen);
  const fresh = [];
  keys.forEach((key, index) => {
    if (!seen.has(key)) fresh.push(index);
    seen.add(key);
  });
  return fresh;
}

// The keys whose status differs from the one drawn last time; a first sighting is not a change.
function statusChanges(tasks) {
  const changed = new Set();
  for (const task of tasks) {
    const previous = motionStatus.get(task.key);
    if (previous !== undefined && previous !== task.status) changed.add(task.key);
    motionStatus.set(task.key, task.status);
  }
  return changed;
}

function markEntrances(list, elements) {
  const fresh = newEntries(
    list,
    elements.map((item) => item.dataset.key),
  );
  fresh.forEach((index, order) => {
    elements[index].dataset.enter = "";
    elements[index].style.setProperty("--i", Math.min(order, ENTER_STAGGER_LIMIT));
  });
}

function markMoves(cards, tasks) {
  const changed = statusChanges(tasks);
  for (const card of cards) if (changed.has(card.dataset.key)) card.dataset.moved = "";
}

// Write a figure and, when it changed, replay its tick.
function tickText(node, text) {
  if (node.textContent === text) return;
  node.textContent = text;
  delete node.dataset.tick;
  void node.offsetWidth; // Restart the animation: the attribute must be seen to leave and return.
  node.dataset.tick = "";
}

// Place the sliding indicator under the selected tab.
function positionTabIndicator() {
  const tabs = document.querySelector(".view-tabs");
  const tab = tabs?.querySelector('[aria-selected="true"]');
  if (!tab || !tab.offsetWidth) return;
  tabs.style.setProperty("--tab-x", tab.offsetLeft + "px");
  tabs.style.setProperty("--tab-y", tab.offsetTop + tab.offsetHeight - 2 + "px");
  tabs.style.setProperty("--tab-w", String(tab.offsetWidth));
  tabs.dataset.indicator = "";
}

function initMotion() {
  window.addEventListener("resize", positionTabIndicator);
}
