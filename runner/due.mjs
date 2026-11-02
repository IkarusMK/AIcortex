#!/usr/bin/env node
// Ask the AICortex connector which cron jobs are due — without any LLM.
// Prints cron_due's answer on stdout: a JSON array, "[]" when nothing is due.
// Env: CONNECTOR_URL (…/mcp) and RUNNER_TOKEN (the token set on the connector).
const url = process.env.CONNECTOR_URL;
const token = process.env.RUNNER_TOKEN;
if (!url || !token) {
  console.error("CONNECTOR_URL and RUNNER_TOKEN are required");
  process.exit(2);
}

const PROTOCOL = "2025-06-18";
let session = "";

function headers() {
  const h = {
    "Content-Type": "application/json",
    Accept: "application/json, text/event-stream",
    Authorization: `Bearer ${token}`,
    "MCP-Protocol-Version": PROTOCOL,
  };
  if (session) h["Mcp-Session-Id"] = session;
  return h;
}

// One JSON-RPC message; returns the reply's result (none for a notification).
async function rpc(body) {
  const res = await fetch(url, {
    method: "POST", headers: headers(), body: JSON.stringify(body),
    signal: AbortSignal.timeout(30000),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  session = res.headers.get("mcp-session-id") || session;
  if (body.id === undefined) return null;
  const text = await res.text();
  const messages = (res.headers.get("content-type") || "").includes("text/event-stream")
    ? text.split(/\r?\n/).filter((l) => l.startsWith("data:")).map((l) => JSON.parse(l.slice(5)))
    : [JSON.parse(text)];
  const reply = messages.find((m) => m.id === body.id);
  if (!reply) throw new Error("no reply from the connector");
  if (reply.error) throw new Error(reply.error.message || "connector error");
  return reply.result;
}

try {
  await rpc({ jsonrpc: "2.0", id: 1, method: "initialize", params: {
    protocolVersion: PROTOCOL, capabilities: {},
    clientInfo: { name: "aicortex-runner", version: "1" } } });
  await rpc({ jsonrpc: "2.0", method: "notifications/initialized" });
  const result = await rpc({ jsonrpc: "2.0", id: 2, method: "tools/call",
    params: { name: "cron_due", arguments: {} } });
  const text = (result.content || []).map((c) => c.text || "").join("").trim();
  process.stdout.write(`${text}\n`);
} catch (err) {
  console.error(`cron_due failed: ${err.message}`);
  process.exitCode = 1;
} finally {
  if (session) {
    await fetch(url, { method: "DELETE", headers: headers() }).catch(() => {});
  }
}
