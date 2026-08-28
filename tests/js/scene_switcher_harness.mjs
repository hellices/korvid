// Executes the shipped scene-switcher controller against a minimal DOM.
//
// `docs/assets/javascripts/visual-storytelling.js` is the only script the
// documentation site ships, and its failure mode is what a source-reading
// test cannot see: whether a switcher whose markup is broken leaves the page
// half-enhanced (a visible tab strip that switches nothing, panels stuck
// hidden, a revealed player with no source) and whether a later, healthy
// switcher still initializes.
//
// The repository ships no JavaScript dependencies and must not grow one for
// a documentation script, so this file implements exactly the DOM surface
// the controller touches — no more — and runs the real, unmodified source in
// a `node:vm` context. Run it directly: `node tests/js/scene_switcher_harness.mjs`.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";

const CONTROLLER = new URL(
  "../../docs/assets/javascripts/visual-storytelling.js",
  import.meta.url,
);

const MOTION_QUERY = "(prefers-reduced-motion: reduce)";

/** The `MediaQueryList` half of `matchMedia`, including the `change` registry
 *  a controller has to subscribe to in order to notice a visitor turning
 *  `prefers-reduced-motion` on mid-visit.
 *
 *  One instance is shared by every `matchMedia()` call in a run on purpose: a
 *  stub that handed back a fresh object per call could never deliver a
 *  preference change to anybody, which is precisely the failure this fake
 *  exists to catch. `changeEvents: false` models the older browsers whose
 *  `MediaQueryList` has no `addEventListener` at all. */
class MediaQueryListFake {
  constructor(matches, { changeEvents = true } = {}) {
    this.media = MOTION_QUERY;
    this.matches = matches;
    this.listeners = [];
    if (changeEvents) {
      this.addEventListener = (type, handler) => {
        if (type === "change") this.listeners.push(handler);
      };
    }
  }

  /** Flip the preference the way an OS setting would, and notify listeners. */
  set(matches) {
    this.matches = matches;
    for (const handler of this.listeners) {
      handler({ type: "change", media: MOTION_QUERY, matches });
    }
  }
}

class HTMLElement {
  constructor(tag, attributes = {}) {
    this.tagName = tag.toUpperCase();
    this.attributes = new Map(Object.entries(attributes));
    this.children = [];
    this.parent = null;
    this.listeners = new Map();
    this.focused = false;
    this.paused = 0;
    this.played = 0;
    this.currentTime = 7;
    this.playError = null;
    this.dataset = datasetFor(this);
  }

  get id() {
    return this.attributes.get("id") ?? "";
  }

  getAttribute(name) {
    return this.attributes.has(name) ? this.attributes.get(name) : null;
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }

  removeAttribute(name) {
    this.attributes.delete(name);
  }

  get hidden() {
    return this.attributes.has("hidden");
  }

  set hidden(value) {
    if (value) this.attributes.set("hidden", "");
    else this.attributes.delete("hidden");
  }

  get tabIndex() {
    return Number(this.attributes.get("tabindex") ?? 0);
  }

  set tabIndex(value) {
    this.attributes.set("tabindex", String(value));
  }

  append(...children) {
    for (const child of children) {
      child.parent = this;
      this.children.push(child);
    }
    return this;
  }

  descendants() {
    return this.children.flatMap((child) => [child, ...child.descendants()]);
  }

  contains(node) {
    return node === this || this.descendants().includes(node);
  }

  querySelectorAll(selector) {
    return this.descendants().filter((node) => matches(node, selector));
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] ?? null;
  }

  addEventListener(type, handler) {
    const handlers = this.listeners.get(type) ?? [];
    handlers.push(handler);
    this.listeners.set(type, handlers);
  }

  dispatch(type, event = {}) {
    for (const handler of this.listeners.get(type) ?? []) handler(event);
  }

  /* Focus is exclusive in a real DOM: moving it to one element takes it off
     whichever element held it. The roving-tabindex tab strip depends on that
     — a strip that "focused" three tabs at once would satisfy a sticky
     per-element flag while leaving a visitor's focus ring behind. */
  focus() {
    let root = this;
    while (root.parent) root = root.parent;
    root.focused = false;
    for (const node of root.descendants()) node.focused = false;
    this.focused = true;
  }

  pause() {
    this.paused += 1;
  }

  play() {
    this.played += 1;
    return this.playError === null ? Promise.resolve() : Promise.reject(this.playError);
  }
}

function datasetFor(element) {
  const attribute = (key) => `data-${key.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`)}`;
  return new Proxy(
    {},
    {
      get: (_target, key) => element.attributes.get(attribute(key)),
      set: (_target, key, value) => {
        element.attributes.set(attribute(key), String(value));
        return true;
      },
      has: (_target, key) => element.attributes.has(attribute(key)),
      deleteProperty: (_target, key) => {
        element.attributes.delete(attribute(key));
        return true;
      },
    },
  );
}

/** Match the simple selectors the controller uses: `tag`, `.class`, `[attr]`,
 *  `[attr="value"]`, `#id`, and a tag followed by one of those. */
function matches(node, selector) {
  const parts = selector.trim().match(/^([a-z]+)?(.*)$/i);
  assert.ok(parts, `unsupported selector ${selector}`);
  const [, tag, rest] = parts;
  if (tag && node.tagName !== tag.toUpperCase()) return false;
  if (!rest) return Boolean(tag);
  if (rest.startsWith("#")) return node.id === rest.slice(1);
  if (rest.startsWith(".")) {
    return (node.getAttribute("class") ?? "").split(/\s+/).includes(rest.slice(1));
  }
  const attribute = rest.match(/^\[([\w-]+)(?:="([^"]*)")?\]$/);
  assert.ok(attribute, `unsupported selector ${selector}`);
  const [, name, value] = attribute;
  if (!node.attributes.has(name)) return false;
  return value === undefined || node.attributes.get(name) === value;
}

function element(tag, attributes, ...children) {
  return new HTMLElement(tag, attributes).append(...children);
}

/** Build one switcher shaped like the landing page's, with three scenes. */
function buildSwitcher(prefix, { brokenTab = null, panelOutside = false } = {}) {
  const scenes = ["direct", "agent", "mcp"];
  const tabs = scenes.map((scene, index) =>
    element("button", {
      id: `${prefix}-tab-${scene}`,
      role: "tab",
      "aria-selected": index === 0 ? "true" : "false",
      ...(index === 0 ? {} : { tabindex: "-1" }),
      "aria-controls": brokenTab === scene ? `${prefix}-${scene}-missing` : `${prefix}-${scene}`,
    }),
  );
  const videos = scenes.map((scene, index) =>
    element("video", {
      controls: "",
      ...(index === 0
        ? { src: `${scene}.mp4`, poster: `${scene}.png` }
        : { "data-src": `${scene}.mp4`, "data-poster": `${scene}.png` }),
    }),
  );
  const panels = scenes.map((scene, index) =>
    element(
      "article",
      { id: `${prefix}-${scene}`, class: "scene-panel", role: "tabpanel" },
      videos[index],
    ),
  );
  const switcher = element(
    "section",
    { class: "scene-switcher", "data-scene-switcher": "" },
    element("div", { class: "scene-tabs", role: "tablist" }, ...tabs),
    element("div", { class: "scene-panels" }, ...(panelOutside ? panels.slice(0, 2) : panels)),
  );
  return { switcher, tabs, panels, videos, stray: panelOutside ? panels[2] : null };
}

function buildDocument(switchers, strays = []) {
  const body = element("body", {}, ...switchers, ...strays);
  return {
    body,
    querySelectorAll: (selector) => body.querySelectorAll(selector),
    getElementById: (id) => body.descendants().find((node) => node.id === id) ?? null,
  };
}

function run(
  document,
  {
    intersectionObserver = true,
    reducedMotion = false,
    matchMedia = true,
    motionChangeEvents = true,
  } = {},
) {
  const errors = [];
  const observers = [];
  const queries = [];
  const media = new MediaQueryListFake(reducedMotion, { changeEvents: motionChangeEvents });
  const sandbox = {
    document,
    HTMLElement,
    console: { error: (...args) => errors.push(args.map(String).join(" ")) },
  };
  if (matchMedia) {
    sandbox.matchMedia = (query) => {
      queries.push(query);
      return media;
    };
  }
  if (intersectionObserver) {
    sandbox.IntersectionObserver = class {
      constructor(callback) {
        this.callback = callback;
        observers.push(this);
      }

      observe(target) {
        this.target = target;
      }
    };
  }
  const context = createContext(sandbox);
  runInContext(readFileSync(CONTROLLER, "utf8"), context, { filename: "visual-storytelling.js" });
  return { errors, observers, media, queries };
}

const scenarios = {
  "healthy switchers enhance, defer posters, and answer the keyboard"() {
    const first = buildSwitcher("a");
    const second = buildSwitcher("b");
    const document = buildDocument([first.switcher, second.switcher]);
    const { errors, observers } = run(document);

    assert.deepEqual(errors, [], "a well-formed switcher must not log an error");
    for (const built of [first, second]) {
      assert.equal(built.switcher.getAttribute("data-enhanced"), "true");
      assert.deepEqual(
        built.panels.map((panel) => panel.hidden),
        [false, true, true],
        "only the selected panel stays visible",
      );
      assert.deepEqual(
        built.tabs.map((tab) => tab.getAttribute("aria-selected")),
        ["true", "false", "false"],
      );
      assert.deepEqual(
        built.tabs.map((tab) => tab.tabIndex),
        [0, -1, -1],
      );
      assert.equal(built.videos[1].getAttribute("poster"), null, "deferred poster stays deferred");
    }

    first.tabs[0].dispatch("keydown", { key: "ArrowRight", preventDefault() {} });
    assert.deepEqual(
      first.panels.map((panel) => panel.hidden),
      [true, false, true],
      "ArrowRight moves the selection to the next scene",
    );
    assert.equal(first.videos[1].getAttribute("poster"), "agent.png", "poster promoted on select");
    assert.equal(first.videos[1].getAttribute("data-poster"), null);
    assert.ok(first.videos[0].paused > 0, "leaving a scene pauses its video");
    assert.ok(first.tabs[1].focused, "keyboard selection moves focus with it");

    observers[0].callback([{ isIntersecting: false }]);
    assert.ok(
      first.videos.every((video) => video.paused > 0),
      "an off-screen switcher pauses every video it contains",
    );
    assert.ok(
      first.videos.every((video) => video.played === 0),
      "the controller never resumes playback itself",
    );
  },

  "a visible switcher starts the selected scene and restarts the next one on selection"() {
    const first = buildSwitcher("a");
    const document = buildDocument([first.switcher]);
    const { observers } = run(document);

    observers[0].callback([{ isIntersecting: true }]);
    assert.equal(
      first.videos[0].played,
      1,
      "entering the viewport starts the already-selected scene",
    );
    assert.equal(
      first.videos[0].currentTime,
      0,
      "playback must restart from the beginning, not resume mid-scene",
    );
    assert.equal(first.videos[1].played, 0, "an unselected scene must never be started");

    first.tabs[1].dispatch("click", {});
    assert.ok(first.videos[0].paused > 0, "switching away pauses the previously playing scene");
    assert.equal(
      first.videos[1].played,
      1,
      "the newly selected scene starts because the switcher is already visible",
    );
    assert.equal(
      first.videos[1].getAttribute("src"),
      "agent.mp4",
      "selecting a scene promotes its deferred video source",
    );
    assert.equal(first.videos[1].getAttribute("data-src"), null, "the deferred attribute is dropped");

    observers[0].callback([{ isIntersecting: false }]);
    assert.ok(
      first.videos.every((video) => video.paused > 0),
      "leaving the viewport pauses every scene, playing or not",
    );
  },

  "prefers-reduced-motion suppresses autoplay even while visible"() {
    const built = buildSwitcher("a");
    const document = buildDocument([built.switcher]);
    const { observers } = run(document, { reducedMotion: true });

    observers[0].callback([{ isIntersecting: true }]);
    assert.equal(
      built.videos[0].played,
      0,
      "a reduced-motion visitor must never see programmatic autoplay",
    );

    built.tabs[1].dispatch("click", {});
    assert.equal(
      built.videos[1].played,
      0,
      "selecting a scene under reduced motion must still wait for a manual play",
    );
  },

  "a rejected play() promise is swallowed without rolling back the switcher"() {
    const built = buildSwitcher("a");
    const document = buildDocument([built.switcher]);
    const { errors, observers } = run(document);
    built.videos[0].playError = new Error("NotAllowedError");

    assert.doesNotThrow(() => {
      observers[0].callback([{ isIntersecting: true }]);
    });
    assert.deepEqual(errors, [], "a browser-blocked autoplay must not be reported as a failure");
    assert.equal(
      built.switcher.getAttribute("data-enhanced"),
      "true",
      "a blocked autoplay must not roll the switcher back to its no-JavaScript state",
    );
    assert.equal(
      built.videos[0].getAttribute("controls"),
      "",
      "native controls must remain available after a blocked autoplay",
    );
    assert.equal(
      built.videos[0].getAttribute("poster"),
      "direct.png",
      "a blocked autoplay must not remove the already-visible poster",
    );
  },

  async "a media playback failure is reported and restores the scene poster"() {
    const built = buildSwitcher("a");
    const document = buildDocument([built.switcher]);
    const { errors, observers } = run(document);
    built.videos[1].playError = Object.assign(new Error("unsupported codec"), {
      name: "NotSupportedError",
    });

    built.tabs[1].dispatch("click", {});
    observers[0].callback([{ isIntersecting: true }]);
    await Promise.resolve();

    assert.equal(errors.length, 1, "a media failure must be reported");
    assert.equal(
      built.videos[1].getAttribute("data-poster"),
      "agent.png",
      "the CSS fallback must hide the failed player and reveal its poster image",
    );
    assert.ok(built.videos[1].paused > 0, "the failed player must be stopped");
  },

  async "a late media error after a successful play restores the scene poster"() {
    const built = buildSwitcher("a");
    const document = buildDocument([built.switcher]);
    const { errors, observers } = run(document);
    const decodeFailure = new Error("decode failure");

    built.tabs[1].dispatch("click", {});
    observers[0].callback([{ isIntersecting: true }]);
    await Promise.resolve();

    built.videos[1].error = decodeFailure;
    built.videos[1].dispatch("error", { type: "error" });

    assert.equal(errors.length, 1, "a late media failure must be reported exactly once");
    assert.match(errors[0], /decode failure/, "the logged failure should include the media error");
    assert.ok(built.videos[1].paused > 0, "the failed player must be stopped");
    assert.equal(
      built.videos[1].getAttribute("data-poster"),
      "agent.png",
      "the CSS fallback must hide the failed player and reveal its poster image",
    );
  },

  async "a late media error before a rejected play settles reports one failure"() {
    const built = buildSwitcher("a");
    const document = buildDocument([built.switcher]);
    const { errors, observers } = run(document);
    const rejection = Object.assign(new Error("unsupported codec"), { name: "NotSupportedError" });
    const lateError = new Error("decode failure");
    built.videos[1].playError = rejection;

    built.tabs[1].dispatch("click", {});
    observers[0].callback([{ isIntersecting: true }]);
    const pausesBeforeFailure = built.videos[1].paused;
    built.videos[1].error = lateError;
    built.videos[1].dispatch("error", { type: "error" });
    await Promise.resolve();

    assert.equal(errors.length, 1, "one failed playback attempt must only be reported once");
    assert.match(errors[0], /decode failure/, "the first real media failure should be what is logged");
    assert.equal(
      built.videos[1].paused,
      pausesBeforeFailure + 1,
      "restoring the fallback once proves the rejection and media event shared one failure latch",
    );
    assert.equal(
      built.videos[1].getAttribute("data-poster"),
      "agent.png",
      "the CSS fallback must still hide the failed player and reveal its poster image",
    );
  },

  async "an interrupted pending play is not reported as a media failure"() {
    const built = buildSwitcher("a");
    const document = buildDocument([built.switcher]);
    const { errors, observers } = run(document);
    built.videos[0].playError = Object.assign(new Error("play interrupted by pause"), {
      name: "AbortError",
    });

    observers[0].callback([{ isIntersecting: true }]);
    observers[0].callback([{ isIntersecting: false }]);
    await Promise.resolve();

    assert.deepEqual(errors, []);
    assert.equal(built.videos[0].getAttribute("data-poster"), null);
  },

  "prototype-named keys are ignored as non-navigation input"() {
    const built = buildSwitcher("a");
    const document = buildDocument([built.switcher]);
    const { errors } = run(document);
    let prevented = false;

    assert.doesNotThrow(() => {
      built.tabs[0].dispatch("keydown", {
        key: "constructor",
        preventDefault() {
          prevented = true;
        },
      });
    });
    assert.equal(prevented, false, "an unrelated key must keep its default behavior");
    assert.deepEqual(errors, []);
    assert.deepEqual(
      built.panels.map((panel) => panel.hidden),
      [false, true, true],
      "an unrelated key must not change the selected scene",
    );
  },

  "turning on reduced motion mid-visit pauses every managed video at once"() {
    /* Two independent switchers, because the property under test is that one
       preference flip reaches *every* video the controller has taken charge
       of — across roots — not just the one that happens to be playing in the
       root the flip was noticed in. */
    const first = buildSwitcher("a");
    const second = buildSwitcher("b");
    const document = buildDocument([first.switcher, second.switcher]);
    const { errors, observers, media, queries } = run(document);
    const managed = [...first.videos, ...second.videos];

    assert.deepEqual(errors, []);
    assert.deepEqual(
      queries,
      [MOTION_QUERY],
      "the controller must build exactly one shared MediaQueryList for the page, " +
        "not a throwaway one per play attempt",
    );
    assert.ok(
      media.listeners.length > 0,
      "the controller must subscribe to `change` so a mid-visit preference flip is noticed",
    );

    observers[0].callback([{ isIntersecting: true }]);
    observers[1].callback([{ isIntersecting: true }]);
    first.tabs[1].dispatch("click", {});
    assert.equal(first.videos[1].played, 1, "the selected scene is playing before the flip");
    assert.equal(
      second.videos[0].played,
      1,
      "the second switcher's own selected scene is playing before the flip",
    );

    const pausesBefore = managed.map((video) => video.paused);
    media.set(true);
    for (const [index, video] of managed.entries()) {
      assert.ok(
        video.paused > pausesBefore[index],
        "every video the controller manages must be paused the moment the visitor " +
          `asks for reduced motion (video ${index})`,
      );
    }

    const playedAfterFlip = managed.map((video) => video.played);
    media.set(false);
    assert.deepEqual(
      managed.map((video) => video.played),
      playedAfterFlip,
      "turning the preference back off must never resume playback by itself; only " +
        "an ordinary visibility or selection event may start motion again",
    );

    observers[0].callback([{ isIntersecting: true }]);
    assert.equal(
      first.videos[1].played,
      playedAfterFlip[1] + 1,
      "a later ordinary visibility event may restart the selected scene once the " +
        "preference is off again — the controller simply never resumes on its own",
    );
  },

  "a reduced-motion flip is still honored where the switcher was already quiet"() {
    const built = buildSwitcher("a");
    const document = buildDocument([built.switcher]);
    const { media, observers } = run(document, { reducedMotion: true });

    observers[0].callback([{ isIntersecting: true }]);
    assert.equal(built.videos[0].played, 0, "reduced motion suppresses the initial start");

    media.set(false);
    assert.equal(
      built.videos[0].played,
      0,
      "relaxing the preference must not start anything on its own either",
    );

    built.tabs[1].dispatch("click", {});
    assert.equal(
      built.videos[1].played,
      1,
      "a visitor-driven selection after the preference is relaxed may start motion",
    );
    const pausedBeforeFlip = built.videos[1].paused;

    media.set(true);
    assert.ok(
      built.videos[1].paused > pausedBeforeFlip,
      "flipping the preference back on pauses the scene that was just started",
    );
  },

  "a browser whose MediaQueryList has no addEventListener still works"() {
    const built = buildSwitcher("a");
    const document = buildDocument([built.switcher]);
    const { errors, observers } = run(document, { motionChangeEvents: false });

    assert.deepEqual(errors, [], "a legacy MediaQueryList must not break the enhancement");
    assert.equal(built.switcher.getAttribute("data-enhanced"), "true");
    observers[0].callback([{ isIntersecting: true }]);
    assert.equal(
      built.videos[0].played,
      1,
      "without change events the preference is still read at play time",
    );
  },

  "a browser without matchMedia still gets a working switcher"() {
    const built = buildSwitcher("a");
    const document = buildDocument([built.switcher]);
    const { errors, observers } = run(document, { matchMedia: false });

    assert.deepEqual(errors, [], "a missing matchMedia must not break the enhancement");
    assert.equal(built.switcher.getAttribute("data-enhanced"), "true");
    observers[0].callback([{ isIntersecting: true }]);
    assert.equal(built.videos[0].played, 1, "no matchMedia means no stated preference to obey");
  },

  "modified arrow and Home/End keys keep their browser behavior"() {
    const built = buildSwitcher("a");
    const document = buildDocument([built.switcher]);
    const { errors } = run(document);

    /* `Alt+ArrowLeft` is browser history back, `Ctrl/Cmd+Home` and
       `Ctrl/Cmd+End` jump to the top/bottom of the document, and
       `Shift+Arrow` extends a selection. A tab strip that swallows any of
       them takes a navigation command away from the visitor. */
    for (const [key, modifier] of [
      ["ArrowLeft", "altKey"],
      ["ArrowRight", "altKey"],
      ["Home", "ctrlKey"],
      ["End", "metaKey"],
      ["ArrowRight", "shiftKey"],
      ["Home", "shiftKey"],
    ]) {
      let prevented = false;
      built.tabs[0].dispatch("keydown", {
        key,
        [modifier]: true,
        preventDefault() {
          prevented = true;
        },
      });
      assert.equal(prevented, false, `${modifier}+${key} must keep its default browser behavior`);
      assert.deepEqual(
        built.panels.map((panel) => panel.hidden),
        [false, true, true],
        `${modifier}+${key} must not move the scene selection`,
      );
    }

    assert.deepEqual(errors, []);

    let unmodifiedPrevented = false;
    built.tabs[0].dispatch("keydown", {
      key: "ArrowRight",
      altKey: false,
      ctrlKey: false,
      metaKey: false,
      shiftKey: false,
      preventDefault() {
        unmodifiedPrevented = true;
      },
    });
    assert.equal(unmodifiedPrevented, true, "an unmodified ArrowRight is still tab navigation");
    assert.deepEqual(
      built.panels.map((panel) => panel.hidden),
      [true, false, true],
      "unmodified arrow navigation must keep working",
    );

    built.tabs[1].dispatch("keydown", { key: "End", preventDefault() {} });
    assert.deepEqual(
      built.panels.map((panel) => panel.hidden),
      [true, true, false],
      "unmodified End still jumps to the last scene",
    );
    built.tabs[2].dispatch("keydown", { key: "Home", preventDefault() {} });
    assert.deepEqual(
      built.panels.map((panel) => panel.hidden),
      [false, true, true],
      "unmodified Home still jumps to the first scene",
    );
  },

  "a switcher whose tab controls no panel is left in the no-JavaScript state"() {
    const broken = buildSwitcher("a", { brokenTab: "mcp" });
    const healthy = buildSwitcher("b");
    const document = buildDocument([broken.switcher, healthy.switcher]);
    const { errors } = run(document);

    assert.equal(
      broken.switcher.getAttribute("data-enhanced"),
      null,
      "a switcher that cannot be enhanced must not claim it was: the stylesheet " +
        "reveals the tab strip on this hook",
    );
    assert.deepEqual(
      broken.panels.map((panel) => panel.hidden),
      [false, false, false],
      "every scene must stay readable, exactly as with no JavaScript at all",
    );
    assert.deepEqual(
      broken.tabs.map((tab) => tab.getAttribute("aria-selected")),
      ["true", "false", "false"],
      "the authored tab state must survive the failed enhancement",
    );
    assert.deepEqual(
      broken.tabs.map((tab) => tab.getAttribute("tabindex")),
      [null, "-1", "-1"],
      "no runtime tabindex may be left behind on a rolled-back switcher",
    );
    assert.deepEqual(
      broken.videos.map((video) => video.getAttribute("poster")),
      ["direct.png", "agent.png", "mcp.png"],
      "every visible fallback scene must retain a product frame",
    );
    assert.ok(
      broken.videos.every((video) => video.getAttribute("data-poster") === null),
      "rollback must finish promoting every deferred poster",
    );
    /* Dropping `data-poster` is what the stylesheet watches: it reveals the
       `<video>` and hides the adjacent `.scene-panel__fallback` image. A
       rollback that promotes only the poster therefore swaps a real product
       frame for a player with no source at all — worse than the no-JavaScript
       rendering it claims to restore. */
    assert.deepEqual(
      broken.videos.map((video) => video.getAttribute("src")),
      ["direct.mp4", "agent.mp4", "mcp.mp4"],
      "rollback must give every revealed video a real source, not a dead player",
    );
    assert.ok(
      broken.videos.every((video) => video.getAttribute("data-src") === null),
      "rollback must stop deferring every scene source it just revealed",
    );
    assert.ok(
      broken.videos.every((video) => video.played === 0),
      "rollback restores media without starting any of it",
    );
    assert.equal(errors.length, 1, "the failure must be reported, not swallowed");
    assert.match(errors[0], /scene switcher/i);

    assert.equal(
      healthy.switcher.getAttribute("data-enhanced"),
      "true",
      "one broken switcher must not stop the next one from initializing",
    );
    assert.deepEqual(
      healthy.panels.map((panel) => panel.hidden),
      [false, true, true],
    );
  },

  "a tab pointing at a panel outside its own switcher is rejected"() {
    const built = buildSwitcher("a", { panelOutside: true });
    const document = buildDocument([built.switcher], [built.stray]);
    const { errors } = run(document);

    assert.equal(built.switcher.getAttribute("data-enhanced"), null);
    assert.equal(errors.length, 1);
    assert.deepEqual(
      built.panels.slice(0, 2).map((panel) => panel.hidden),
      [false, false],
      "the switcher's own panels stay visible",
    );
    assert.equal(built.stray.hidden, false, "a panel outside the switcher is never touched");
  },

  "two ArrowRight presses walk the roving selection to the last scene"() {
    /* The compact strip is a three-column grid on a handset precisely so the
       third tab is on screen when the keyboard reaches it. That is a layout
       contract, verified in a browser; what belongs here is the half the
       controller owns — that a second `ArrowRight` really does land the
       selection, the roving `tabIndex`, and the focus on the MCP tab, with
       its deferred source and poster promoted so the scene it reveals is a
       real player rather than an empty box. */
    const built = buildSwitcher("a");
    const document = buildDocument([built.switcher]);
    const { errors, observers } = run(document);

    observers[0].callback([{ isIntersecting: true }]);
    built.tabs[0].dispatch("keydown", { key: "ArrowRight", preventDefault() {} });
    built.tabs[1].dispatch("keydown", { key: "ArrowRight", preventDefault() {} });

    assert.deepEqual(errors, []);
    assert.deepEqual(
      built.panels.map((panel) => panel.hidden),
      [true, true, false],
      "two ArrowRight presses must leave the MCP scene as the visible one",
    );
    assert.deepEqual(
      built.tabs.map((tab) => tab.getAttribute("aria-selected")),
      ["false", "false", "true"],
      "the MCP tab must be the one that reports itself selected",
    );
    assert.deepEqual(
      built.tabs.map((tab) => tab.tabIndex),
      [-1, -1, 0],
      "the roving tabindex must follow the selection to the last tab",
    );
    assert.ok(built.tabs[2].focused, "the MCP tab must hold focus after arrowing onto it");
    assert.ok(
      built.tabs.slice(0, 2).every((tab) => !tab.focused),
      "focus must not be left behind on a deselected tab",
    );
    assert.equal(built.videos[2].getAttribute("src"), "mcp.mp4", "deferred source promoted");
    assert.equal(built.videos[2].getAttribute("poster"), "mcp.png", "deferred poster promoted");
    assert.ok(
      built.videos.slice(0, 2).every((video) => video.paused > 0),
      "the scenes arrowed past must be paused, not left decoding",
    );

    built.tabs[2].dispatch("keydown", { key: "ArrowRight", preventDefault() {} });
    assert.deepEqual(
      built.panels.map((panel) => panel.hidden),
      [false, true, true],
      "a third press wraps back to the first scene",
    );
  },

  "a browser without IntersectionObserver gets a working switcher that never autoplays"() {
    const built = buildSwitcher("a");
    const document = buildDocument([built.switcher]);
    const { errors } = run(document, { intersectionObserver: false });

    assert.deepEqual(errors, []);
    assert.equal(built.switcher.getAttribute("data-enhanced"), "true");
    built.tabs[2].dispatch("click", {});
    assert.deepEqual(
      built.panels.map((panel) => panel.hidden),
      [true, true, false],
      "tab switching must not depend on IntersectionObserver support",
    );
    /* Playback is a visible-only contract. Without `IntersectionObserver`
       the switcher's visibility is simply unknown, and unknown must not mean
       "assume on screen and play": that is how a browser with no observer
       ends up decoding video the visitor has never scrolled to. The scene is
       still fully usable by hand. */
    assert.ok(
      built.videos.every((video) => video.played === 0),
      "unknown visibility must never autoplay",
    );
    assert.equal(
      built.videos[2].getAttribute("src"),
      "mcp.mp4",
      "selection still promotes the deferred source so the manual play has bytes",
    );
    assert.equal(
      built.videos[2].getAttribute("poster"),
      "mcp.png",
      "selection still promotes the deferred poster",
    );
    assert.equal(
      built.videos[2].getAttribute("controls"),
      "",
      "native controls stay available so the visitor can start the scene by hand",
    );
    assert.equal(built.videos[2].hidden, false, "the selected panel's video is visible");
  },
};

let failed = 0;
for (const [name, scenario] of Object.entries(scenarios)) {
  try {
    scenario();
    console.log(`ok ${name}`);
  } catch (error) {
    failed += 1;
    console.log(`not ok ${name}`);
    console.log(String(error.message ?? error));
  }
}
process.exit(failed === 0 ? 0 : 1);
