(() => {
  const revealUnenhancedScenes = () => {
    const switchers = document.querySelectorAll(
      "[data-scene-switcher]:not([data-enhanced])",
    );
    for (const switcher of switchers) {
      for (const panel of switcher.querySelectorAll(".scene-panel[hidden]")) {
        panel.hidden = false;
        for (const video of panel.querySelectorAll("video[data-poster]")) {
          const poster = video.dataset.poster;
          if (!poster) continue;
          video.setAttribute("poster", poster);
          video.removeAttribute("data-poster");
        }
      }
    }
  };

  if (document.readyState === "complete") {
    revealUnenhancedScenes();
  } else {
    window.addEventListener("load", revealUnenhancedScenes, { once: true });
  }
})();
