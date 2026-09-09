import { writeSync } from "node:fs";

writeSync(
  1,
  `stdout-start\n${"x".repeat(10_000)}stdout-middle${"x".repeat(10_000)}stdout-tail`,
);
writeSync(
  2,
  `stderr-start\n${"y".repeat(10_000)}stderr-middle${"y".repeat(10_000)}stderr-tail`,
);
setInterval(() => {}, 1000);
