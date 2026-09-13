import { writeSync } from "node:fs";

const MAX_RESOURCE_TYPES = 6;
const MAX_RESOURCE_NAME = 48;
const STARTED = Symbol.for("korvid.harness.started");

function harnessStarted() {
  if (typeof globalThis[STARTED] !== "bigint") {
    globalThis[STARTED] = process.hrtime.bigint();
  }
  return globalThis[STARTED];
}

const started = harnessStarted();

function elapsedMilliseconds() {
  const elapsed = process.hrtime.bigint() - started;
  return elapsed < 0n ? 0n : elapsed / 1_000_000n;
}

function summarize(values) {
  const types = values
    .map((value) => {
      const name =
        typeof value === "string"
          ? value
          : (value?.constructor?.name ?? typeof value);
      return String(name).slice(0, MAX_RESOURCE_NAME);
    })
    .sort();
  return { count: types.length, types: types.slice(0, MAX_RESOURCE_TYPES) };
}

function resources() {
  const activeResources =
    typeof process.getActiveResourcesInfo === "function"
      ? process.getActiveResourcesInfo()
      : [];
  const activeHandles =
    typeof process._getActiveHandles === "function"
      ? process._getActiveHandles()
      : [];
  const activeRequests =
    typeof process._getActiveRequests === "function"
      ? process._getActiveRequests()
      : [];
  return [
    `active-resources=${JSON.stringify(summarize(activeResources))}`,
    `active-handles=${JSON.stringify(summarize(activeHandles))}`,
    `active-requests=${JSON.stringify(summarize(activeRequests))}`,
  ].join(" ");
}

export function createHarnessLifecycle(label) {
  let finished = false;
  const writeStage = (stage, detail = "") => {
    writeSync(
      2,
      `${label} stage=${stage}${detail} elapsed-ms=${elapsedMilliseconds()}\n`,
    );
  };

  process.once("beforeExit", (exitCode) => {
    if (finished) {
      writeStage("before-exit", ` exit-code=${exitCode} ${resources()}`);
    }
  });
  process.once("exit", (exitCode) => {
    if (finished) writeStage("exit", ` exit-code=${exitCode}`);
  });

  return {
    milestone(stage, scenario = null) {
      const detail =
        scenario === null ? "" : ` scenario=${JSON.stringify(scenario)}`;
      writeStage(stage, detail);
    },
    finish(exitCode) {
      finished = true;
      process.exitCode = exitCode;
      writeStage("complete", ` exit-code=${exitCode} ${resources()}`);
    },
  };
}
