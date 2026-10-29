#!/usr/bin/env node
// Managed by VibePod — sends one herdr socket API request.
// Usage: herdr-report.js <method> <agent> [state] [sessionId] [sessionPath]
// Uses the socket at HERDR_SOCKET_PATH; without one, appends the
// pane.report_agent params as one JSON line to HERDR_EVENTS_FILE, which the
// host-side `vp run` relays to herdr.
// Exits non-zero on failure so calling hooks can log the reason.
"use strict";
const fs = require("fs");
const net = require("net");

const sockPath = process.env.HERDR_SOCKET_PATH;
const eventsFile = process.env.HERDR_EVENTS_FILE;
const pane = process.env.HERDR_PANE_ID;
const [method, agent, state, sessionId, sessionPath] = process.argv.slice(2);

if ((!sockPath && !eventsFile) || !pane || !method || !agent) {
  console.error("herdr-report: missing socket/events file/pane/method/agent");
  process.exit(2);
}

const params = { pane_id: pane, source: "vibepod", agent, display_agent: `vp:${agent}` };
if (state) params.state = state;
if (sessionId) params.agent_session_id = sessionId;
if (sessionPath) params.agent_session_path = sessionPath;
const request = { id: `vibepod:${process.pid}:${Date.now()}`, method, params };

if (!sockPath) {
  // The relay forwards state reports only; session paths are host-meaningless.
  if (method !== "pane.report_agent") process.exit(0);
  delete params.agent_session_path;
  const line = Buffer.from(JSON.stringify(params) + "\n");
  // one write under 4 KB on an O_APPEND fd: concurrent hooks never interleave
  if (line.length >= 4096) {
    console.error("herdr-report: event too large for the events file");
    process.exit(7);
  }
  try {
    const fd = fs.openSync(eventsFile, "a");
    try {
      fs.writeSync(fd, line);
    } finally {
      fs.closeSync(fd);
    }
  } catch (err) {
    console.error(`herdr-report: ${err.message || String(err)}`);
    process.exit(8);
  }
  process.exit(0);
}

let finished = false;
const finish = (code, message) => {
  if (finished) return;
  finished = true;
  if (message) console.error(`herdr-report: ${message}`);
  process.exit(code);
};

const sock = net.connect(sockPath);
sock.setTimeout(3000);
sock.on("timeout", () => finish(3, "timeout waiting for herdr"));
sock.on("error", (err) => finish(4, err.message || String(err)));
sock.on("connect", () => sock.write(JSON.stringify(request) + "\n"));
let buffer = "";
sock.on("data", (chunk) => {
  buffer += chunk.toString();
  const newline = buffer.indexOf("\n");
  if (newline === -1) return;
  const line = buffer.slice(0, newline);
  try {
    const reply = JSON.parse(line);
    if (reply.error) return finish(5, JSON.stringify(reply.error));
  } catch {
    // non-JSON reply — treat receipt as success
  }
  finish(0);
});
sock.on("close", () => finish(6, "herdr closed the connection without a reply"));
