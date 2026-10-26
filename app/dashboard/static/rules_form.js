// Offer rules editor: rupee fields follow the discount %.
// For each offer the allowed range of its ₹ fields is recomputed whenever a value, a category cap or the per-offer
// limit changes; a value outside its range snaps into it when you leave the field, and a hint says why.
// The server checks the same relations (app/rules/schema.py check_rules), this only makes them visible.
(function () {
  var form = document.querySelector("form.rules-form");
  if (!form) return;
  var pointValue = parseFloat(form.dataset.pointValue || "1") || 1;
  var DYNAMIC = { from_cart: 1, top_category: 1 };

  function field(name) { return form.elements.namedItem(name); }
  function num(name) {
    var el = field(name);
    if (!el || el.value === "") return null;
    var v = parseFloat(el.value);
    return isNaN(v) ? null : v;
  }
  function rs(v) { return "₹" + Math.round(v).toLocaleString("en-IN"); }
  function caps() {
    var out = {};
    Array.prototype.forEach.call(form.querySelectorAll("input[name^='cap.']"), function (el) {
      var v = parseFloat(el.value);
      if (!isNaN(v)) out[el.name.slice(4)] = v;
    });
    return out;
  }
  function capFor(category, c, highest) {
    if (category in c) return c[category];
    if (DYNAMIC[category]) {  // depends on the customer: % is capped per category later; ₹ must fit the store-wide cap
      if (highest) return Math.max.apply(null, Object.keys(c).map(function (k) { return c[k]; }));
      return c.any;
    }
    return c.any;
  }
  function setRange(el, min, max) {
    if (!el) return;
    if (min === null || min === undefined) el.removeAttribute("min"); else el.min = String(min);
    if (max === null || max === undefined || !isFinite(max)) el.removeAttribute("max"); else el.max = String(max);
  }
  // Snap a value into [min, max]; returns the note to show, or "" if nothing changed.
  function clamp(el, label, snap) {
    if (!el || el.value === "" || !snap) return "";
    var v = parseFloat(el.value);
    var min = el.min !== "" ? parseFloat(el.min) : -Infinity;
    var max = el.max !== "" ? parseFloat(el.max) : Infinity;
    var nv = Math.min(Math.max(v, min), max);
    if (nv === v) return "";
    el.value = String(nv);
    el.classList.add("adjusted");
    setTimeout(function () { el.classList.remove("adjusted"); }, 1600);
    return label + " adjusted to " + (label.indexOf("%") >= 0 ? nv + "%" : rs(nv)) + ".";
  }

  function worstCase(row, perOffer) {
    var k = row.dataset.key, type = row.dataset.type;
    if (!field("offer." + k + ".enabled") || !field("offer." + k + ".enabled").checked) return 0;
    if (type === "percent_off") return Math.min(num("offer." + k + ".max_discount_rs") || perOffer, perOffer);
    if (type === "flat_off") return num("offer." + k + ".value") || 0;
    if (type === "bonus_points") return (num("offer." + k + ".value") || 0) * pointValue;
    return num("limits.delivery_fee_rs") || 0;
  }

  function update(snap) {
    var c = caps();
    var perOffer = num("limits.max_discount_rs_per_offer") || Infinity;
    var largest = 0;
    Array.prototype.forEach.call(form.querySelectorAll("tr.offer-row"), function (row) {
      var k = row.dataset.key, type = row.dataset.type, cat = row.dataset.category;
      var value = field("offer." + k + ".value"), minOrder = field("offer." + k + ".min_order_value"),
          maxRs = field("offer." + k + ".max_discount_rs"), hint = row.querySelector(".limit-hint");
      var notes = [];
      if (type === "percent_off" && value) {
        var cap = capFor(cat, c, true);
        setRange(value, 0.5, cap);
        notes.push(clamp(value, "Amount %", snap));
        var pct = parseFloat(value.value) || 0;
        // Minimum order: the % must still apply in full below the per-offer limit.
        setRange(minOrder, 0, pct > 0 && isFinite(perOffer) ? Math.floor(perOffer * 100 / pct) : null);
        notes.push(clamp(minOrder, "Minimum order", snap));
        var mo = parseFloat(minOrder.value) || 0;
        var floor = Math.ceil(pct * mo / 100);
        setRange(maxRs, Math.max(1, floor), isFinite(perOffer) ? perOffer : null);
        notes.push(clamp(maxRs, "Maximum discount", snap));
        hint.textContent = pct + "% of a " + rs(mo) + " minimum order is " + rs(pct * mo / 100) +
          ", so the maximum discount can be " + rs(Math.max(1, floor)) + "–" + (isFinite(perOffer) ? rs(perOffer) : "any") +
          ". Up to " + cap + "% allowed" + (DYNAMIC[cat] ? " (capped per category for each customer)." : ".");
      } else if (type === "flat_off" && value) {
        var fcap = capFor(cat, c, false);
        setRange(value, 1, isFinite(perOffer) ? perOffer : null);
        notes.push(clamp(value, "Amount ₹", snap));
        var amount = parseFloat(value.value) || 0;
        var need = fcap > 0 ? Math.ceil(amount * 100 / fcap) : null;
        setRange(minOrder, need, null);
        notes.push(clamp(minOrder, "Minimum order", snap));
        hint.textContent = rs(amount) + " off stays within the " + fcap + "% limit from a " + rs(need || 0) +
          " order up" + (DYNAMIC[cat] ? " (store-wide limit; stricter categories are skipped)." : ".");
      } else if (type === "bonus_points" && value) {
        var maxPts = isFinite(perOffer) ? Math.floor(perOffer / pointValue) : null;
        setRange(value, 1, maxPts);
        notes.push(clamp(value, "Points", snap));
        hint.textContent = maxPts ? "Up to " + maxPts.toLocaleString("en-IN") + " points (worth " + rs(perOffer) +
          ", the per-offer limit)." : "";
      } else if (hint) {
        hint.textContent = "";
      }
      notes = notes.filter(Boolean);
      if (notes.length) hint.textContent += " " + notes.join(" ");
      largest = Math.max(largest, worstCase(row, perOffer));
    });

    // Budgets: the daily budget must cover the most expensive enabled offer; the total ceiling covers the budget.
    var budget = field("limits.daily_budget_rs"), ceiling = field("limits.max_total_spend_per_day_rs");
    setRange(budget, Math.ceil(largest), null);
    var bnote = clamp(budget, "Daily budget", snap);
    setRange(ceiling, num("limits.daily_budget_rs"), null);
    var cnote = clamp(ceiling, "Maximum total spend", snap);
    budgetHint(budget, "At least " + rs(largest) + ": your most expensive enabled offer. " + bnote);
    budgetHint(ceiling, "Empty = twice the daily budget; otherwise at least the daily budget. " + cnote);
  }

  function budgetHint(el, text) {
    if (!el) return;
    var cell = el.closest("td");
    var h = cell.querySelector(".limit-hint");
    if (!h) {
      h = document.createElement("div");
      h.className = "desc limit-hint";
      h.setAttribute("aria-live", "polite");
      cell.appendChild(h);
    }
    h.textContent = text.trim();
  }

  form.addEventListener("input", function () { update(false); });   // ranges and hints while typing
  form.addEventListener("change", function () { update(true); });   // snap into range on leaving a field
  update(false);
})();
