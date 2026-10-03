// Managed by VibePod — reports OpenCode events to herdr via the socket API,
// or, without a mounted socket, as JSON lines in HERDR_EVENTS_FILE that the
// host-side `vp run` relays to herdr.
import fs from "node:fs";
import net from "node:net";

const sockPath = process.env.HERDR_SOCKET_PATH;
const eventsFile = process.env.HERDR_EVENTS_FILE;
const pane = process.env.HERDR_PANE_ID;

const appendEvent = (params) => {
  try {
    // one small write on an O_APPEND fd: concurrent reporters never interleave
    fs.appendFileSync(eventsFile, JSON.stringify(params) + "\n");
  } catch {
    // events file unwritable — never disturb the agent
  }
};

const report = (state) =>
  new Promise((resolve) => {
    if (!pane) return resolve();
    const params = { pane_id: pane, source: "vibepod", agent: "opencode", display_agent: "vp:opencode", state };
    if (!sockPath) {
      if (eventsFile) appendEvent(params);
      return resolve();
    }
    const request = {
      id: `vibepod:${process.pid}:${Date.now()}`,
      method: "pane.report_agent",
      params,
    };
    const sock = net.connect(sockPath);
    const done = () => {
      sock.destroy();
      resolve();
    };
    sock.setTimeout(3000, done);
    sock.on("error", done);
    sock.on("connect", () => sock.write(JSON.stringify(request) + "\n"));
    sock.on("data", done);
    sock.on("close", done);
  });

export const HerdrAgentState = async () => {
  if ((!sockPath && !eventsFile) || !pane) return {};
  return {
    event: async ({ event }) => {
      const type = event?.type ?? "";
      if (type === "session.idle") await report("idle");
      else if (type === "permission.updated") await report("blocked");
      else if (type.startsWith("message.")) await report("working");
    },
  };
};
