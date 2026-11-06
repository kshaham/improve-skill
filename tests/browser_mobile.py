"""Mobile layout and voice steering checks called by browser_board.py; no model launches."""
from playwright.sync_api import expect

import improve_control as controls

# A scripted stand-in for the Web Speech API: each start() consumes one queued phrase.
FAKE_SPEECH = """
window.__voiceQueue = [];
window.__spoken = [];
class FakeRecognition {
  start() {
    const phrase = window.__voiceQueue.shift();
    setTimeout(() => {
      if (phrase !== undefined) {
        const result = [{ transcript: phrase }];
        result.isFinal = true;
        this.onresult?.({ resultIndex: 0, results: [result] });
      }
      this.onend?.();
    }, 30);
  }
  abort() { this.onend?.(); }
  stop() { this.onend?.(); }
}
window.SpeechRecognition = FakeRecognition;
window.webkitSpeechRecognition = FakeRecognition;
window.speechSynthesis.speak = (utterance) => {
  window.__spoken.push(utterance.text);
  setTimeout(() => utterance.onend?.(), 10);
};
window.speechSynthesis.cancel = () => {};
"""


def speak(page, *phrases):
    page.evaluate("phrases => window.__voiceQueue.push(...phrases)", list(phrases))


def check_mobile(page, repo, url, tasks, write_tasks, artifacts):
    tasks[:] = [dict(id="m-1", title="Queue item", area="ui", status="ready"),
                dict(id="m-2", title="Working item", area="security", status="in_progress"),
                dict(id="m-3", title="Blocked item", area="docs", status="blocked")]
    write_tasks()
    # Desktop stays desktop, and the footer toggle forces either layout.
    page.set_viewport_size({"width": 1600, "height": 1050})
    page.goto(url + "/?layout=auto")
    expect(page.locator("html")).not_to_have_class("mobile")
    expect(page.locator("#mobile-nav")).to_be_hidden()
    page.locator("#layout-toggle").click()
    expect(page.locator("html")).to_have_class("mobile")
    expect(page.locator("#mobile-nav")).to_be_visible()
    page.locator("#layout-toggle").click()
    expect(page.locator("html")).not_to_have_class("mobile")

    browser = page.context.browser
    context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True,
                                  device_scale_factor=2, locale="en-US")
    context.add_init_script(FAKE_SPEECH)
    phone = context.new_page()
    errors = []
    phone.on("pageerror", lambda error: errors.append(str(error)))
    phone.goto(url)
    expect(phone.locator("#connection")).to_have_text("Board connected")
    expect(phone.locator("html")).to_have_class("mobile")
    expect(phone.locator("#mobile-nav")).to_be_visible()
    expect(phone.locator("#focus-panel")).to_have_class("focus-panel collapsed")
    phone.locator("#run").select_option("current")
    expect(phone.locator("#board .column[data-active]")).to_have_count(1)
    expect(phone.locator("#board .column[data-active]")).to_have_attribute("data-status", "ready")
    phone.locator('#mobile-columns [data-status="in_progress"]').click()
    expect(phone.locator("#board .column[data-active] .task-card")).to_contain_text("Working item")
    phone.evaluate("""() => {
      const board = document.getElementById('board');
      const touch = (x) => new Touch({identifier: 1, target: board, clientX: x, clientY: 300});
      board.dispatchEvent(new TouchEvent('touchstart', {touches: [touch(320)], changedTouches: [touch(320)]}));
      board.dispatchEvent(new TouchEvent('touchend', {touches: [], changedTouches: [touch(120)]}));
    }""")
    expect(phone.locator("#board .column[data-active]")).to_have_attribute("data-status", "blocked")
    assert phone.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Mobile board overflows"
    phone.screenshot(path=str(artifacts / "mobile-board.png"), full_page=False)

    # Hands-free focus change: speak, hear it read back, confirm by voice.
    before = len(controls.request_view(repo))
    speak(phone, "focus on security and skip docs", "send")
    phone.locator("#nav-steer").click()
    expect(phone.locator("#voice-dialog")).to_be_visible()
    expect(phone.locator("#voice-live")).to_contain_text("Sent.")
    focus = controls.request_view(repo)[-1]
    assert focus["type"] == "focus" and focus["areas"].get("security") == "prefer" and focus["areas"].get("docs") == "skip", focus
    assert any("Say send" in text for text in phone.evaluate("window.__spoken")), "Command was not read back"

    # Cancel by voice sends nothing.
    speak(phone, "new task add dark mode to settings", "cancel")
    phone.locator("#voice-mic").click()
    expect(phone.locator("#voice-live")).to_contain_text("Cancelled")
    assert len(controls.request_view(repo)) == before + 1

    # A task by voice, confirmed by voice.
    speak(phone, "new task add offline mode so that the board works on a train, high priority", "yes")
    phone.locator("#voice-mic").click()
    expect(phone.locator("#voice-live")).to_contain_text("Sent.")
    task = controls.request_view(repo)[-1]
    assert task["type"] == "task" and task["title"] == "Add offline mode" and task["priority"] == "high", task

    # Stopping needs the explicit phrase and cannot happen from a plain "send".
    speak(phone, "stop the run", "send")
    phone.locator("#voice-mic").click()
    expect(phone.locator("#voice-send")).to_have_text("Confirm stop")
    assert not (repo / ".improve" / "stop.request").exists()
    phone.evaluate("window.__voiceQueue.length = 0")
    sent = phone.evaluate("""() => {
      const original = sendVoice, calls = [];
      sendVoice = () => calls.push('sent');
      voiceIntent = {type: 'control', action: 'stop', label: 'Stop the run'};
      handleConfirmation('send');
      const afterSend = calls.length;
      handleConfirmation('confirm-stop');
      sendVoice = original;
      return [afterSend, calls.length];
    }""")
    assert sent == [0, 1], f"Stop must need 'confirm stop', got {sent}"

    # Manual mode: edit the transcript and send with a tap.
    phone.locator("#voice-handsfree").uncheck()
    phone.locator("#voice-clear").click()
    phone.locator("#voice-transcript").fill("The settings screen should remember the last tab")
    expect(phone.locator("#voice-type")).to_have_value("guidance")
    phone.screenshot(path=str(artifacts / "mobile-voice.png"), full_page=False)
    phone.locator("#voice-send").click()
    expect(phone.locator("#voice-live")).to_contain_text("Sent.")
    assert controls.request_view(repo)[-1]["text"] == "The settings screen should remember the last tab"
    phone.locator('#voice-dialog [data-close="voice-dialog"]').last.click()

    # Dictation into an ordinary form field.
    phone.locator("#new-task").click()
    speak(phone, "keep focus visible on every screen")
    phone.locator("#request-text + .mic-button").click()
    expect(phone.locator("#request-text")).to_have_value("keep focus visible on every screen")
    assert phone.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Mobile form overflows"
    phone.locator('#request-dialog [data-close="request-dialog"]').last.click()
    phone.locator('#mobile-nav [data-view="requests"]').click()
    expect(phone.locator("#requests-view")).to_be_visible()
    assert not errors, errors
    context.close()

    # Without speech support the mic explains why instead of failing silently.
    context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    context.add_init_script("delete window.SpeechRecognition; delete window.webkitSpeechRecognition;")
    plain = context.new_page()
    plain.goto(url)
    expect(plain.locator(".mic-button").first).to_be_hidden()
    plain.locator("#nav-steer").click()
    expect(plain.locator("#voice-support")).to_contain_text("speech recognition")
    context.close()
