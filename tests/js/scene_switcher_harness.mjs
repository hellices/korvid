// Executes the shipped scene-switcher controller against a minimal DOM.
//
// `docs/assets/javascripts/visual-storytelling.js` is the only script the
// documentation site ships, and its failure mode is what a source-reading
// test cannot see: whether a switcher whose markup is broken leaves the page
// half-enhanced (a visible tab strip that switches nothing, panels stuck
// hidden) and whether a later, healthy switcher still initializes.
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

  focus() {
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

function run(document, { intersectionObserver = true, reducedMotion = false } = {}) {
  const errors = [];
  const observers = [];
  const sandbox = {
    document,
    HTMLElement,
    console: { error: (...args) => errors.push(args.map(String).join(" ")) },
    matchMedia: () => ({ matches: reducedMotion }),
  };
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
  return { errors, observers };
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

  "a browser without IntersectionObserver still gets a working switcher"() {
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
