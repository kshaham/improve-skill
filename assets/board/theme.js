"use strict";
// Colour theme: follows the system by default, with a per-browser override from the footer.
const THEME_KEY = "improve-board-theme";
const THEME_CYCLE = ["auto", "light", "dark"];
let themePreference = "auto",
  themeReveal = null;

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

// The new palette opens in a circle from the toggle where the browser can do it; reduced motion
// and older browsers switch at once.
function cycleTheme(event) {
  const next = THEME_CYCLE[(THEME_CYCLE.indexOf(themePreference) + 1) % THEME_CYCLE.length];
  const root = document.documentElement;
  const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (!document.startViewTransition || still) {
    setThemePreference(next);
    return;
  }
  const box = event?.currentTarget?.getBoundingClientRect();
  root.style.setProperty("--reveal-x", (box ? box.left + box.width / 2 : window.innerWidth / 2) + "px");
  root.style.setProperty("--reveal-y", (box ? box.top + box.height / 2 : window.innerHeight) + "px");
  root.dataset.themeReveal = "";
  const transition = document.startViewTransition(() => setThemePreference(next));
  themeReveal = transition;
  // A second click replaces the first transition; only the latest one may clear the mark.
  transition.finished.finally(() => {
    if (themeReveal === transition) delete root.dataset.themeReveal;
  });
}

// Runs before the board renders so a dark preference never flashes the light palette.
themePreference = readThemePreference();
applyTheme();

function initTheme() {
  document.getElementById("theme-toggle").addEventListener("click", cycleTheme);
  applyTheme();
}
