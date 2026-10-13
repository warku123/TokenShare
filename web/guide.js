/* TokenShare — guide.html role tabs.
   BUYER / SELLER single-panel switcher. Deep links #buyer / #seller; tab
   clicks push hash entries so back/forward restores the role. Progressive
   enhancement: without JS both panels render (no static `hidden`) and the
   tabs act as plain anchors. No wallet, no chain reads on this page. */
"use strict";
(() => {
  const tabs = [...document.querySelectorAll("[data-guide-role]")];
  if (!tabs.length) return;
  const panels = {};
  for (const t of tabs) {
    const p = document.getElementById(t.getAttribute("aria-controls") || "");
    if (p) panels[t.dataset.guideRole] = p;
  }
  const ROLES = tabs.map((t) => t.dataset.guideRole);
  const DEFAULT_ROLE = "buyer";
  const roleFromHash = () => {
    const h = (location.hash || "").slice(1).toLowerCase();
    return ROLES.includes(h) ? h : DEFAULT_ROLE;
  };

  function apply(role, { push = false, focus = false } = {}) {
    for (const t of tabs) {
      const on = t.dataset.guideRole === role;
      t.setAttribute("aria-selected", String(on));
      t.setAttribute("tabindex", on ? "0" : "-1");
      const p = panels[t.dataset.guideRole];
      if (p) p.hidden = !on;
      if (on && focus) t.focus();
    }
    /* one hash entry per switch → back/forward walk the role history;
       no element owns the ids "buyer"/"seller", so this never scroll-jumps */
    if (push) {
      const target = "#" + role;
      if (location.hash !== target) location.hash = target;
    }
  }

  tabs.forEach((t, i) => {
    t.addEventListener("click", (e) => {
      e.preventDefault();
      apply(t.dataset.guideRole, { push: true });
    });
    /* roving-tabindex tab pattern: arrows move+select, Home/End jump,
       Space activates (anchors natively activate on Enter only) */
    t.addEventListener("keydown", (e) => {
      const k = e.key;
      let nxt = null;
      if (k === "ArrowRight") nxt = tabs[(i + 1) % tabs.length];
      else if (k === "ArrowLeft") nxt = tabs[(i - 1 + tabs.length) % tabs.length];
      else if (k === "Home") nxt = tabs[0];
      else if (k === "End") nxt = tabs[tabs.length - 1];
      else if (k === " ") nxt = t;
      if (!nxt) return;
      e.preventDefault();
      apply(nxt.dataset.guideRole, { push: true, focus: true });
    });
  });

  window.addEventListener("hashchange", () => apply(roleFromHash()));
  apply(roleFromHash());
})();
