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

  /* `prefers-reduced-motion: reduce` must suppress every programmatic
     autoplay, and must keep suppressing it when a visitor turns the
     preference on *during* a visit. One `MediaQueryList` is built for the
     whole page and consulted live, instead of a throwaway query per play
     attempt, so a single `change` subscription can reach everything already
     running. Feature-detected: a browser without `matchMedia` states no
     preference at all, and keeps its motion. */
  const reducedMotion =
    typeof matchMedia === "function" ? matchMedia("(prefers-reduced-motion: reduce)") : null;

  const motionAllowed = () => !(reducedMotion && reducedMotion.matches);

  /* Every video this controller may start by itself. Registering them is
     what lets a mid-visit preference change reach media that is already
     playing; re-querying the document from the change handler would instead
     reach videos the controller has no business touching. */
  const managedVideos = new Set();
  const handledVideoErrors = new WeakSet();
  const reportedPlaybackFailures = new WeakMap();

  const reportVideoFailure = (video, error) => {
    if (reportedPlaybackFailures.get(video)) return;
    reportedPlaybackFailures.set(video, true);
    restoreVideoPoster(video, video.error ?? error);
  };

  const manageVideo = (video) => {
    managedVideos.add(video);
    if (handledVideoErrors.has(video)) return;
    handledVideoErrors.add(video);
    video.addEventListener("error", (event) => {
      reportVideoFailure(video, event);
    });
  };

  const manageVideos = (root) => {
    for (const video of root.querySelectorAll("video")) manageVideo(video);
  };

  const pauseManagedVideos = () => {
    for (const video of managedVideos) video.pause();
  };

  /* Turning `reduce` on stops all of it at once. Turning it back off
     deliberately resumes nothing: relaxing a preference is not a request for
     motion, so playback returns only through an ordinary visibility or
     selection event — or the native controls, which never go away. Older
     browsers whose `MediaQueryList` predates `addEventListener` simply keep
     the read-at-play-time behaviour. */
  if (reducedMotion && typeof reducedMotion.addEventListener === "function") {
    reducedMotion.addEventListener("change", () => {
      if (reducedMotion.matches) pauseManagedVideos();
    });
  }

  /* Below-fold scene video bytes are deferred behind `data-src` until the
     scene is actually selected, mirroring `promotePoster` above. Idempotent:
     a video with no deferred source (already promoted, or never deferred)
     is left untouched. */
  const promoteVideo = (video) => {
    const source = video.dataset.src;
    if (source) {
      video.setAttribute("src", source);
      video.removeAttribute("data-src");
      video.load?.();
    }
  };

  /* Restarting from the beginning — rather than resuming — is what makes a
     scene feel like a looping GIF each time it becomes the visible one
     again, whether by tab selection or by scrolling back into view. A
     rejected `play()` promise (autoplay blocked by browser policy) is
     expected, not an application error: the poster and native controls
     remain exactly as they were. */
  const startFromBeginning = (video) => {
    if (!motionAllowed()) return;
    promoteVideo(video);
    video.currentTime = 0;
    reportedPlaybackFailures.set(video, false);
    const playback = video.play();
    if (playback && typeof playback.catch === "function") {
      playback.catch((error) => {
        if (error && (error.name === "NotAllowedError" || error.name === "AbortError")) return;
        reportVideoFailure(video, error);
      });
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
      /* Dropping `data-poster` is what reveals the `<video>` and hides the
         `.scene-panel__fallback` image beside it, so the source has to be
         promoted in the same pass: a revealed player still holding only
         `data-src` would replace a real product frame with an empty one. */
      promotePoster(panel);
      for (const video of panel.querySelectorAll("video")) {
        promoteVideo(video);
      }
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
    const mediaBox = switcher.querySelector(".scene-panels");
    if (!(mediaBox instanceof HTMLElement)) {
      throw new Error("Scene switcher has no media box");
    }

    /* Playback is a visible-only contract, so visibility that has not been
       reported is not visibility: a switcher stays quiet until an
       `IntersectionObserver` says it is on screen, and a browser without one
       never autoplays at all. Tabs, posters, deferred sources and the native
       controls all keep working there — only the automatic start is
       withheld. */
    let switcherVisible = false;

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
      const selectedVideo = panels.get(nextTab).querySelector("video");
      if (selectedVideo) {
        promoteVideo(selectedVideo);
        if (switcherVisible) startFromBeginning(selectedVideo);
      }
    };

    select(tabs.find((tab) => tab.getAttribute("aria-selected") === "true") ?? tabs[0], false);

    /* The stylesheet reveals the tab strip on this hook, so it is set only
       once the switcher demonstrably works. Its media joins the managed set
       in the same breath: from here on the controller may start these
       videos, so a reduced-motion change has to be able to stop them. */
    switcher.dataset.enhanced = "true";
    manageVideos(switcher);

    for (const tab of tabs) {
      tab.addEventListener("click", () => select(tab, false));
      tab.addEventListener("keydown", (event) => {
        /* A modified chord is a browser or OS command, not tab navigation:
           `Alt+ArrowLeft`/`Alt+ArrowRight` are history back/forward,
           `Ctrl/Cmd+Home`/`Ctrl/Cmd+End` jump to the ends of the document,
           `Shift+Arrow` extends a selection. Bail out before anything is
           recognised or prevented, or the command is already lost. */
        if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
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
          switcherVisible = entry.isIntersecting;
          if (!entry.isIntersecting) {
            for (const video of switcher.querySelectorAll("video")) {
              video.pause();
            }
            continue;
          }
          const selectedTab = tabs.find((tab) => tab.getAttribute("aria-selected") === "true");
          const selectedVideo = selectedTab ? panels.get(selectedTab).querySelector("video") : null;
          if (selectedVideo) startFromBeginning(selectedVideo);
        }
      });
      observer.observe(mediaBox);
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
