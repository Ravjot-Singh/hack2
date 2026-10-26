// Small-screen helpers shared by the portal (/app) and the admin console (/admin).
(function () {
  // 1. Phone menu: the sidebar becomes a top bar with a "Menu" button that opens the links.
  var btn = document.querySelector(".menu-btn");
  var side = document.querySelector(".side");
  if (btn && side) {
    btn.addEventListener("click", function () {
      var open = side.classList.toggle("open");
      btn.setAttribute("aria-expanded", open ? "true" : "false");
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && side.classList.contains("open")) { side.classList.remove("open"); btn.setAttribute("aria-expanded", "false"); btn.focus(); }
    });
  }

  // 2. Data tables stack into one card per row on phones: every cell gets its column heading as a label.
  // Only tables whose first row is all headings and has 3+ columns; two-column lists stay as they are.
  document.querySelectorAll("table:not(.no-stack)").forEach(function (table) {
    var head = table.rows[0];
    if (!head || head.cells.length < 3) return;
    for (var i = 0; i < head.cells.length; i++) if (head.cells[i].tagName !== "TH") return;
    var labels = Array.prototype.map.call(head.cells, function (th) { return th.textContent.trim(); });
    table.classList.add("stack");
    head.classList.add("stack-head");
    for (var r = 1; r < table.rows.length; r++) {
      var col = 0;
      Array.prototype.forEach.call(table.rows[r].cells, function (cell) {
        if (!cell.hasAttribute("data-label")) cell.setAttribute("data-label", labels[col] || "");
        // Form fields or several pieces of content: label above, content full width (instead of side by side).
        if (cell.querySelector("input, select, textarea, table, form") || cell.children.length > 1) {
          cell.classList.add("cell-block");
        }
        col += cell.colSpan || 1;
      });
    }
  });
})();
