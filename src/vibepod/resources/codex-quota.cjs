// Runs in the existing Codex container. No inference, auth-file reads, or error dumps.
const { spawn } = require("node:child_process");
const child = spawn("codex", ["app-server", "--listen", "stdio://"], {
  // The Docker exec caller already selected a neutral working directory.
  stdio: ["pipe", "pipe", "ignore"],
  detached: true,
});
let finished = false,
  input = "",
  bytes = 0;
const timer = setTimeout(() => finish(null), 18000);
function finish(result) {
  if (finished) return;
  finished = true;
  clearTimeout(timer);
  if (result) process.stdout.write(JSON.stringify(result) + "\n");
  process.exitCode = result ? 0 : 1;
  child.stdin.destroy();
  child.stdout.destroy();
  try {
    process.kill(-child.pid, "SIGTERM");
  } catch {}
  const kill = setTimeout(() => {
    try {
      process.kill(-child.pid, "SIGKILL");
    } catch {}
  }, 500);
  child.once("exit", () => clearTimeout(kill));
}
const send = (value) => {
  if (!finished) child.stdin.write(JSON.stringify(value) + "\n");
};
child.on("error", () => finish(null));
child.stdin.on("error", () => finish(null));
child.on("exit", () => finish(null));
child.stdout.on("data", (chunk) => {
  bytes += chunk.length;
  if (bytes > 1048576) return finish(null);
  input += chunk.toString("utf8");
  while (input.includes("\n") && !finished) {
    const end = input.indexOf("\n");
    const line = input.slice(0, end);
    input = input.slice(end + 1);
    let message;
    try {
      message = JSON.parse(line);
    } catch {
      continue;
    }
    if (message.id === 1) {
      if (message.error) return finish(null);
      send({ method: "initialized" });
      send({ method: "account/rateLimits/read", id: 2 });
    } else if (message.id === 2) {
      if (message.error || !message.result) return finish(null);
      const source = message.result.rateLimitsByLimitId || {
        codex: message.result.rateLimits,
      };
      const buckets = {};
      for (const [i, [name, bucket]] of Object.entries(source)
        .slice(0, 32)
        .entries()) {
        if (!bucket || typeof bucket !== "object") continue;
        const target = {};
        for (const key of ["primary", "secondary"]) {
          const window = bucket[key];
          if (!window || typeof window !== "object") continue;
          target[key] = {};
          for (const field of [
            "usedPercent",
            "windowDurationMins",
            "resetsAt",
          ]) {
            const value = window[field];
            if (typeof value === "number" && Number.isFinite(value))
              target[key][field] = value;
          }
        }
        buckets[name === "codex" ? "codex" : `limit-${i + 1}`] = target;
      }
      finish({ rateLimitsByLimitId: buckets });
    }
  }
});
send({
  method: "initialize",
  id: 1,
  params: {
    clientInfo: { name: "vibepod_usage", title: "VibePod Usage", version: "1" },
  },
});
