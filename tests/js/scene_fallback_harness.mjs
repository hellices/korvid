import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(
  new URL("../../docs/assets/javascripts/scene-fallback.js", import.meta.url),
  "utf8",
);

function run({ enhanced = false, readyState = "loading" } = {}) {
  const panels = [{ hidden: true }, { hidden: true }];
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
  },

  "enhanced scenes keep their controller-owned visibility"() {
    const result = run({ enhanced: true });
    result.fireLoad();
    assert.deepEqual(
      result.panels.map((panel) => panel.hidden),
      [true, true],
    );
  },

  "a late watchdog reveals scenes synchronously without a load listener"() {
    const result = run({ readyState: "complete" });
    assert.equal(result.hasLoadListener(), false);
    assert.deepEqual(
      result.panels.map((panel) => panel.hidden),
      [false, false],
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
