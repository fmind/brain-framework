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

    // The overlay duplicates the named drawer control; code action landmarks need distinct names.
    document.querySelector(".md-overlay")?.setAttribute("aria-hidden", "true");
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
