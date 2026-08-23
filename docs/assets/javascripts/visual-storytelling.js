(() => {
  /* Resolve a tab's panel without ever building a selector from its id: an
     `aria-controls` value is author data, and interpolating it into
     `querySelector("#" + id)` turns a stray space, dot or digit into a
     different selector — or a SyntaxError — instead of a missing panel.
     `getElementById` takes the id verbatim; containment keeps a switcher
     from adopting a panel that belongs to another one. */
  const panelFor = (switcher, tab) => {
    const id = tab.getAttribute("aria-controls");
    const panel = id ? document.getElementById(id) : null;
    if (!(panel instanceof HTMLElement) || !switcher.contains(panel)) {
      throw new Error(`Missing scene panel for ${tab.id || "an unnamed tab"}`);
    }
    return panel;
  };

  const promotePoster = (panel) => {
    for (const video of panel.querySelectorAll("video[data-poster]")) {
      const poster = video.dataset.poster;
      if (!poster) continue;
      video.setAttribute("poster", poster);
      video.removeAttribute("data-poster");
    }
  };

  /* The authored markup is the no-JavaScript fallback: every panel visible,
     the tab strip hidden by CSS while `data-enhanced` is absent. Restoring
     it is what keeps a failed enhancement from leaving a half-switched page
     behind. */
  const readAuthoredTabState = (tabs) =>
    tabs.map((tab) => [tab, tab.getAttribute("aria-selected"), tab.getAttribute("tabindex")]);

  const restoreFallback = (switcher, authoredTabState) => {
    switcher.removeAttribute("data-enhanced");
    for (const panel of switcher.querySelectorAll(".scene-panel")) {
      panel.hidden = false;
      promotePoster(panel);
    }
    for (const [tab, selected, tabIndex] of authoredTabState) {
      if (selected === null) tab.removeAttribute("aria-selected");
      else tab.setAttribute("aria-selected", selected);
      if (tabIndex === null) tab.removeAttribute("tabindex");
      else tab.setAttribute("tabindex", tabIndex);
    }
  };

  const enhance = (switcher, tabs) => {
    if (tabs.length === 0) {
      throw new Error("Scene switcher has no tabs");
    }

    /* Every tab is resolved before a single `hidden`, `aria-selected` or
       `tabindex` is written, so a switcher that cannot be driven is never
       partially rewritten. */
    const panels = new Map(tabs.map((tab) => [tab, panelFor(switcher, tab)]));

    const select = (nextTab, focus) => {
      for (const tab of tabs) {
        const selected = tab === nextTab;
        const panel = panels.get(tab);
        if (!(panel instanceof HTMLElement)) {
          throw new Error(`Missing scene panel for ${tab.id || "an unnamed tab"}`);
        }
        tab.setAttribute("aria-selected", String(selected));
        tab.tabIndex = selected ? 0 : -1;
        panel.hidden = !selected;
        if (selected) {
          promotePoster(panel);
        }
        if (!selected) {
          for (const video of panel.querySelectorAll("video")) {
            video.pause();
          }
        }
      }
      if (focus) nextTab.focus();
    };

    select(tabs.find((tab) => tab.getAttribute("aria-selected") === "true") ?? tabs[0], false);

    /* The stylesheet reveals the tab strip on this hook, so it is set only
       once the switcher demonstrably works. */
    switcher.dataset.enhanced = "true";

    for (const tab of tabs) {
      tab.addEventListener("click", () => select(tab, false));
      tab.addEventListener("keydown", (event) => {
        const index = tabs.indexOf(tab);
        const keys = new Map([
          ["ArrowLeft", (index - 1 + tabs.length) % tabs.length],
          ["ArrowRight", (index + 1) % tabs.length],
          ["Home", 0],
          ["End", tabs.length - 1],
        ]);
        const nextIndex = keys.get(event.key);
        if (nextIndex === undefined) return;
        event.preventDefault();
        select(tabs[nextIndex], true);
      });
    }

    if (typeof IntersectionObserver === "function") {
      const observer = new IntersectionObserver((entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) continue;
          for (const video of switcher.querySelectorAll("video")) {
            video.pause();
          }
        }
      });
      observer.observe(switcher);
    }
  };

  for (const switcher of document.querySelectorAll("[data-scene-switcher]")) {
    const tabs = Array.from(switcher.querySelectorAll('[role="tab"]'));
    const authoredTabState = readAuthoredTabState(tabs);
    try {
      enhance(switcher, tabs);
    } catch (error) {
      /* One malformed switcher must cost only itself: roll this one back to
         the no-JavaScript rendering, say why, and keep initializing the
         rest of the page. */
      restoreFallback(switcher, authoredTabState);
      console.error("korvid: scene switcher left unenhanced", error);
    }
  }
})();
