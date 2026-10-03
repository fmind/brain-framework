(() => {
  // Native buttons supply Enter/Space behavior and valid semantics; labels do not.
  const buttonFor = (element) => {
    if (element instanceof HTMLButtonElement) return element;
    if (!(element instanceof HTMLLabelElement)) return null;
    const toggle = document.getElementById(element.htmlFor);
    if (!(toggle instanceof HTMLInputElement)) return null;
    const button = document.createElement("button");
    for (const { name, value } of element.attributes) {
      if (!["for", "role", "tabindex"].includes(name)) button.setAttribute(name, value);
    }
    button.type = "button";
    button.dataset.bfToggle = toggle.id;
    button.append(...element.childNodes);
    button.querySelectorAll("svg").forEach((svg) => svg.setAttribute("aria-hidden", "true"));
    button.addEventListener("click", () => {
      toggle.checked = !toggle.checked;
      toggle.dispatchEvent(new Event("change", { bubbles: true }));
    });
    element.replaceWith(button);
    return button;
  };
  const drawer = document.querySelector("#__drawer");
  const trigger = buttonFor(document.querySelector('header label[for="__drawer"]'));
  if (!(drawer instanceof HTMLInputElement) || !trigger) return;

  const navigation = () => document.querySelector('[data-md-type="navigation"]');
  const enhanced = new WeakSet();
  const synchronize = () => {
    const sidebar = navigation();
    const mobile = getComputedStyle(trigger).display !== "none";
    trigger.setAttribute("aria-expanded", String(drawer.checked));
    trigger.setAttribute("aria-controls", "__navigation");
    if (sidebar instanceof HTMLElement) {
      sidebar.id = "__navigation";
      sidebar.inert = mobile && !drawer.checked;
    }

    sidebar?.querySelectorAll('label.md-nav__link[for^="__nav_"], button.md-nav__link[data-bf-toggle^="__nav_"]').forEach((element) => {
      const button = buttonFor(element);
      const toggle = document.getElementById(button?.dataset.bfToggle ?? "");
      const group = button?.closest("li")?.querySelector(":scope > nav");
      if (!button || !(toggle instanceof HTMLInputElement) || !(group instanceof HTMLElement)) return;
      group.id = `${toggle.id}-group-navigation`;
      group.inert = mobile && !toggle.checked;
      button.disabled = !mobile;
      button.setAttribute("aria-controls", group.id);
      button.setAttribute("aria-expanded", String(!mobile || toggle.checked));
      if (!enhanced.has(toggle)) {
        enhanced.add(toggle);
        toggle.addEventListener("change", synchronize);
      }
    });

    const tocToggle = document.querySelector("#__toc");
    const tocTrigger = buttonFor(document.querySelector(".md-sidebar-button"));
    const toc = document.querySelector('[data-md-type="toc"] nav.md-nav--secondary');
    if (tocToggle instanceof HTMLInputElement && tocTrigger && toc instanceof HTMLElement) {
      const compact = getComputedStyle(tocTrigger).display !== "none";
      toc.id = "__toc-navigation";
      tocTrigger.setAttribute("aria-label", "On this page");
      tocTrigger.setAttribute("aria-controls", toc.id);
      tocTrigger.setAttribute("aria-expanded", String(tocToggle.checked));
      toc.inert = compact && !tocToggle.checked;
      const content = toc.closest(".md-sidebar__inner");
      if (content instanceof HTMLElement) content.inert = toc.inert;
      if (!enhanced.has(tocToggle)) {
        enhanced.add(tocToggle);
        tocToggle.addEventListener("change", synchronize);
      }
    }

    // The compact header's search label carries an aria-label that labels may not have.
    buttonFor(document.querySelector('header label[for="__search"]'));

    // The overlay duplicates the named drawer control; landmarks need distinct names.
    document.querySelector(".md-overlay")?.setAttribute("aria-hidden", "true");
    document.querySelector("nav.md-path")?.setAttribute("aria-label", "Breadcrumb");
    document.querySelectorAll("nav.md-code__nav").forEach((nav, index) => {
      nav.setAttribute("aria-label", `Code block ${index + 1} actions`);
    });
    document.querySelectorAll(".md-typeset__scrollwrap").forEach((wrapper, index) => {
      if (wrapper.scrollWidth > wrapper.clientWidth) {
        wrapper.tabIndex = 0;
        wrapper.setAttribute("role", "region");
        const headings = wrapper.querySelector("thead")?.textContent.trim().replace(/\s+/g, " ") ?? "";
        wrapper.setAttribute("aria-label", `Scrollable table ${index + 1}: ${headings}`);
      } else {
        for (const attribute of ["tabindex", "role", "aria-label"]) wrapper.removeAttribute(attribute);
      }
    });
  };
  drawer.addEventListener("change", synchronize);
  window.addEventListener("resize", synchronize);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && drawer.checked && getComputedStyle(trigger).display !== "none") {
      drawer.checked = false;
      synchronize();
      trigger.focus();
    }
  });
  document$.subscribe(synchronize);
})();

(() => {
  // Zensical 0.0.67 renders search in an open shadow root whose class names are minified, so match structure:
  // a panel holding the combobox and a scrollable results list, plus a filter panel holding a heading.
  const contrast = new CSSStyleSheet();
  // Result breadcrumbs default to 45% opacity text, below the 4.5:1 minimum; the class rule needs !important to lose.
  contrast.replaceSync("menu { color: rgb(var(--color-foreground) / 0.72) !important; }");

  const patch = (root) => {
    const input = root.querySelector('input[role="combobox"]');
    const top = root.firstElementChild;
    if (!(input instanceof HTMLInputElement) || !(top instanceof HTMLElement)) return;
    const panel = [...top.children].find((child) => child.contains(input));
    const results = root.querySelector("ol");
    if (!(panel instanceof HTMLElement) || !(results instanceof HTMLElement)) return;

    // The closed dialog is only transparent; hide it from assistive technology and the tab order.
    const open = getComputedStyle(panel).pointerEvents !== "none";
    panel.style.visibility = open ? "" : "hidden";
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-modal", "true");
    panel.setAttribute("aria-label", "Search");
    results.id = "bf-search-results";
    input.setAttribute("aria-label", "Search");
    input.setAttribute("aria-controls", results.id);
    input.setAttribute("aria-expanded", String(open));
    const scroller = results.parentElement;
    if (scroller instanceof HTMLElement && scroller !== panel) {
      scroller.tabIndex = 0;
      scroller.setAttribute("role", "region");
      scroller.setAttribute("aria-label", "Search results");
    }
    for (const button of root.querySelectorAll("button")) {
      button.querySelectorAll("svg").forEach((svg) => svg.setAttribute("aria-hidden", "true"));
      if (button.querySelector("svg.lucide-search")) button.setAttribute("aria-label", "Search");
      if (button.querySelector("svg.lucide-list-filter")) button.setAttribute("aria-label", "Filters");
    }

    // This site has no tags, so the filter panel and its button would offer only empty headings.
    const filters = [...panel.querySelectorAll(":scope > *")].find((child) => !child.contains(input) && child.querySelector("h3"));
    const empty = filters instanceof HTMLElement && !filters.querySelector("li");
    if (filters instanceof HTMLElement) filters.hidden = empty;
    root.querySelector("button:has(svg.lucide-list-filter)")?.toggleAttribute("hidden", empty);
    if (!root.adoptedStyleSheets.includes(contrast)) root.adoptedStyleSheets = [...root.adoptedStyleSheets, contrast];
  };

  // The search host can render its shadow content after insertion, so watch every shadow root on the body.
  const observed = new WeakSet();
  const attach = () => {
    for (const host of document.body.children) {
      const root = host.shadowRoot;
      if (!root || observed.has(root)) continue;
      observed.add(root);
      patch(root);
      new MutationObserver(() => patch(root)).observe(root, { attributes: true, attributeFilter: ["class"], childList: true, subtree: true });
    }
  };
  new MutationObserver(attach).observe(document.body, { childList: true });
  document$.subscribe(attach);
})();
