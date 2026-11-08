"use strict";
// Turn a spoken phrase into a board request. Pure functions: no DOM, no network.
const VOICE_AREA_WORDS = [
  ["gate-speed", /\b(gate speed|build speed|build times?|ci speed|faster (builds|tests|ci))\b/],
  ["accessibility", /\b(accessibility|accessible|a11y|screen readers?|keyboard)\b/],
  ["security", /\b(security|secure|vulnerabilit(y|ies)|privacy|auth(entication)?)\b/],
  ["performance", /\b(performance|speed|fast(er)?|slow|latency|memory|load times?)\b/],
  ["coverage", /\b(tests?|testing|coverage)\b/],
  ["docs", /\b(docs|documentation|readme|guides?)\b/],
  ["ui", /\b(ui|user interface|interface|design|visuals?|layout|styling)\b/],
  ["assets", /\b(assets|images|icons|fonts|media|illustrations)\b/],
  ["features", /\b(features?|functionality)\b/],
  ["quality", /\b(quality|bugs?|clean ?up|refactor(ing)?|dead code)\b/],
  ["resilience", /\b(resilience|reliability|reliable|error handling|errors|retries|crash(es)?)\b/],
  ["concurrency", /\b(concurrency|race conditions?|races|threading|async)\b/],
  ["contracts", /\b(contracts?|apis?|schemas?|interfaces? between)\b/],
];
// Spoken names for board improvement categories (categories.js ids).
const VOICE_THEME_WORDS = [
  ["animation", /\b(animations?|transitions?|motion|micro-?interactions)\b/],
  ["scale", /\b(scal(e|ing|ability)|production( readiness)?|high load|load handling)\b/],
  ["ux", /\b(ux|user experience|ui\/ux|usability|polish)\b/],
  ["observability", /\b(observability|logging|logs|monitoring|metrics|telemetry)\b/],
  ["seo", /\b(seo|search engines?|discoverability)\b/],
  ["i18n", /\b(i18n|internationali[sz]ation|locali[sz]ation|translations?)\b/],
  ["privacy", /\b(privacy|compliance|gdpr|personal data)\b/],
  ["mobile", /\b(mobile|responsive|small screens?)\b/],
  ["dx", /\b(developer experience|dx|onboarding)\b/],
  ["code-health", /\b(code health|refactor(ing)?|tech(nical)? debt)\b/],
];
const VOICE_SKIP = /\b(skip|ignore|avoid|stop working on|don'?t (work on|touch)|no more|leave out|less)\b/;
const VOICE_PREFER = /\b(focus( more)? on|prioriti[sz]e|prefer|concentrate on|work on|more (?=ui\b|tests?\b|docs\b|security\b|performance\b|features?\b|accessibility\b|coverage\b))/;
const VOICE_NORMAL = /\b(back to normal|unskip|un-skip|stop (prioriti[sz]ing|preferring)|normal)\b/;

function voiceClean(text) {
  return String(text || "").replace(/\s+/g, " ").trim();
}

function voiceSentence(text) {
  const cleaned = voiceClean(text);
  return cleaned ? cleaned[0].toUpperCase() + cleaned.slice(1) : "";
}

function voiceAreasIn(phrase) {
  const found = [];
  for (const [area, pattern] of VOICE_AREA_WORDS) {
    if (pattern.test(phrase) && !found.includes(area)) found.push(area);
  }
  return found;
}

function voiceFocus(lower, current, currentThemes = []) {
  if (/\b(reset|clear) (the )?focus\b|\bbalanced( focus)?\b|\bfocus on everything\b/.test(lower))
    return { areas: {}, themes: [], changed: true };
  // Split into clauses so "focus on security and skip docs" maps each area to its verb.
  const clauses = lower.split(/[,.;]|\b(?:but|and then|then|also|while)\b|\band (?=(?:skip|ignore|avoid|focus|prioriti|prefer|stop|don'?t))/);
  const areas = { ...current };
  const themes = new Set(currentThemes);
  let changed = false,
    mode = null;
  for (const clause of clauses) {
    if (!clause || !clause.trim()) continue;
    if (VOICE_NORMAL.test(clause)) mode = "normal";
    else if (VOICE_SKIP.test(clause)) mode = "skip";
    else if (VOICE_PREFER.test(clause)) mode = "prefer";
    if (!mode) continue;
    for (const [theme, pattern] of VOICE_THEME_WORDS) {
      if (!pattern.test(clause)) continue;
      if (mode === "prefer") themes.add(theme);
      else themes.delete(theme);
      changed = true;
    }
    for (const area of voiceAreasIn(clause)) {
      if (mode === "normal") delete areas[area];
      else areas[area] = mode;
      changed = true;
    }
  }
  return { areas, themes: [...themes], changed };
}

function voicePriority(lower) {
  if (/\b(urgent|high priority|asap|right away|critical)\b/.test(lower)) return "high";
  if (/\b(low priority|when you have time|eventually|nice to have)\b/.test(lower)) return "low";
  return "normal";
}

// Returns {type, label, ...payload fields}. `current` is the saved area focus map.
function parseVoiceCommand(transcript, current = {}, currentThemes = []) {
  const text = voiceClean(transcript);
  const lower = text.toLowerCase();
  if (!text) return { type: "empty", label: "Nothing heard" };
  if (/^(please )?(pause)( the)?( run| work)?\b/.test(lower))
    return { type: "control", action: "pause", label: "Pause after this cycle" };
  if (/^(please )?(resume|unpause|continue|keep going)( the)?( run| work)?\b/.test(lower))
    return { type: "control", action: "resume", label: "Resume the run" };
  if (/^(please )?(stop|end|cancel)( the)? (run|improvement run|improvements)\b/.test(lower))
    return { type: "control", action: "stop", label: "Stop the run", dangerous: true };
  const task = text.match(/^(?:please )?(?:new task|add (?:a )?task|create (?:a )?task|make (?:a )?task|task)\s*[:,.-]?\s*(?:to\s+)?(.+)$/i);
  if (task) {
    const body = voiceSentence(task[1]);
    const title = body.split(/(?<=[.!?])\s|\s(?:so that|because|and make sure)\s/i)[0].replace(/[.!?]$/, "").slice(0, 200);
    const area = voiceAreasIn(lower)[0] || "general";
    return {
      type: "task",
      title: title || body.slice(0, 200),
      text: body,
      area,
      priority: voicePriority(lower),
      label: "New task",
    };
  }
  if (VOICE_SKIP.test(lower) || VOICE_PREFER.test(lower) || VOICE_NORMAL.test(lower) || /\bfocus\b|\bbalanced\b/.test(lower)) {
    const focus = voiceFocus(lower, current, currentThemes);
    // Only an utterance that is mostly about areas becomes a focus change.
    if (focus.changed && text.split(" ").length <= 24)
      return { type: "focus", areas: focus.areas, themes: focus.themes, text: voiceSentence(text), label: "Focus areas" };
  }
  return { type: "guidance", text: voiceSentence(text), label: "Guidance" };
}

// Recognizes spoken confirmation words in hands-free mode.
function voiceConfirmation(transcript) {
  const lower = voiceClean(transcript).toLowerCase();
  if (/\b(confirm stop|yes,? stop( the run)?)\b/.test(lower)) return "confirm-stop";
  if (/^(send|send it|yes|yeah|yep|confirm|do it|go|go ahead|submit|save)\b/.test(lower)) return "send";
  if (/^(cancel|no|nope|discard|never ?mind|stop|scratch that)\b/.test(lower)) return "cancel";
  if (/^(again|redo|retry|start over|try again)\b/.test(lower)) return "retry";
  return null;
}
