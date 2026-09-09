import { writeSync } from "node:fs";

writeSync(1, "contract stdout\n");
writeSync(2, "contract stderr\n");
process.exitCode = 7;
