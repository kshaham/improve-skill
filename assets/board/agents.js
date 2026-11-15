"use strict";
// Parallel helpers: a switch and a 1-10 slider at the top of the focus panel. The choice travels
// with the focus request as `agents` (references/board-control.md); off sends no field, and the
// assistant then decides for itself when a helper is worth it.
const AGENTS_MAX = 10;
const AGENTS_DEFAULT = 5;
let focusAgents = 0, // What a save would send: 0 is off.
  agentsChoice = AGENTS_DEFAULT; // The slider's value, kept while the switch is off.

function agentsLabel(count) {
  return count === 1 ? "1 agent" : `${count} agents`;
}

function buildAgents() {
  // One seat per possible helper; the lit ones are the team.
  for (let index = 0; index < AGENTS_MAX; index++) {
    const seat = element("i");
    seat.style.setProperty("--i", index);
    $("agents-track").append(seat);
  }
  $("agents-enabled").addEventListener("change", (event) => {
    focusAgents = event.target.checked ? agentsChoice : 0;
    focusDirty = true;
    renderFocus();
  });
  $("agents-count").addEventListener("input", (event) => {
    agentsChoice = Math.min(AGENTS_MAX, Math.max(1, Number(event.target.value) || AGENTS_DEFAULT));
    focusAgents = agentsChoice;
    focusDirty = true;
    renderFocus();
  });
}

function renderAgents(allowed) {
  const on = focusAgents > 0;
  if (on) agentsChoice = focusAgents;
  const control = $("agents-control"),
    slider = $("agents-count");
  control.dataset.on = String(on);
  control.style.setProperty("--ratio", String((agentsChoice - 1) / (AGENTS_MAX - 1)));
  $("agents-enabled").checked = on;
  $("agents-enabled").disabled = !allowed;
  slider.disabled = !allowed || !on;
  if (Number(slider.value) !== agentsChoice) slider.value = String(agentsChoice);
  slider.setAttribute("aria-valuetext", agentsLabel(agentsChoice));
  [...$("agents-track").children].forEach((seat, index) =>
    seat.toggleAttribute("data-lit", on && index < agentsChoice),
  );
  tickText($("agents-value"), String(agentsChoice));
  $("agents-note").textContent = on
    ? `Up to ${agentsLabel(agentsChoice)} at a time, for finding and reviewing work. This uses more tokens.`
    : "Off: the assistant decides when a helper is worth it.";
}
