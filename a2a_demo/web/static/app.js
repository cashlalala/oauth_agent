// Plain JS, no build step. Talks only to this web app's /api and /auth routes (never to the agent).
const $ = (id) => document.getElementById(id);
let contextId = null;   // A2A conversation (the agent keeps the memory per contextId)
let taskId = null;      // current task, for Stop
let busy = false;

function show(el, on) { el.hidden = !on; }

function addMessage(cls, text) {
  const div = document.createElement("div");
  div.className = "msg " + cls;
  div.textContent = text;           // textContent, never innerHTML: model output is untrusted
  $("messages").appendChild(div);
  $("messages").scrollTop = $("messages").scrollHeight;
  return div;
}

function logWire(obj) {
  if ($("wire").hidden) return;
  $("wire-log").textContent += JSON.stringify(obj, null, 2) + "\n";
  $("wire").scrollTop = $("wire").scrollHeight;
}

async function refresh() {
  let me;
  try {
    const r = await fetch("/api/me");
    me = await r.json();
    if (!r.ok) throw new Error(me.message || me.error);
  } catch (e) {
    show($("gate"), true); show($("chat"), false);
    $("gate-error").textContent = "Cannot reach the agent: " + e.message;
    $("gate-login").hidden = true;
    return;
  }
  $("agent-name").textContent = me.agent.name;
  $("agent-desc").textContent = me.agent.description;
  const loggedIn = !me.loginRequired || me.user;
  $("user").textContent = me.user ? (me.user.name || me.user.sub) : "";
  show($("login-btn"), me.loginRequired && !me.user);
  show($("logout-btn"), !!me.user);
  show($("new-btn"), loggedIn);
  show($("gate"), !loggedIn);
  show($("chat"), loggedIn);
  $("gate-login").hidden = false;
}

async function send(text) {
  busy = true; show($("send"), false); show($("stop"), true);
  addMessage("user", text);
  const reply = addMessage("agent pending", "");
  let resp;
  try {
    resp = await fetch("/api/chat", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, contextId }),
    });
    if (resp.status === 401) { reply.remove(); await refresh(); return; }   // session expired -> login gate
    if (!resp.ok) throw new Error((await resp.json()).message || resp.statusText);
    await readEvents(resp, reply);
  } catch (e) {
    reply.className = "msg error"; reply.textContent = reply.textContent || String(e.message || e);
  } finally {
    reply.classList.remove("pending");
    busy = false; taskId = null; show($("send"), true); show($("stop"), false);
    $("input").focus();
  }
}

// message/stream arrives as Server-Sent Events; fetch (not EventSource) because we POST.
async function readEvents(resp, reply) {
  const reader = resp.body.getReader(), dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const frame = buf.slice(0, i); buf = buf.slice(i + 2);
      const event = (frame.match(/^event: (.*)$/m) || [])[1];
      const data = frame.split("\n").filter((l) => l.startsWith("data:")).map((l) => l.slice(5).trim()).join("\n");
      if (!data) continue;
      const obj = JSON.parse(data);
      logWire(obj);
      if (event === "error") { reply.className = "msg error"; reply.textContent += "\n" + obj.error; continue; }
      handle(obj, reply);
    }
  }
}

function handle(ev, reply) {
  if (ev.contextId) contextId = ev.contextId;
  if (ev.kind === "task") taskId = ev.id;
  else if (ev.kind === "artifact-update") {
    reply.textContent += ev.artifact.parts.map((p) => p.text || "").join("");   // append=true chunks
    $("messages").scrollTop = $("messages").scrollHeight;
  } else if (ev.kind === "status-update") {
    const s = ev.status;
    if (s.state === "failed") { reply.className = "msg error"; reply.textContent = "Task failed: " + (s.message?.parts?.[0]?.text || ""); }
    else if (s.state === "canceled") reply.textContent += "\n[stopped]";
  }
}

$("form").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = $("input").value.trim();
  if (!text || busy) return;
  $("input").value = "";
  send(text);
});
$("input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("form").requestSubmit(); }  // isComposing: IME (中文) confirm
});
$("stop").addEventListener("click", () => {
  if (taskId) fetch("/api/cancel", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ taskId }) });
});
$("new-btn").addEventListener("click", () => { contextId = null; $("messages").replaceChildren(); $("wire-log").textContent = ""; });
$("wire-toggle").addEventListener("change", (e) => show($("wire"), e.target.checked));
const login = () => { location.href = "/auth/login"; };
$("login-btn").addEventListener("click", login);
$("gate-login").addEventListener("click", login);
$("logout-btn").addEventListener("click", async () => {
  await fetch("/auth/logout", { method: "POST" });
  contextId = null; $("messages").replaceChildren();
  await refresh();
});

// the callback redirects here with ?login_error=... when login failed
const err = new URLSearchParams(location.search).get("login_error");
if (err) history.replaceState(null, "", "/");
refresh().then(() => { if (err) $("gate-error").textContent = err; });
