"use strict";
// Colour theme: follows the system by default, with a per-browser override from the footer.
const THEME_KEY = "improve-board-theme";
const THEME_CYCLE = ["auto", "light", "dark"];
let themePreference = "auto";

function readThemePreference() {
  try {
    const stored = localStorage.getItem(THEME_KEY);
    return THEME_CYCLE.includes(stored) ? stored : "auto";
  } catch (error) {
    // Blocked storage means the system preference applies on every load.
    void error;
    return "auto";
  }
}

function applyTheme() {
  const root = document.documentElement;
  if (themePreference === "auto") delete root.dataset.theme;
  else root.dataset.theme = themePreference;
  const toggle = document.getElementById("theme-toggle");
  if (toggle) {
    toggle.textContent = "Theme: " + themePreference;
    toggle.setAttribute(
      "aria-label",
      "Colour theme is " + themePreference + ". Activate to switch between auto, light, and dark.",
    );
  }
}

function setThemePreference(preference) {
  themePreference = preference;
  try {
    if (preference === "auto") localStorage.removeItem(THEME_KEY);
    else localStorage.setItem(THEME_KEY, preference);
  } catch (error) {
    void error;
  }
  applyTheme();
}

function cycleTheme() {
  const next = THEME_CYCLE[(THEME_CYCLE.indexOf(themePreference) + 1) % THEME_CYCLE.length];
  setThemePreference(next);
}

// Runs before the board renders so a dark preference never flashes the light palette.
themePreference = readThemePreference();
applyTheme();

function initTheme() {
  document.getElementById("theme-toggle").addEventListener("click", cycleTheme);
  applyTheme();
}
