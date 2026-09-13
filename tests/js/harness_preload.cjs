const { writeSync } = require("node:fs");

const started = Symbol.for("korvid.harness.started");
globalThis[started] = process.hrtime.bigint();
writeSync(2, "korvid-harness stage=node-started elapsed-ms=0\n");
