// Managed by VibePod — reports OpenCode events to herdr via the socket API.
import net from "node:net";

const sockPath = process.env.HERDR_SOCKET_PATH;
const pane = process.env.HERDR_PANE_ID;

const report = (state) =>
  new Promise((resolve) => {
    if (!sockPath || !pane) return resolve();
    const request = {
      id: `vibepod:${process.pid}:${Date.now()}`,
      method: "pane.report_agent",
      params: { pane_id: pane, source: "vibepod", agent: "opencode", display_agent: "vp:opencode", state },
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
  if (!sockPath || !pane) return {};
  return {
    event: async ({ event }) => {
      const type = event?.type ?? "";
      if (type === "session.idle") await report("idle");
      else if (type === "permission.updated") await report("blocked");
      else if (type.startsWith("message.")) await report("working");
    },
  };
};
