// Managed by VibePod — reports Pi coding-agent events to herdr via the socket
// API, or, without a mounted socket, as JSON lines in HERDR_EVENTS_FILE that
// the host-side `vp run` relays to herdr.
import fs from "node:fs";
import net from "node:net";

const sockPath = process.env.HERDR_SOCKET_PATH;
const eventsFile = process.env.HERDR_EVENTS_FILE;
const pane = process.env.HERDR_PANE_ID;

function report(state: string): void {
  if (!pane) return;
  const params = { pane_id: pane, source: "vibepod", agent: "pi", display_agent: "vp:pi", state };
  try {
    if (!sockPath) {
      // one small write on an O_APPEND fd: concurrent reporters never interleave
      if (eventsFile) fs.appendFileSync(eventsFile, JSON.stringify(params) + "\n");
      return;
    }
    const request = {
      id: `vibepod:${process.pid}:${Date.now()}`,
      method: "pane.report_agent",
      params,
    };
    const sock = net.connect(sockPath);
    const done = () => sock.destroy();
    sock.setTimeout(3000, done);
    sock.on("error", done);
    sock.on("connect", () => sock.write(JSON.stringify(request) + "\n"));
    sock.on("data", done);
  } catch {
    // herdr unreachable — never disturb the agent
  }
}

export default function herdrAgentState(pi: {
  on: (event: string, handler: (...args: unknown[]) => void) => void;
}): void {
  pi.on("agent_start", () => report("working"));
  pi.on("turn_start", () => report("working"));
  pi.on("agent_end", () => report("idle"));
  pi.on("turn_end", () => report("idle"));
}
