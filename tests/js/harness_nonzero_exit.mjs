import { writeSync } from "node:fs";

import { createHarnessLifecycle } from "./harness_lifecycle.mjs";

const { finish } = createHarnessLifecycle("harness-contract");
writeSync(1, "contract stdout\n");
writeSync(2, "contract stderr\n");
finish(7);
