import { spawn } from "node:child_process";
import { writeFileSync, writeSync } from "node:fs";

const pidFile = process.env.KORVID_HARNESS_DESCENDANT_PID_FILE;
if (!pidFile) throw new Error("KORVID_HARNESS_DESCENDANT_PID_FILE is required");

const descendant = spawn(
  process.execPath,
  ["--eval", "setInterval(() => {}, 1000)"],
  {
    detached: true,
    stdio: ["ignore", 1, 2],
    windowsHide: true,
  },
);
descendant.unref();
writeFileSync(pidFile, String(descendant.pid));
writeSync(2, "parent-complete\n");
