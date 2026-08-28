/* satprep progressive enhancement.
 *
 * One file, no build step, no CDN — the box serves this on a LAN and must not
 * depend on the internet being up. Every behaviour here is an enhancement:
 * the markup it attaches to already works with JavaScript disabled, because
 * each confidence control is a real submit button carrying its own value.
 *
 * What this adds:
 *   - the drill timer, and the elapsed_ms the server records
 *   - keyboard shortcuts: A-D to choose, 1-3 for confidence, Enter to advance
 */

(function () {
  "use strict";

  var form = document.getElementById("answer-form");
  if (form) enhanceQuestion(form);

  var advance = document.getElementById("advance");
  if (advance) enhanceFeedback(advance);

  function enhanceQuestion(form) {
    var started = Date.now();
    var elapsed = document.getElementById("elapsed_ms");
    var timer = document.getElementById("timer");

    if (timer) {
      var tick = function () {
        var s = Math.floor((Date.now() - started) / 1000);
        timer.textContent = Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
      };
      tick();
      setInterval(tick, 1000);
    }

    // The hidden field is stamped on submit rather than on a button's onclick:
    // the form can also be submitted by Enter or by the keyboard shortcuts
    // below, and those paths recorded 0ms before.
    form.addEventListener("submit", function () {
      if (elapsed) elapsed.value = String(Date.now() - started);
    });

    var choices = Array.prototype.slice.call(
      form.querySelectorAll('input[name="letter"]')
    );
    var confidence = Array.prototype.slice.call(form.querySelectorAll(".conf-btn"));

    document.addEventListener("keydown", function (event) {
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      // never steal a keystroke aimed at a field the student is typing in
      var tag = (event.target.tagName || "").toLowerCase();
      if (tag === "input" && event.target.type !== "radio") return;
      if (tag === "textarea" || tag === "select") return;

      var key = event.key.toUpperCase();

      var choice = choices.filter(function (input) {
        return input.value.toUpperCase() === key;
      })[0];
      if (choice) {
        event.preventDefault();
        choice.checked = true;
        // :has(input:checked) is CSS-only, but assistive tech and any listener
        // need the event a real click would have produced.
        choice.dispatchEvent(new Event("change", { bubbles: true }));
        return;
      }

      if (key >= "1" && key <= "3") {
        var button = confidence.filter(function (b) {
          return b.value === key;
        })[0];
        // Submitting without a choice would fail `required` validation and
        // leave the student staring at a native bubble; nudge instead.
        if (button && choices.some(function (c) { return c.checked; })) {
          event.preventDefault();
          button.click();
        }
      }
    });
  }

  function enhanceFeedback(link) {
    document.addEventListener("keydown", function (event) {
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      if (event.key === "Enter" || event.key === " ") {
        // Enter on a focused control already does the right thing; only take
        // over when nothing in particular is focused.
        if (document.activeElement && document.activeElement !== document.body) return;
        event.preventDefault();
        link.click();
      }
    });
  }
})();
