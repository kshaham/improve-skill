"use strict";
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
  if (intent.type === "focus") return focusSummary(intent.areas);
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

function interpretTranscript() {
  const text = $("voice-transcript").value;
  let intent = parseVoiceCommand(text, savedFocus().areas);
  if (voiceTypeLocked) {
    const kind = $("voice-type").value;
    if (["pause", "resume", "stop"].includes(kind))
      intent = { type: "control", action: kind, label: VOICE_TYPES[kind] };
    else if (kind === "task" && intent.type !== "task")
      intent = parseVoiceCommand("new task " + text);
    else if (kind === "guidance")
      intent = { type: "guidance", text: voiceSentence(text), label: "Guidance" };
    else if (kind === "focus" && intent.type !== "focus")
      intent = { type: "focus", areas: { ...savedFocus().areas }, text, label: "Focus areas" };
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
  if (intent.type === "focus") Object.assign(payload, { areas: intent.areas, text: (intent.text || "").slice(0, 2000) });
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
