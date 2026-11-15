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
// Spoken names for board improvement categories (the IMPROVEMENT_CATEGORIES ids in focus.js).
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

// Voice steering: dictate into any field, or speak a whole command hands-free.
const SpeechRecognitionApi =
  window.SpeechRecognition || window.webkitSpeechRecognition || null;
const VOICE_FIELDS = ["request-text", "request-task-title", "focus-note"];
const VOICE_HANDSFREE_KEY = "improve-board-voice-handsfree";
const VOICE_TYPES = {
  guidance: "Guidance",
  task: "New task",
  focus: "Focus areas",
  pause: "Pause run",
  resume: "Resume run",
  stop: "Stop run",
};
let activeRecognition = null,
  voiceIntent = null,
  voiceTypeLocked = false,
  voiceRetry = null,
  voiceSending = false,
  confirmPrompts = 0;

function voiceAvailable() {
  return !!SpeechRecognitionApi && window.isSecureContext;
}

function voiceUnavailableReason() {
  if (!window.isSecureContext)
    return "Voice input needs a secure page. Open the board at its localhost URL or through an HTTPS tunnel (see docs/board.md).";
  return "This browser has no speech recognition. Use Chrome, Edge, or Safari, or your keyboard's dictation key.";
}

function stopListening() {
  const recognition = activeRecognition;
  activeRecognition = null;
  if (recognition) {
    try {
      recognition.abort();
    } catch (error) {
      // An already-ended session cannot be aborted twice; report anything else.
      if (error?.name !== "InvalidStateError") voiceError(error.message);
    }
  }
  for (const button of document.querySelectorAll(".mic-button[aria-pressed='true']"))
    button.setAttribute("aria-pressed", "false");
  $("voice-mic")?.classList.remove("listening");
}

function listen({ onInterim, onFinal, onEnd, onError }) {
  stopListening();
  const recognition = new SpeechRecognitionApi();
  recognition.lang = navigator.language || "en-US";
  recognition.interimResults = true;
  recognition.continuous = false;
  recognition.maxAlternatives = 1;
  let finalText = "";
  recognition.onresult = (event) => {
    let interim = "";
    for (let index = event.resultIndex; index < event.results.length; index++) {
      const result = event.results[index];
      if (result.isFinal) finalText += result[0].transcript;
      else interim += result[0].transcript;
    }
    onInterim?.(voiceClean(finalText + " " + interim));
  };
  recognition.onerror = (event) => {
    if (event.error === "aborted" || event.error === "no-speech") return;
    onError?.(
      event.error === "not-allowed" || event.error === "service-not-allowed"
        ? "Microphone access was blocked. Allow the microphone for this page in your browser settings."
        : "Voice input stopped: " + event.error,
    );
  };
  recognition.onend = () => {
    if (activeRecognition !== recognition) return;
    activeRecognition = null;
    $("voice-mic")?.classList.remove("listening");
    if (voiceClean(finalText)) onFinal?.(voiceClean(finalText));
    onEnd?.();
  };
  activeRecognition = recognition;
  recognition.start();
  return recognition;
}

function speak(text, then) {
  if (!("speechSynthesis" in window) || !$("voice-handsfree").checked) {
    then?.();
    return;
  }
  speechSynthesis.cancel();
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.lang = navigator.language || "en-US";
  utterance.onend = () => then?.();
  utterance.onerror = () => then?.();
  speechSynthesis.speak(utterance);
}

function attachDictation(id) {
  const field = $(id);
  const button = element("button", "mic-button", "");
  button.type = "button";
  button.setAttribute("aria-pressed", "false");
  button.setAttribute("aria-label", "Dictate into " + (field.getAttribute("aria-label") || field.closest("label")?.firstChild?.textContent?.trim() || "this field"));
  button.title = "Dictate";
  button.hidden = !voiceAvailable();
  button.addEventListener("click", () => {
    if (button.getAttribute("aria-pressed") === "true") return stopListening();
    const start = field.value;
    button.setAttribute("aria-pressed", "true");
    const write = (spoken) => {
      const joined = (start ? start.replace(/\s*$/, " ") : "") + spoken;
      field.value = joined.slice(0, field.maxLength > 0 ? field.maxLength : joined.length);
      field.dispatchEvent(new Event("input", { bubbles: true }));
    };
    listen({
      onInterim: write,
      onFinal: write,
      onEnd: () => button.setAttribute("aria-pressed", "false"),
      onError: (message) => {
        button.setAttribute("aria-pressed", "false");
        feedback(message, true);
      },
    });
  });
  const wrap = element("span", "dictation-wrap");
  field.replaceWith(wrap);
  wrap.append(field, button);
}

function describeIntent(intent) {
  if (!intent || intent.type === "empty") return "Speak or type a command.";
  if (intent.type === "focus") return focusSummary(intent.areas, intent.themes, savedFocus().agents);
  if (intent.type === "task")
    return `${intent.title} · ${intent.area} · ${intent.priority} priority`;
  if (intent.type === "control")
    return intent.action === "stop"
      ? "Ends the run after current work settles. Say “confirm stop”, or press Confirm stop."
      : intent.label;
  return intent.text;
}

function intentKind(intent) {
  return intent?.type === "control" ? intent.action : intent?.type;
}

// A newly spoken category also prefers its areas, exactly like tapping its tile.
function withCategoryAreas(intent, previous = []) {
  if (intent.type !== "focus") return intent;
  const areas = { ...intent.areas };
  for (const [id, , , , categoryAreas] of IMPROVEMENT_CATEGORIES)
    if (intent.themes?.includes(id) && !previous.includes(id))
      for (const area of categoryAreas) areas[area] = "prefer";
  return { ...intent, areas };
}

function interpretTranscript() {
  const text = $("voice-transcript").value;
  const saved = savedFocus();
  let intent = withCategoryAreas(parseVoiceCommand(text, saved.areas, saved.themes), saved.themes);
  if (voiceTypeLocked) {
    const kind = $("voice-type").value;
    if (["pause", "resume", "stop"].includes(kind))
      intent = { type: "control", action: kind, label: VOICE_TYPES[kind] };
    else if (kind === "task" && intent.type !== "task")
      intent = parseVoiceCommand("new task " + text);
    else if (kind === "guidance")
      intent = { type: "guidance", text: voiceSentence(text), label: "Guidance" };
    else if (kind === "focus" && intent.type !== "focus")
      intent = { type: "focus", areas: { ...saved.areas }, themes: [...saved.themes], text, label: "Focus areas" };
  }
  voiceIntent = intent;
  renderVoice();
}

function renderVoice() {
  const intent = voiceIntent;
  const kind = intentKind(intent);
  if (kind && kind !== "empty" && !voiceTypeLocked) $("voice-type").value = kind;
  $("voice-summary").textContent = describeIntent(intent);
  const controls = snapshot?.controls || {};
  const isControl = intent?.type === "control";
  const allowed = isControl
    ? controls.available && !controls.stop_requested
    : controls.requests_available;
  $("voice-send").disabled =
    voiceSending || !connected || !allowed || !intent || intent.type === "empty" ||
    (intent.type !== "control" && intent.type !== "focus" && !voiceClean(intent.text));
  $("voice-send").textContent = kind === "stop" ? "Confirm stop" : "Send";
  $("voice-send").classList.toggle("danger", kind === "stop");
  $("voice-blocked").hidden = !intent || intent.type === "empty" || allowed;
  $("voice-blocked").textContent = isControl
    ? controls.reason || "Run controls are unavailable."
    : controls.reason || "Requests are unavailable.";
}

function voiceError(message) {
  $("voice-error").textContent = message;
  $("voice-error").hidden = !message;
}

function captureCommand() {
  if (!voiceAvailable()) return voiceError(voiceUnavailableReason());
  voiceError("");
  voiceTypeLocked = false;
  confirmPrompts = 0;
  $("voice-mic").classList.add("listening");
  $("voice-live").textContent = "Listening…";
  listen({
    onInterim: (text) => {
      $("voice-live").textContent = text || "Listening…";
      $("voice-transcript").value = text;
    },
    onFinal: (text) => {
      $("voice-transcript").value = text;
      $("voice-live").textContent = "";
      interpretTranscript();
      if ($("voice-handsfree").checked) askConfirmation();
    },
    onEnd: () => {
      if (!$("voice-transcript").value) $("voice-live").textContent = "Didn’t catch that. Tap the microphone to try again.";
    },
    onError: voiceError,
  });
}

function askConfirmation() {
  if (!$("voice-dialog").open) return;
  if ($("voice-send").disabled) {
    speak($("voice-blocked").hidden
      ? "I couldn't turn that into a request. Tap the microphone to try again."
      : $("voice-blocked").textContent);
    return;
  }
  const kind = intentKind(voiceIntent);
  const prompt = kind === "stop"
    ? "Stop the run? Say confirm stop, or cancel."
    : `${VOICE_TYPES[kind]}: ${describeIntent(voiceIntent)}. Say send, cancel, or again.`;
  $("voice-live").textContent = kind === "stop" ? "Say “confirm stop” or “cancel”" : "Say “send”, “cancel”, or “again”";
  speak(prompt, () => {
    if (!$("voice-dialog").open) return;
    $("voice-mic").classList.add("listening");
    listen({
      onFinal: (answer) => handleConfirmation(voiceConfirmation(answer)),
      onEnd: () => {
        if ($("voice-mic").classList.contains("listening")) $("voice-mic").classList.remove("listening");
      },
      onError: voiceError,
    });
  });
}

function handleConfirmation(answer) {
  const kind = intentKind(voiceIntent);
  if (answer === "cancel") return resetVoice("Cancelled. Nothing was sent.");
  if (answer === "retry") return captureCommand();
  if ((answer === "send" && kind !== "stop") || (answer === "confirm-stop" && kind === "stop"))
    return sendVoice();
  if (++confirmPrompts < 2) return askConfirmation();
  $("voice-live").textContent = "Waiting. Press Send or tap the microphone.";
}

function resetVoice(message = "") {
  stopListening();
  voiceIntent = null;
  voiceTypeLocked = false;
  $("voice-transcript").value = "";
  $("voice-live").textContent = message;
  renderVoice();
  if (message) speak(message);
}

function voicePayload(intent) {
  const run_id = snapshot.current.control_id || null;
  if (intent.type === "control") return ["/api/control", { action: intent.action, run_id }];
  const payload = { type: intent.type, run_id, text: (intent.text || "").slice(0, 8000) };
  if (intent.type === "task")
    Object.assign(payload, { title: intent.title, area: intent.area, priority: intent.priority });
  if (intent.type === "focus") {
    Object.assign(payload, { areas: intent.areas, themes: intent.themes || [], text: (intent.text || "").slice(0, 2000) });
    // A focus request replaces the last one, so a spoken change keeps the parallel-agents choice.
    if (savedFocus().agents) payload.agents = savedFocus().agents;
  }
  return ["/api/requests", payload];
}

async function sendVoice() {
  if (voiceSending || !voiceIntent || $("voice-send").disabled) return;
  stopListening();
  const [path, payload] = voicePayload(voiceIntent);
  if (path === "/api/requests") {
    const signature = JSON.stringify(payload);
    if (voiceRetry?.signature !== signature) voiceRetry = { signature, id: crypto.randomUUID() };
    payload.id = voiceRetry.id;
  }
  voiceSending = true;
  renderVoice();
  voiceError("");
  try {
    const result = await boardAction(path, payload);
    voiceRetry = null;
    navigator.vibrate?.(15);
    const message = typeof result.message === "string" ? result.message : "Sent.";
    feedback(message);
    resetVoice("Sent. " + message);
    await refresh();
  } catch (error) {
    voiceError(error.message);
    speak("That didn't send. " + error.message);
  } finally {
    voiceSending = false;
    renderVoice();
  }
}

function openVoice(autoStart = true) {
  if (!$("voice-dialog").open) $("voice-dialog").showModal();
  $("voice-support").hidden = voiceAvailable();
  $("voice-support").textContent = voiceAvailable() ? "" : voiceUnavailableReason();
  $("voice-mic").disabled = !voiceAvailable();
  renderVoice();
  if (autoStart && voiceAvailable() && !voiceClean($("voice-transcript").value)) captureCommand();
}

function initVoice() {
  for (const id of VOICE_FIELDS) attachDictation(id);
  for (const [value, label] of Object.entries(VOICE_TYPES)) {
    const option = element("option", "", label);
    option.value = value;
    $("voice-type").append(option);
  }
  try {
    $("voice-handsfree").checked = localStorage.getItem(VOICE_HANDSFREE_KEY) !== "0";
  } catch (error) {
    $("voice-handsfree").checked = true;
  }
  $("voice-handsfree").addEventListener("change", () => {
    try {
      localStorage.setItem(VOICE_HANDSFREE_KEY, $("voice-handsfree").checked ? "1" : "0");
    } catch (error) {
      voiceError("Browser storage is blocked; this setting resets after a reload.");
    }
  });
  $("voice-mic").addEventListener("click", () =>
    activeRecognition ? stopListening() : captureCommand(),
  );
  $("voice-transcript").addEventListener("input", interpretTranscript);
  $("voice-type").addEventListener("change", () => {
    voiceTypeLocked = true;
    interpretTranscript();
  });
  $("voice-send").addEventListener("click", sendVoice);
  $("voice-clear").addEventListener("click", () => resetVoice());
  $("voice-dialog").addEventListener("close", () => {
    stopListening();
    if ("speechSynthesis" in window) speechSynthesis.cancel();
  });
  $("steer-voice").addEventListener("click", () => openVoice());
  document.addEventListener("keydown", (event) => {
    // Shift+V opens voice steering when no field has focus.
    if (event.key === "V" && event.shiftKey && !event.target.closest("input, textarea, select, dialog"))
      openVoice();
  });
}
