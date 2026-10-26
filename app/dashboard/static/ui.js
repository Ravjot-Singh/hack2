// Page behaviour shared by the portal (/app) and the admin console (/admin). No inline scripts or handlers in the
// templates: everything is declared with data attributes and wired up here.
//
//   <form data-confirm="Send this deal to 120 customers now?" data-confirm-title="Launch deal"
//         data-confirm-ok="Launch" data-confirm-danger>          → in-page confirmation dialog before submitting
//   (the same attributes on a submit button apply to that button only)
//   <select data-autosubmit>                                     → submits its form when changed
//   <select data-type-switch> + <x data-show-for="a b">          → shows fields only for some values
//   <span data-labels='{"a": "Label A"}'>                        → label text follows the switch
//   <div data-live-refresh="3">                                  → refreshes the page content every 3 s in place
(function () {
  "use strict";

  // ---- confirmation dialog (instead of the browser's confirm()) ----
  var dialog = null, pending = null;  // pending: {form, submitter} waiting for an answer
  function buildDialog() {
    dialog = document.createElement("dialog");
    dialog.className = "confirm-dialog";
    dialog.setAttribute("aria-labelledby", "confirm-title");
    dialog.setAttribute("aria-describedby", "confirm-text");
    dialog.innerHTML =
      '<div class="confirm-body">' +
      '<h2 id="confirm-title"></h2><p id="confirm-text"></p>' +
      '<div class="confirm-actions">' +
      '<button type="button" class="secondary confirm-cancel">Cancel</button>' +
      '<button type="button" class="confirm-ok"></button>' +
      "</div></div>";
    document.body.appendChild(dialog);
    dialog.querySelector(".confirm-cancel").addEventListener("click", function () { answer(false); });
    dialog.querySelector(".confirm-ok").addEventListener("click", function () { answer(true); });
    dialog.addEventListener("cancel", function (e) { e.preventDefault(); answer(false); });  // Escape
    dialog.addEventListener("click", function (e) { if (e.target === dialog) answer(false); });  // backdrop
  }

  function answer(yes) {
    var p = pending;
    pending = null;
    if (dialog.open) dialog.close();
    if (!yes || !p) return;
    p.form.dataset.confirmed = "1";
    if (p.form.requestSubmit) p.form.requestSubmit(p.submitter || undefined); else p.form.submit();
  }

  function askFirst(form, submitter, source) {
    if (!dialog) buildDialog();
    var d = source.dataset;
    dialog.querySelector("#confirm-title").textContent = d.confirmTitle || "Are you sure?";
    dialog.querySelector("#confirm-text").textContent = d.confirm;
    var ok = dialog.querySelector(".confirm-ok");
    ok.textContent = d.confirmOk || "Confirm";
    ok.classList.toggle("danger", "confirmDanger" in d);
    pending = { form: form, submitter: submitter };
    dialog.showModal();
    // Destructive actions start on Cancel; others on the action itself.
    ("confirmDanger" in d ? dialog.querySelector(".confirm-cancel") : ok).focus();
  }

  // Capture phase: runs before the double-submit guard below.
  // A button's own data-confirm wins over the form's (e.g. "Send" asks, "Check" doesn't).
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!form.dataset) return;
    var source = e.submitter && e.submitter.dataset.confirm ? e.submitter : form;
    if (!source.dataset.confirm) return;
    if (form.dataset.confirmed === "1") { delete form.dataset.confirmed; return; }
    e.preventDefault();
    e.stopImmediatePropagation();
    askFirst(form, e.submitter, source);
  }, true);

  // ---- one click = one submit (no double offers or double deal launches) ----
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (e.defaultPrevented || (form.method || "").toLowerCase() !== "post") return;
    var buttons = form.querySelectorAll("button[type=submit], button:not([type])");
    setTimeout(function () {  // after the browser has taken the form data (incl. the clicked button)
      buttons.forEach(function (b) { b.disabled = true; b.dataset.busy = "1"; });
    }, 0);
  });
  window.addEventListener("pageshow", function () {  // back button: buttons usable again
    document.querySelectorAll("button[data-busy]").forEach(function (b) { b.disabled = false; delete b.dataset.busy; });
  });

  // ---- selects that submit their form ----
  document.addEventListener("change", function (e) {
    if (e.target.matches && e.target.matches("select[data-autosubmit]")) e.target.form.submit();
  });

  // ---- fields that depend on a type select ----
  function applySwitch(select) {
    var scope = select.form || document;
    var value = select.value;
    scope.querySelectorAll("[data-show-for]").forEach(function (el) {
      el.hidden = el.dataset.showFor.split(/\s+/).indexOf(value) < 0;
    });
    scope.querySelectorAll("[data-labels]").forEach(function (el) {
      try { var labels = JSON.parse(el.dataset.labels); if (value in labels) el.textContent = labels[value]; } catch (err) { /* ignore */ }
    });
  }
  document.querySelectorAll("select[data-type-switch]").forEach(function (s) {
    applySwitch(s);
    s.addEventListener("change", function () { applySwitch(s); });
  });

  // ---- live progress without a full reload (keeps scroll position) ----
  var live = document.querySelector("[data-live-refresh]");
  if (live) {
    var every = Math.max(2, parseInt(live.dataset.liveRefresh, 10) || 3) * 1000;
    var timer = setInterval(function () {
      if (document.hidden) return;
      fetch(location.href, { headers: { Accept: "text/html" }, credentials: "same-origin" })
        .then(function (r) {
          if (!r.ok || r.redirected) throw new Error("stop");
          return r.text();
        })
        .then(function (html) {
          var doc = new DOMParser().parseFromString(html, "text/html");
          var fresh = doc.querySelector("main"), current = document.querySelector("main");
          if (!fresh || !current) throw new Error("stop");
          current.innerHTML = fresh.innerHTML;
          if (window.stackTables) window.stackTables(current);
          if (!current.querySelector("[data-live-refresh]")) clearInterval(timer);  // finished
        })
        .catch(function () { clearInterval(timer); });
    }, every);
  }
})();
