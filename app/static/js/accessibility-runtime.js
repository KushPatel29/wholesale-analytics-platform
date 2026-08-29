(function () {
  "use strict";

  function regionLabel(wrapper) {
    var caption = wrapper.querySelector("table > caption");
    if (caption && caption.textContent.trim()) return caption.textContent.trim();

    var section = wrapper.closest("section, article, .card, [role='region']");
    var heading = section && section.querySelector("h1, h2, h3, h4, h5, h6");
    if (heading && heading.textContent.trim()) {
      return "Scrollable data table: " + heading.textContent.trim();
    }
    return "Scrollable data table";
  }

  function enhanceWrapper(wrapper) {
    if (!wrapper.querySelector("table")) return;
    if (!wrapper.hasAttribute("tabindex")) wrapper.setAttribute("tabindex", "0");
    if (!wrapper.hasAttribute("role")) wrapper.setAttribute("role", "region");
    if (!wrapper.hasAttribute("aria-label") && !wrapper.hasAttribute("aria-labelledby")) {
      wrapper.setAttribute("aria-label", regionLabel(wrapper));
    }
  }

  function enhanceTables(scope) {
    var root = scope && scope.querySelectorAll ? scope : document;
    if (root.matches && root.matches(".table-responsive")) enhanceWrapper(root);
    root.querySelectorAll(".table-responsive").forEach(enhanceWrapper);

    // A table can be injected into an already-present empty scroll wrapper.
    // In that case the mutation's root is the table, not the wrapper itself.
    var ancestor = root.closest && root.closest(".table-responsive");
    if (ancestor) enhanceWrapper(ancestor);
  }

  function start() {
    enhanceTables(document);
    var observer = new MutationObserver(function (records) {
      records.forEach(function (record) {
        record.addedNodes.forEach(function (node) {
          if (node.nodeType !== Node.ELEMENT_NODE) return;
          enhanceTables(node);
        });
      });
    });
    observer.observe(document.body, { childList: true, subtree: true });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start, { once: true });
  else start();
})();
