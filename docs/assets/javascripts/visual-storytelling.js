(() => {
  const switchers = document.querySelectorAll("[data-scene-switcher]");

  for (const switcher of switchers) {
    const tabs = Array.from(switcher.querySelectorAll('[role="tab"]'));
    if (tabs.length === 0) {
      throw new Error("Scene switcher has no tabs");
    }

    const panels = new Map(
      tabs.map((tab) => [
        tab.id,
        switcher.querySelector(`#${tab.getAttribute("aria-controls")}`),
      ]),
    );

    const select = (nextTab, focus) => {
      for (const tab of tabs) {
        const selected = tab === nextTab;
        tab.setAttribute("aria-selected", String(selected));
        tab.tabIndex = selected ? 0 : -1;
        const panel = panels.get(tab.id);
        if (!(panel instanceof HTMLElement)) {
          throw new Error(`Missing scene panel for ${tab.id}`);
        }
        panel.hidden = !selected;
        if (!selected) {
          for (const video of panel.querySelectorAll("video")) {
            video.pause();
          }
        }
      }
      if (focus) nextTab.focus();
    };

    for (const tab of tabs) {
      tab.addEventListener("click", () => select(tab, false));
      tab.addEventListener("keydown", (event) => {
        const index = tabs.indexOf(tab);
        const keys = {
          ArrowLeft: (index - 1 + tabs.length) % tabs.length,
          ArrowRight: (index + 1) % tabs.length,
          Home: 0,
          End: tabs.length - 1,
        };
        const nextIndex = keys[event.key];
        if (nextIndex === undefined) return;
        event.preventDefault();
        select(tabs[nextIndex], true);
      });
    }

    switcher.dataset.enhanced = "true";
    select(
      tabs.find((tab) => tab.getAttribute("aria-selected") === "true") ?? tabs[0],
      false,
    );
  }
})();
