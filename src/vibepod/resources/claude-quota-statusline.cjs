// Passive Claude Code status-line adapter. Only numeric quotas are exported.
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const { spawn } = require("node:child_process");
const root = path.join(
  process.env.CLAUDE_CONFIG_DIR || "/claude",
  "vibepod-usage",
);
let input = "",
  oversized = false;
process.stdin.on("data", (chunk) => {
  if (input.length + chunk.length > 1048576) oversized = true;
  if (!oversized) input += chunk.toString("utf8");
});
function atomic(file, value) {
  const temporary = `${file}.${process.pid}.${crypto.randomBytes(4).toString("hex")}`;
  try {
    fs.writeFileSync(temporary, JSON.stringify(value), {
      mode: 0o600,
      flag: "wx",
    });
    fs.renameSync(temporary, file);
  } finally {
    try {
      fs.unlinkSync(temporary);
    } catch {}
  }
}
function valid(value, max) {
  return (
    typeof value === "number" &&
    Number.isFinite(value) &&
    value >= 0 &&
    value <= max
  );
}
process.stdin.on("end", () => {
  if (oversized) return;
  let limits = {};
  try {
    const data = JSON.parse(input);
    for (const key of ["five_hour", "seven_day"]) {
      const window = data.rate_limits?.[key];
      if (!window || !valid(window.used_percentage, 100)) continue;
      limits[key] = { used_percentage: window.used_percentage };
      if (valid(window.resets_at, 253402300799))
        limits[key].resets_at = window.resets_at;
    }
    if (Object.keys(limits).length) {
      fs.mkdirSync(root, { recursive: true, mode: 0o700 });
      // Idle redraws and refreshInterval timers must not make cached quotas fresh.
      const counters = [
        data.cost?.total_api_duration_ms,
        data.context_window?.total_input_tokens,
        data.context_window?.total_output_tokens,
      ].map((value) => (valid(value, 1e15) ? value : null));
      const fingerprint = crypto
        .createHash("sha256")
        .update(JSON.stringify([limits, counters]))
        .digest("hex");
      const session = crypto
        .createHash("sha256")
        .update(String(data.session_id || "default"))
        .digest("hex");
      const stateFile = path.join(root, `state-${session}.json`);
      let previous;
      try {
        previous = JSON.parse(fs.readFileSync(stateFile, "utf8"));
      } catch {}
      if (previous?.fingerprint !== fingerprint) {
        const observed_at = Date.now() / 1000;
        atomic(path.join(root, "snapshot.json"), {
          observed_at,
          rate_limits: limits,
        });
        atomic(stateFile, { fingerprint });
      }
    }
  } catch {} // Usage collection must never interrupt the status line.
  let original;
  try {
    original = JSON.parse(
      fs.readFileSync(path.join(root, "original.json"), "utf8"),
    );
  } catch {}
  if (typeof original?.command === "string" && original.command.trim()) {
    const child = spawn(original.command, {
      shell: true,
      stdio: ["pipe", "inherit", "ignore"],
    });
    child.on("error", () => {});
    child.stdin.on("error", () => {});
    child.stdin.end(input);
  } else {
    const text = [
      ["five_hour", "5h"],
      ["seven_day", "Weekly"],
    ]
      .filter(([key]) => limits[key])
      .map(
        ([key, label]) =>
          `${label}: ${Math.round(100 - limits[key].used_percentage)}% left`,
      );
    process.stdout.write(
      text.length ? text.join(" · ") + "\n" : "Usage not yet available\n",
    );
  }
});
