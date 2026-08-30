import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(
  new URL("../../docs/assets/javascripts/scene-fallback.js", import.meta.url),
  "utf8",
);

function run({ enhanced = false, readyState = "loading" } = {}) {
  const videos = ["agent.png", "mcp.png"].map((poster) => ({
    dataset: { poster },
    poster: null,
    setAttribute(name, value) {
      assert.equal(name, "poster");
      this.poster = value;
    },
    removeAttribute(name) {
      assert.equal(name, "data-poster");
      delete this.dataset.poster;
    },
  }));
  const panels = videos.map((video) => ({
    hidden: true,
    querySelectorAll(selector) {
      assert.equal(selector, "video[data-poster]");
      return "poster" in video.dataset ? [video] : [];
    },
  }));
  const switcher = {
    querySelectorAll(selector) {
      assert.equal(selector, ".scene-panel[hidden]");
      return panels.filter((panel) => panel.hidden);
    },
  };
  let onLoad = null;
  const sandbox = {
    document: {
      readyState,
      querySelectorAll(selector) {
        assert.equal(selector, "[data-scene-switcher]:not([data-enhanced])");
        return enhanced ? [] : [switcher];
      },
    },
    window: {
      addEventListener(type, listener, options) {
        assert.equal(type, "load");
        assert.equal(options.once, true);
        onLoad = listener;
      },
    },
  };

  vm.runInNewContext(source, sandbox);
  return {
    panels,
    videos,
    fireLoad: () => onLoad?.(),
    hasLoadListener: () => onLoad !== null,
  };
}

const scenarios = {
  "controller load failure reveals authored-hidden scenes"() {
    const result = run();
    result.fireLoad();
    assert.deepEqual(
      result.panels.map((panel) => panel.hidden),
      [false, false],
    );
    assert.deepEqual(
      result.videos.map((video) => [video.poster, video.dataset.poster]),
      [
        ["agent.png", undefined],
        ["mcp.png", undefined],
      ],
      "controller failure must expose playable videos with native controls",
    );
  },

  "enhanced scenes keep their controller-owned visibility"() {
    const result = run({ enhanced: true });
    result.fireLoad();
    assert.deepEqual(
      result.panels.map((panel) => panel.hidden),
      [true, true],
    );
    assert.deepEqual(
      result.videos.map((video) => video.dataset.poster),
      ["agent.png", "mcp.png"],
    );
  },

  "a late watchdog reveals scenes synchronously without a load listener"() {
    const result = run({ readyState: "complete" });
    assert.equal(result.hasLoadListener(), false);
    assert.deepEqual(
      result.panels.map((panel) => panel.hidden),
      [false, false],
    );
    assert.deepEqual(
      result.videos.map((video) => video.poster),
      ["agent.png", "mcp.png"],
    );
  },
};

for (const [name, scenario] of Object.entries(scenarios)) {
  try {
    scenario();
    console.log(`ok ${name}`);
  } catch (error) {
    console.error(`not ok ${name}`);
    throw error;
  }
}
