"use strict";
// Loaded before the page renders to avoid a light flash. "auto" follows Home Assistant's
// theme when the panel runs in the same-origin ingress iframe, otherwise the system setting.
(() => {
  const KEY = "nibe-comfort-theme";
  const modes = ["auto", "light", "dark"];
  const labels = { auto: "Tema: auto", light: "Tema: ljust", dark: "Tema: mörkt" };
  const system = window.matchMedia("(prefers-color-scheme: dark)");
  let choice = "auto";
  try { choice = modes.includes(localStorage.getItem(KEY)) ? localStorage.getItem(KEY) : "auto"; } catch { /* storage blocked */ }

  const luminance = (color) => {
    const match = /rgba?\(\s*([\d.]+)[ ,]+([\d.]+)[ ,]+([\d.]+)(?:[ ,/]+([\d.]+%?))?/.exec(color || "");
    if (!match || (match[4] !== undefined && parseFloat(match[4]) === 0)) return null;
    return (0.2126 * match[1] + 0.7152 * match[2] + 0.0722 * match[3]) / 255;
  };
  const resolveColor = (doc, value) => {
    if (!value) return null;
    const probe = doc.createElement("span");
    probe.style.color = value;
    probe.style.display = "none";
    doc.body.append(probe);
    const color = doc.defaultView.getComputedStyle(probe).color;
    probe.remove();
    return color;
  };
  const parentDocument = () => {
    try {
      if (window.parent === window) return null;
      return window.parent.document;
    } catch { return null; }
  };
  const homeAssistantDark = () => {
    const doc = parentDocument();
    if (!doc || !doc.body) return null;
    const style = doc.defaultView.getComputedStyle(doc.documentElement);
    const scheme = style.colorScheme || "";
    if (/^dark$/.test(scheme.trim())) return true;
    if (/^light$/.test(scheme.trim())) return false;
    for (const value of [style.getPropertyValue("--primary-background-color").trim(), doc.defaultView.getComputedStyle(doc.body).backgroundColor]) {
      const light = luminance(resolveColor(doc, value));
      if (light !== null) return light < 0.5;
    }
    return null;
  };
  const apply = () => {
    const dark = choice === "dark" || (choice === "auto" && (homeAssistantDark() ?? system.matches));
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    const button = document.getElementById("theme-toggle");
    if (button) {
      button.textContent = labels[choice];
      button.title = "Växla mellan automatiskt (följer Home Assistant), ljust och mörkt tema";
    }
    window.dispatchEvent(new Event("nibe-theme"));
  };
  apply();
  system.addEventListener?.("change", apply);
  const doc = parentDocument();
  if (doc) {
    const observer = new MutationObserver(() => { if (choice === "auto") apply(); });
    for (const node of [doc.documentElement, doc.body].filter(Boolean)) observer.observe(node, { attributes: true, attributeFilter: ["style", "class"] });
  }
  document.addEventListener("DOMContentLoaded", () => {
    apply();
    document.getElementById("theme-toggle")?.addEventListener("click", () => {
      choice = modes[(modes.indexOf(choice) + 1) % modes.length];
      try { localStorage.setItem(KEY, choice); } catch { /* storage blocked */ }
      apply();
    });
  });
})();
