#!/usr/bin/env bun
/** Authenticated ntfy approval relay and live control panel for Codex app-server. */

import { approvalResponse, proposedAmendment } from "./codex-ntfy-decisions";

type Json = Record<string, unknown>;
type Pending = {
  id: string; rpcId: unknown; kind: "command" | "file"; nonce: string;
  threadId: string; turnId: string; command: string; cwd: string;
  reason: string; createdAt: number; expiresAt: number | null; resolved: boolean;
  proposedAmendment: string[] | null;
};
type History = { id: string; kind: string; command: string; status: string; at: number; detail?: string };

const env = (name: string, fallback = "") => Bun.env[name]?.trim() || fallback;
const CODEX_BIN = env("CODEX_NTFY_CODEX_BIN", "codex-android");
const CODEX_ARGS = env("CODEX_NTFY_CODEX_ARGS", "app-server --stdio").split(/\s+/);
const NTFY_URL = env("NTFY_URL", "http://localhost:8086").replace(/\/$/, "");
const NTFY_TOPIC = env("NTFY_TOPIC", "codex");
const NTFY_TOKEN = env("CODEX_NTFY_PUBLISH_TOKEN", env("NTFY_OPENCODE_TOKEN", env("NTFY_TOKEN")));
const HOOK_TOKEN = env("CODEX_NTFY_HOOK_TOKEN");
const HOOK_PORT = Number(env("CODEX_NTFY_HOOK_PORT", "10009"));
const TTL_MS = Number(env("CODEX_NTFY_APPROVAL_TTL_MS", "0"));
// Persisting a rule writes <CODEX_HOME>/rules/default.rules, which requires a
// Codex build carrying the Android execpolicy patch. Set to 0 to keep the relay
// strictly in-memory (the pre-patch behaviour).
const PERSIST_RULES = env("CODEX_NTFY_PERSIST_RULES", "1") !== "0";
function detectLanHost(): string {
  const explicit = env("CODEX_NTFY_CALLBACK_HOST");
  if (explicit) return explicit;
  for (const command of [["ip", "route", "get", "1.1.1.1"], ["hostname", "-I"]]) {
    try {
      const result = Bun.spawnSync(command);
      const output = new TextDecoder().decode(result.stdout);
      const source = output.match(/\bsrc\s+(\d{1,3}(?:\.\d{1,3}){3})/)?.[1];
      if (source && !source.startsWith("127.") && !source.startsWith("169.254.")) return source;
      const candidate = output.match(/\b(192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b/)?.[1];
      if (candidate) return candidate;
    } catch { /* try the next Android-compatible resolver */ }
  }
  return "";
}
const CALLBACK_HOST = detectLanHost();
const PANEL_TOKEN = `${crypto.randomUUID()}-${crypto.randomUUID()}`;
const PANEL_URL = `http://${CALLBACK_HOST}:${HOOK_PORT}/panel/${PANEL_TOKEN}`;

if (!NTFY_TOKEN || !HOOK_TOKEN) {
  console.error("codex-ntfy-relay: faltan CODEX_NTFY_PUBLISH_TOKEN y/o CODEX_NTFY_HOOK_TOKEN");
  process.exit(2);
}
if (!CALLBACK_HOST) {
  console.error("codex-ntfy-relay: no pude detectar una IP LAN; define CODEX_NTFY_CALLBACK_HOST");
  process.exit(2);
}
if (!Number.isInteger(HOOK_PORT) || HOOK_PORT < 1 || HOOK_PORT > 65535 || !Number.isFinite(TTL_MS) || TTL_MS < 0) {
  console.error("codex-ntfy-relay: HOOK_PORT o APPROVAL_TTL_MS inválido");
  process.exit(2);
}

const pending = new Map<string, Pending>();
const history: History[] = [];
const sessionRules: Array<{ prefix: string; createdAt: number }> = [];
const internalRpc = new Set<string>();
let nextRpc = 900000;
let nextAction = 0;
let child: ReturnType<typeof Bun.spawn> | undefined;

const keyOf = (value: unknown) => typeof value === "string" ? value : JSON.stringify(value);
const text = (value: unknown, fallback = "") => typeof value === "string" ? value : fallback;
const clip = (value: string, max = 1400) => value.length > max ? `${value.slice(0, max - 1)}…` : value;
const jsonLine = (value: unknown) => `${JSON.stringify(value)}\n`;
const esc = (value: string) => value.replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);

function addHistory(item: History) {
  history.unshift(item);
  if (history.length > 80) history.pop();
}
function authOk(request: Request) { return request.headers.get("authorization") === `Bearer ${HOOK_TOKEN}`; }
function panelOk(path: string) { return path === `/panel/${PANEL_TOKEN}` || path.startsWith(`/panel/${PANEL_TOKEN}/`); }
function actionBody(rpcId: unknown, action: string, nonce: string) { return JSON.stringify({ id: rpcId, action, nonce }); }
function ntfyAction(label: string, rpcId: unknown, actionName: string, nonce: string) {
  return {
    action: "http", label, method: "POST", url: `http://${CALLBACK_HOST}:${HOOK_PORT}/approve`,
    headers: { Authorization: `Bearer ${HOOK_TOKEN}`, "Content-Type": "application/json" },
    body: actionBody(rpcId, actionName, nonce), clear: actionName !== "ack",
  };
}
async function publish(title: string, message: string, actions: unknown[], tags = "lock") {
  const response = await fetch(`${NTFY_URL}/${encodeURIComponent(NTFY_TOPIC)}`, {
    method: "POST", headers: { Authorization: `Bearer ${NTFY_TOKEN}`, "Content-Type": "application/json" },
    body: JSON.stringify({ title, message: clip(message), priority: "urgent", tags, actions }),
  });
  if (!response.ok) throw new Error(`ntfy HTTP ${response.status}`);
}
function writeToChild(value: Json) {
  if (!child?.stdin) throw new Error("codex app-server no está conectado");
  child.stdin.write(jsonLine(value));
}
function sendChildRequest(method: string, params: Json): number {
  const id = nextRpc++;
  internalRpc.add(String(id));
  writeToChild({ jsonrpc: "2.0", id, method, params });
  return id;
}
function responseFor(p: Pending, actionName: string): Json {
  return approvalResponse(p.rpcId, actionName, p.proposedAmendment);
}
function matchingRule(command: string) { return sessionRules.find(rule => command.startsWith(rule.prefix)); }
function steer(p: Pending, message: string) {
  if (!message.trim()) return;
  try {
    sendChildRequest("turn/steer", {
      threadId: p.threadId, expectedTurnId: p.turnId,
      input: [{ type: "text", text: message.trim() }],
    });
  } catch (error) { addHistory({ id: p.id, kind: p.kind, command: p.command, status: "steer-error", at: Date.now(), detail: String(error) }); }
}
function resolve(p: Pending, actionName: string, detail = "") {
  if (p.resolved) return;
  p.resolved = true; pending.delete(p.id);
  writeToChild(responseFor(p, actionName));
  addHistory({ id: p.id, kind: p.kind, command: p.command, status: actionName, at: Date.now(), detail });
  return p;
}

async function handleApproval(message: Json): Promise<boolean> {
  const method = text(message.method);
  const params = (message.params || {}) as Json;
  const isCommand = method === "item/commandExecution/requestApproval";
  const isFile = method === "item/fileChange/requestApproval";
  if (!isCommand && !isFile) return false;
  const rpcId = message.id;
  const id = keyOf(rpcId);
  const command = text(params.command, isFile ? "Se solicita aplicar cambios de archivos" : "Comando no disponible");
  const threadId = text(params.threadId); const turnId = text(params.turnId);
  const cwd = text(params.cwd); const reason = text(params.reason);
  const rule = isCommand && matchingRule(command);
  if (rule) {
    try { writeToChild({ jsonrpc: "2.0", id: rpcId, result: { decision: "accept" } }); addHistory({ id, kind: "command", command, status: "auto-accepted", at: Date.now(), detail: rule.prefix }); }
    catch (error) { console.error(`codex-ntfy-relay: auto-accept falló: ${String(error)}`); }
    return true;
  }
  const nonce = `${Date.now().toString(36)}-${++nextAction}-${crypto.randomUUID()}`;
  const p: Pending = { id, rpcId, kind: isCommand ? "command" : "file", nonce, threadId, turnId, command, cwd, reason, createdAt: Date.now(), expiresAt: TTL_MS > 0 ? Date.now() + TTL_MS : null, resolved: false, proposedAmendment: isCommand ? proposedAmendment(params) : null };
  pending.set(id, p); addHistory({ id, kind: p.kind, command, status: "pending", at: p.createdAt, detail: reason });
  const messageText = `${reason ? `${reason}\n` : ""}${cwd ? `cwd: ${cwd}\n` : ""}${command}`;
  const buttons = [ntfyAction("Aceptar", rpcId, "allow", nonce), ntfyAction("Denegar", rpcId, "deny", nonce), { action: "view", label: "⋯", url: PANEL_URL, clear: false }];
  try { await publish(isFile ? "Codex: cambio requiere aprobación" : "Codex: comando requiere aprobación", messageText, buttons, isFile ? "pencil" : "warning,lock"); }
  catch (error) { pending.delete(id); addHistory({ id, kind: p.kind, command, status: "ntfy-error", at: Date.now(), detail: String(error) }); writeToChild({ jsonrpc: "2.0", id: rpcId, error: { code: -32090, message: `ntfy no disponible: ${String(error)}` } }); }
  return true;
}

async function approve(request: Request): Promise<Response> {
  if (!authOk(request)) return Response.json({ ok: false, reason: "unauthorized" }, { status: 401 });
  let body: Json; try { body = await request.json() as Json; } catch { return Response.json({ ok: false, reason: "invalid_json" }, { status: 400 }); }
  const p = pending.get(keyOf(body.id));
  if (!p || p.resolved || body.nonce !== p.nonce || (p.expiresAt !== null && Date.now() > p.expiresAt)) return Response.json({ ok: false, reason: "unknown_or_expired" });
  const choice = text(body.action); if (!["allow", "deny"].includes(choice)) return Response.json({ ok: false, reason: "invalid_action" }, { status: 400 });
  try { resolve(p, choice); return Response.json({ ok: true }); } catch (error) { return Response.json({ ok: false, reason: String(error) }, { status: 503 }); }
}

async function panelAction(request: Request, p: Pending): Promise<Response> {
  let body: Json; try { body = await request.json() as Json; } catch { return Response.json({ ok: false, reason: "invalid_json" }, { status: 400 }); }
  const actionName = text(body.action);
  if (!["allow", "deny", "session", "pattern", "persist", "deny-and-steer"].includes(actionName)) return Response.json({ ok: false, reason: "invalid_action" }, { status: 400 });
  if (actionName === "persist" && !PERSIST_RULES) return Response.json({ ok: false, reason: "persist_disabled" }, { status: 403 });
  if (actionName === "persist" && !p.proposedAmendment) return Response.json({ ok: false, reason: "no_proposed_amendment" }, { status: 400 });
  try {
    if (actionName === "pattern") {
      const prefix = text(body.prefix).trim();
      if (!prefix || !p.command.startsWith(prefix)) return Response.json({ ok: false, reason: "prefix_must_match_command" }, { status: 400 });
      if (!sessionRules.some(rule => rule.prefix === prefix)) sessionRules.push({ prefix, createdAt: Date.now() });
      resolve(p, "session", `prefijo: ${prefix}`);
    } else {
      const decision = actionName === "persist" ? "persist" : actionName === "session" ? "session" : actionName === "allow" ? "allow" : "deny";
      const detail = actionName === "persist" ? `regla: ${JSON.stringify(p.proposedAmendment)}` : text(body.reason);
      const resolved = resolve(p, decision, detail);
      if (actionName === "deny-and-steer" && resolved) setTimeout(() => steer(resolved, text(body.reason)), 100);
      else if (actionName === "deny" && text(body.reason)) setTimeout(() => steer(p, `El usuario denegó la acción. Motivo: ${text(body.reason)}`), 100);
    }
    return Response.json({ ok: true });
  } catch (error) { return Response.json({ ok: false, reason: String(error) }, { status: 503 }); }
}

function panelHtml() {
  return `<!doctype html><meta name="viewport" content="width=device-width"><title>Codex ntfy</title>
<style>body{font:16px system-ui;max-width:900px;margin:2em auto;padding:0 1em;background:#111;color:#eee}article{border:1px solid #555;border-radius:8px;padding:1em;margin:1em 0}button,input,textarea{font:inherit;padding:.55em;margin:.2em;background:#222;color:#eee;border:1px solid #777;border-radius:5px}button{cursor:pointer}pre{white-space:pre-wrap;word-break:break-word}.muted{color:#aaa}</style>
<h1>Codex · aprobaciones</h1><p class="muted">Panel vivo. “Permitir siempre (regla)” manda la enmienda propuesta al app-server, que la escribe en <code>~/.codex/rules/default.rules</code>; el resto de opciones solo afectan a esta sesión del relay.</p><main id="app">Cargando…</main>
<script>
const base=location.pathname, esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function post(path,data){let r=await fetch(base+'/'+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});return r.json()}
async function act(id,action){let reason=document.querySelector('#reason-'+CSS.escape(id))?.value||'';let prefix=document.querySelector('#prefix-'+CSS.escape(id))?.value||'';let r=await post('action',{id,action,reason,prefix});if(!r.ok)alert(r.reason);render()}
function card(p){return '<article><b>'+esc(p.kind)+' · '+esc(p.id)+'</b><pre>'+esc(p.command)+'</pre><span class="muted">'+esc(p.cwd||'')+'</span><br><textarea id="reason-'+esc(p.id)+'" rows="2" cols="50" placeholder="Motivo o mensaje para steer"></textarea><br><button onclick="act(\''+esc(p.id)+'\',\'allow\')">Aceptar</button><button onclick="act(\''+esc(p.id)+'\',\'deny\')">Denegar</button><button onclick="act(\''+esc(p.id)+'\',\'deny-and-steer\')">Denegar + enviar motivo</button><br><input id="prefix-'+esc(p.id)+'" placeholder="Prefijo exacto del comando"><button onclick="act(\''+esc(p.id)+'\',\'pattern\')">Permitir este prefijo</button><button onclick="act(\''+esc(p.id)+'\',\'session\')">Permitir sesión</button>'+(p.proposedAmendment?'<br><span class="muted">regla propuesta: '+esc(JSON.stringify(p.proposedAmendment))+'</span><button onclick="act(\''+esc(p.id)+'\',\'persist\')">Permitir siempre (regla)</button>':'')+'</article>'}
async function render(){let s=await (await fetch(base+'/state')).json();document.querySelector('#app').innerHTML=(s.pending.length?s.pending.map(card).join(''):'<p>No hay solicitudes pendientes.</p>')+'<h2>Historial</h2>'+s.history.map(x=>'<article><b>'+esc(x.status)+'</b> · '+esc(x.kind)+'<pre>'+esc(x.command)+'</pre></article>').join('');}
render();setInterval(render,2000);
</script>`;
}

const server = Bun.serve({ hostname: "0.0.0.0", port: HOOK_PORT, async fetch(request) {
  const url = new URL(request.url);
  if (url.pathname === "/healthz" && request.method === "GET") return new Response("ok");
  if (url.pathname === "/approve" && request.method === "POST") return approve(request);
  if (!panelOk(url.pathname)) return Response.json({ ok: false, reason: "not_found" }, { status: 404 });
  if (url.pathname === `/panel/${PANEL_TOKEN}` && request.method === "GET") return new Response(panelHtml(), { headers: { "content-type": "text/html; charset=utf-8" } });
  if (url.pathname === `/panel/${PANEL_TOKEN}/state` && request.method === "GET") return Response.json({ pending: [...pending.values()].map(p => ({ id: p.id, kind: p.kind, command: p.command, cwd: p.cwd, reason: p.reason, createdAt: p.createdAt, proposedAmendment: p.proposedAmendment })), history, rules: sessionRules, persistRules: PERSIST_RULES });
  if (url.pathname === `/panel/${PANEL_TOKEN}/action` && request.method === "POST") {
    let body: Json; try { body = await request.clone().json() as Json; } catch { return Response.json({ ok: false, reason: "invalid_json" }, { status: 400 }); }
    const p = pending.get(keyOf(body.id)); if (!p) return Response.json({ ok: false, reason: "unknown_or_resolved" });
    return panelAction(request, p);
  }
  return Response.json({ ok: false, reason: "not_found" }, { status: 404 });
}});

console.error(`codex-ntfy-relay: panel vivo ${PANEL_URL}`);
console.error(`codex-ntfy-relay: "permitir siempre (regla)" persiste en default.rules; prefijos de sesión solo en memoria (persistRules=${PERSIST_RULES})`);
child = Bun.spawn([CODEX_BIN, ...CODEX_ARGS], { stdin: "pipe", stdout: "pipe", stderr: "inherit" });

async function readLines(stream: ReadableStream<Uint8Array>, onLine: (line: string) => Promise<void>) {
  const reader = stream.getReader(); const decoder = new TextDecoder(); let buffer = "";
  while (true) { const part = await reader.read(); if (part.done) break; buffer += decoder.decode(part.value, { stream: true }); const lines = buffer.split("\n"); buffer = lines.pop() || ""; for (const line of lines) if (line.trim()) await onLine(line); }
  if (buffer.trim()) await onLine(buffer);
}
const childOutput = readLines(child.stdout, async line => {
  let message: Json; try { message = JSON.parse(line); } catch { console.error(`codex-ntfy-relay: línea inválida: ${clip(line, 300)}`); return; }
  if (message.id !== undefined && internalRpc.delete(String(message.id))) return;
  if (!(await handleApproval(message))) process.stdout.write(jsonLine(message));
});
const clientInput = readLines(Bun.stdin.stream(), async line => { try { writeToChild(JSON.parse(line)); } catch (error) { console.error(`codex-ntfy-relay: entrada inválida: ${String(error)}`); } });
await Promise.race([childOutput, clientInput]);
for (const item of pending.values()) item.resolved = true; pending.clear(); server.stop(true); child.kill();
