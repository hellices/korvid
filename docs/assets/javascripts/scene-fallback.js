(() => {
  const revealUnenhancedScenes = () => {
    const switchers = document.querySelectorAll(
      "[data-scene-switcher]:not([data-enhanced])",
    );
    for (const switcher of switchers) {
      for (const panel of switcher.querySelectorAll(".scene-panel[hidden]")) {
        panel.hidden = false;
      }
    }
  };

  if (document.readyState === "complete") {
    revealUnenhancedScenes();
  } else {
    window.addEventListener("load", revealUnenhancedScenes, { once: true });
  }
})();
