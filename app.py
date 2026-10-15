"""Local coding-agent web app: Flask + SSE dashboard over the agent loop.

This module owns HTTP and session plumbing only. All harness behaviour (planning,
compaction, verification, permissions) is wired in one place — `hooks_wiring.
default_hooks()` — and the loop itself is in `agent.py`. Nothing in the request
handlers knows which hook implements which event.

Two call sites changed for the harness:

1. `_new_session` binds a session id into the hook set. `default_hooks(
   session_id=sid)` scopes compaction's state re-injection and the writes log to
   this session, so a session can never re-read another session's plan or writes.
   This is the fix for a stale plan leaking across sessions after compaction.

2. The posts get a 'compaction' progress event for free (the loop emits it) and
   the tool log surfaces hook blocks; both are rendered by `renderLive`.

Roots are grouped into named profiles (roots_profiles.ProfileStore). Applying a
profile is just a normal root change: it goes through `_root_manager.apply`, so
the same validation and the same container rebuild happen as when paths are
saved by hand. Profiles store snapshots; they never bypass validation.
"""

from __future__ import annotations

import atexit
import json
import queue
import threading
import uuid
from typing import Any

from flask import Flask, Response, jsonify, render_template_string, request

from agent import Agent
from config import (
    APP_DIR, HOST, IMAGE, MAX_HISTORY, MAX_ITERATIONS, MAX_TOOL_OUTPUT, PORT,
)
from hooks_wiring import default_hooks
from roots_profiles import ProfileError, ProfileStore
from sandbox import RootManager
from tools import build_registry

app = Flask(__name__)
_lock = threading.RLock()
_root_manager: RootManager | None = None
_profile_store: ProfileStore | None = None
_sessions: dict[str, dict[str, Any]] = {}
_bridges: list[Any] = []


def _emit(sess: dict[str, Any], event_type: str, data: dict[str, Any]) -> None:
    with _lock:
        if event_type == "user":
            sess["history"].append({"role": "user", "content": data.get("content", "")})
        elif event_type == "assistant":
            sess["history"].append({"role": "assistant", "content": data.get("content") or ""})
        elif event_type == "tool_call":
            sess["history"].append({"role": "tool_call", "name": data.get("name"),
                                    "arguments": data.get("arguments")})
        elif event_type == "tool_result":
            sess["history"].append({"role": "tool", "name": data.get("name"),
                                    "content": data.get("result", "")})
        sess["history"] = sess["history"][-MAX_HISTORY:]
        sess["queue"].put({"type": event_type, "data": data})


def _cleanup() -> None:
    if _root_manager is not None:
        _root_manager.cleanup()


atexit.register(_cleanup)


def _start_workspace() -> None:
    _root_manager.start()


def _invalidate_container_caches() -> None:
    """Mark every Excel bridge's engine as not-installed after a rebuild.

    A rebuilt container has no engine files. The bridge caches that install with
    `_loaded`; if the flag is not cleared, the next Excel call skips shipping the
    engine into the fresh container and every workbook operation fails.
    """
    with _lock:
        for bridge in _bridges:
            bridge._loaded = False


def _new_session() -> str:
    if _root_manager is None:
        raise RuntimeError("Root manager is not initialised")

    # Recover from a failed first start rather than rejecting sessions forever.
    if _root_manager.state != "ready":
        _root_manager.ensure_started()
    if _root_manager.state != "ready":
        raise RuntimeError(_root_manager.error or "Sandbox is not ready")

    sid = uuid.uuid4().hex[:12]
    sess: dict[str, Any] = {
        "id": sid,
        "history": [],
        "queue": queue.Queue(),
        "running": False,
        "agent": None,
    }

    def callback(event_type: str, data: dict[str, Any]) -> None:
        _emit(sess, event_type, data)

    # build_registry owns the sub-agent roster (it needs the finished tool
    # surface to inherit from) and installs `manage_subagent` itself. It returns
    # (tools, handlers, gate, subs, bridge). The bridge is retained here so the
    # post-rebuild invalidation hook has something to clear.
    #
    # The workspace passed here is the RootManager's stable proxy. Rebuilding the
    # container for a root change swaps the proxy's target in place, so this
    # session keeps working across the change without being recreated.
    tools, handlers, gate, subagents, bridge = build_registry(
        _root_manager.workspace, MAX_TOOL_OUTPUT,
        event_callback=callback, root_manager=_root_manager,
    )
    with _lock:
        _bridges.append(bridge)

    # Bind the session id so compaction's fidelity hook and the writes log only
    # ever see this session's state.
    hooks = default_hooks(gate=gate, session_id=sid)

    sess["agent"] = Agent(
        tools, handlers, callback, MAX_ITERATIONS,
        hooks=hooks, compact=True,
    )
    sess["subagents"] = subagents
    sess["bridge"] = bridge
    _sessions[sid] = sess
    return sid


def _run_agent(sid: str, message: str) -> None:
    sess = _sessions[sid]
    try:
        sess["agent"].run(message)
    except Exception as exc:
        _emit(sess, "error", {"message": str(exc)})
    finally:
        with _lock:
            sess["running"] = False
        _emit(sess, "done", {})


@app.get("/")
def index():
    return render_template_string(DASHBOARD)


@app.get("/api/status")
def status():
    return jsonify({
        "state": _root_manager.state if _root_manager else "starting",
        "error": _root_manager.error if _root_manager else None,
    })


@app.route("/api/roots", methods=["GET", "POST"])
def roots():
    if _root_manager is None:
        return jsonify({"error": "Root manager is not initialised"}), 503
    if request.method == "GET":
        return jsonify({
            "roots": _root_manager.config().to_json(),
            "state": _root_manager.state,
            "writable_file": str(_root_manager.writable_file),
            "readonly_file": str(_root_manager.readonly_file),
        })

    payload = request.get_json() or {}
    config = _root_manager.config()
    writable = payload.get("writable")
    readonly = payload.get("readonly")
    if writable is None:
        writable = [r.host_path for r in config.writable]
    if readonly is None:
        readonly = [r.host_path for r in config.readonly]

    try:
        updated = _root_manager.apply(list(writable), list(readonly))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"roots": updated.to_json(), "state": _root_manager.state})


@app.route("/api/roots/validate", methods=["POST"])
def validate_roots_endpoint():
    """Dry-run validation: check a proposed root set without rebuilding anything."""
    from sandbox import RootsConfigError, build_config

    payload = request.get_json() or {}
    try:
        build_config(list(payload.get("writable") or []),
                     list(payload.get("readonly") or []))
    except RootsConfigError as exc:
        return jsonify({"ok": False, "error": str(exc)})
    return jsonify({"ok": True})


# --- root profiles ---------------------------------------------------------
#
# A profile is a named snapshot of a {writable, readonly} path set. It is
# stored as JSON and is purely a convenience: applying one routes through the
# same /api/roots machinery, so validation and rebuild behaviour are identical
# to a manual save. Profiles cannot smuggle in an invalid root set.

@app.get("/api/roots/profiles")
def list_profiles():
    if _profile_store is None:
        return jsonify({"error": "Profile store is not initialised"}), 503
    return jsonify({
        "profiles": [p.to_json() for p in _profile_store.list()],
        "active": _root_manager.config().to_json() if _root_manager else None,
    })


@app.post("/api/roots/profiles")
def save_profile():
    """Create or overwrite a profile. Body: {name, writable[], readonly[]}.

    The proposed paths are validated first, so a profile can never be saved
    that would fail on apply. Nothing is rebuilt here.
    """
    from sandbox import RootsConfigError, build_config

    if _profile_store is None:
        return jsonify({"error": "Profile store is not initialised"}), 503

    payload = request.get_json() or {}
    name = (payload.get("name") or "").strip()
    writable = list(payload.get("writable") or [])
    readonly = list(payload.get("readonly") or [])

    try:
        build_config(writable, readonly)
    except RootsConfigError as exc:
        return jsonify({"error": f"Invalid roots: {exc}"}), 400

    try:
        profile = _profile_store.save(name, writable, readonly)
    except ProfileError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"profile": profile.to_json(),
                    "profiles": [p.to_json() for p in _profile_store.list()]})


@app.delete("/api/roots/profiles/<name>")
def delete_profile(name: str):
    if _profile_store is None:
        return jsonify({"error": "Profile store is not initialised"}), 503
    try:
        _profile_store.delete(name)
    except ProfileError as exc:
        return jsonify({"error": str(exc)}), 404
    return jsonify({"profiles": [p.to_json() for p in _profile_store.list()]})


@app.post("/api/roots/profiles/apply")
def apply_profile():
    """Apply a saved profile: rebuild the sandbox with the profile's roots.

    Body: {name}. This delegates to the same `apply()` used by a manual save.
    """
    if _root_manager is None or _profile_store is None:
        return jsonify({"error": "Root manager is not initialised"}), 503

    payload = request.get_json() or {}
    name = (payload.get("name") or "").strip()
    profile = _profile_store.get(name)
    if profile is None:
        return jsonify({"error": f"No such profile: {name}"}), 404

    try:
        updated = _root_manager.apply(list(profile.writable), list(profile.readonly))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({
        "roots": updated.to_json(),
        "state": _root_manager.state,
        "applied": profile.name,
    })


@app.route("/api/browse", methods=["GET"])
def browse():
    """List subdirectories of a host path, for the Roots tab's directory picker.

    Host-side and unrestricted by design: this serves the trusted local operator
    who is choosing which directories to expose, not the model. It is never
    reachable from inside the sandbox.
    """
    import os
    from pathlib import Path

    raw = (request.args.get("path") or "").strip()
    if not raw:
        current = str(Path.home())
    else:
        current = str(Path(os.path.expanduser(raw)))

    p = Path(current)
    if not p.exists() or not p.is_dir():
        return jsonify({"error": f"Not a directory: {current}"}), 400

    try:
        entries = sorted(
            (e for e in p.iterdir() if e.is_dir() and not e.name.startswith(".")),
            key=lambda e: e.name.lower(),
        )
    except PermissionError:
        return jsonify({"error": f"Permission denied: {current}"}), 403

    return jsonify({
        "path": str(p),
        "parent": str(p.parent) if p.parent != p else None,
        "directories": [{"name": e.name, "path": str(e)} for e in entries],
    })


@app.route("/api/sessions", methods=["GET", "POST"])
def sessions():
    if request.method == "POST":
        try:
            return jsonify({"session_id": _new_session()})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
    return jsonify([{"id": s["id"], "running": s["running"]} for s in _sessions.values()])


@app.get("/api/sessions/<sid>")
def get_session(sid: str):
    sess = _sessions.get(sid)
    if not sess:
        return jsonify({"error": "not found"}), 404
    return jsonify({"id": sid, "running": sess["running"]})


@app.get("/api/sessions/<sid>/messages")
def messages(sid: str):
    sess = _sessions.get(sid)
    if not sess:
        return jsonify({"error": "not found"}), 404
    return jsonify(sess["history"])


@app.post("/api/sessions/<sid>/chat")
def chat(sid: str):
    sess = _sessions.get(sid)
    if not sess:
        return jsonify({"error": "not found"}), 404
    if sess["running"]:
        return jsonify({"error": "agent already running"}), 409

    message = (request.get_json() or {}).get("message", "").strip()
    if not message:
        return jsonify({"error": "empty message"}), 400

    _emit(sess, "user", {"content": message})
    with _lock:
        sess["running"] = True

    threading.Thread(target=_run_agent, args=(sid, message), daemon=True).start()
    return jsonify({"status": "started"})


@app.post("/api/sessions/<sid>/cancel")
def cancel(sid: str):
    sess = _sessions.get(sid)
    if not sess:
        return jsonify({"error": "not found"}), 404
    sess["agent"].interrupt()
    return jsonify({"status": "cancelled"})


@app.get("/stream/<sid>")
def stream(sid: str):
    sess = _sessions.get(sid)
    if not sess:
        return "invalid session", 404

    def generate():
        while True:
            try:
                event = sess["queue"].get(timeout=30)
            except queue.Empty:
                yield ": keepalive\n\n"
                continue
            yield f"data: {json.dumps(event)}\n\n"
            if event["type"] == "done":
                break

    return Response(generate(), mimetype="text/event-stream")


DASHBOARD = r"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Local Coding Agent</title>
<style>
  *{box-sizing:border-box}html,body{height:100%;margin:0}
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
       display:flex;background:#f4f5f7;color:#1c1e21;font-size:13px}
  aside{width:240px;background:#1e293b;color:#e2e8f0;padding:12px;overflow-y:auto;
        flex-shrink:0;display:flex;flex-direction:column}
  aside h2{font-size:11px;margin:12px 0 6px;text-transform:uppercase;letter-spacing:1px;color:#94a3b8}
  aside h2:first-child{margin-top:0}
  aside button{width:100%;padding:7px;border-radius:4px;border:0;background:#3b82f6;
               color:white;cursor:pointer;margin-bottom:4px}
  aside button:hover{background:#2563eb}
  .session{padding:7px 9px;background:#334155;border-radius:4px;margin-bottom:5px;
           cursor:pointer;font-size:12px}
  .session:hover{background:#475569}
  .session.active{background:#2563eb}
  .session.running{border-left:3px solid #a855f7}
  .session .sid{color:#94a3b8;font-size:10px}
  main{flex:1;display:flex;flex-direction:column;overflow:hidden}
  #tabbar{display:flex;gap:4px;padding:8px 10px 0;background:white;
          border-bottom:1px solid #e2e8f0;flex-shrink:0}
  .tab-btn{padding:6px 12px;border:0;background:transparent;cursor:pointer;font-size:12px;
           color:#64748b;border-bottom:2px solid transparent}
  .tab-btn.active{color:#2563eb;border-bottom-color:#2563eb}
  #tab-body{flex:1;overflow:hidden;display:flex}
  .tab-content{display:none;flex:1;overflow-y:auto;padding:10px 14px;background:white}
  .tab-content.active{display:block}
  .tab-content.log{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}
  .msg{margin:6px 0;padding:6px 9px;border-radius:4px;white-space:pre-wrap;word-break:break-word}
  .user{background:#e3f2fd}
  .assistant{background:#f1f3f4}
  .tool_call{background:#fff7cc}
  .tool_result{background:#ecfdf5}
  .error{background:#fee2e2;color:#991b1b}
  #input-area{border-top:1px solid #e2e8f0;padding:8px 10px;display:flex;gap:6px;background:white;flex-shrink:0}
  #input-area textarea{flex:1;height:52px;padding:8px;border:1px solid #cbd5e1;
                       border-radius:4px;font-size:13px;resize:vertical;font-family:inherit}
  #input-area button{padding:8px 14px;border:0;border-radius:4px;cursor:pointer;font-size:12px}
  #send-btn{background:#2563eb;color:white}
  #cancel-btn{background:#ef4444;color:white;display:none}
  #send-btn:disabled,#input-area textarea:disabled{opacity:.5;cursor:not-allowed}
  #status{float:right;color:#6b7280}
  .empty{color:#94a3b8;font-style:italic;padding:20px;text-align:center}
  .root-group{margin-bottom:18px}
  .root-group h3{font-size:12px;text-transform:uppercase;letter-spacing:1px;color:#475569;margin:0 0 6px}
  .root-row{display:flex;align-items:center;gap:8px;padding:6px 8px;border:1px solid #e2e8f0;
            border-radius:4px;margin-bottom:5px;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}
  .root-row .sandbox{color:#64748b;font-size:11px;min-width:160px}
  .root-row .host{flex:1;word-break:break-all}
  .root-row button{border:0;background:#ef4444;color:white;border-radius:3px;
                   padding:3px 8px;cursor:pointer;font-size:11px}
  .root-add{display:flex;gap:6px;margin-top:6px}
  .root-add input{flex:1;padding:6px 8px;border:1px solid #cbd5e1;border-radius:4px;
                  font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}
  .root-add button{padding:6px 12px;border:0;border-radius:4px;background:#2563eb;color:white;
                   cursor:pointer;font-size:12px}
  #roots-error{color:#991b1b;background:#fee2e2;padding:8px 10px;border-radius:4px;
               margin-bottom:10px;display:none;white-space:pre-wrap}
  #roots-note{color:#475569;font-size:12px;margin-bottom:12px;line-height:1.5}
  #profile-bar{display:flex;align-items:center;gap:8px;margin-bottom:14px;padding:10px;
               border:1px solid #e2e8f0;border-radius:4px;background:#f8fafc;flex-wrap:wrap}
  #profile-bar label{font-size:12px;color:#475569;font-weight:600}
  #profile-select{min-width:220px;padding:6px 8px;border:1px solid #cbd5e1;border-radius:4px;
                  font-size:12px;background:white}
  #profile-name{min-width:180px;padding:6px 8px;border:1px solid #cbd5e1;border-radius:4px;
                font-size:12px;background:white;font-family:ui-monospace,Menlo,Consolas,monospace}
  #profile-bar button{padding:6px 12px;border:0;border-radius:4px;cursor:pointer;font-size:12px}
  #profile-apply{background:#2563eb;color:white}
  #profile-save{background:#0891b2;color:white}
  #profile-new{background:#16a34a;color:white}
  #profile-delete{background:#ef4444;color:white}
  #profile-bar button:disabled{opacity:.45;cursor:not-allowed}
  .picker{border:1px solid #cbd5e1;border-radius:4px;margin-top:6px;max-height:240px;
          overflow-y:auto;display:none;background:white}
  .picker .row{padding:6px 10px;cursor:pointer;font-family:ui-monospace,Menlo,Consolas,monospace;
               font-size:12px;border-bottom:1px solid #f1f5f9}
  .picker .row:hover{background:#eff6ff}
  .picker .row.up{color:#2563eb;font-weight:600}
</style>
</head>
<body>
<aside>
  <h2>Local Coding Agent</h2>
  <button id="new">New session</button>
  <h2>Sessions</h2>
  <div id="sessions"></div>
</aside>

<main>
  <div id="tabbar">
    <button class="tab-btn active" data-tab="chat-log">Chat</button>
    <button class="tab-btn" data-tab="agent-log">Agent</button>
    <button class="tab-btn" data-tab="tools-log">Tools</button>
    <button class="tab-btn" data-tab="roots-tab">Roots <span id="status"></span></button>
  </div>
  <div id="tab-body">
    <div id="chat-log" class="tab-content log active"></div>
    <div id="agent-log" class="tab-content log"></div>
    <div id="tools-log" class="tab-content log"></div>
    <div id="roots-tab" class="tab-content">
      <div id="roots-note">
        Writable roots are mounted read/write at <b>/workspace/write/rootN</b>;
        read-only roots are mounted at <b>/workspace/read/rootN</b>. Saving rebuilds
        the sandbox container with the new mounts and rewrites
        <b>roots.txt</b> / <b>readonly_roots.txt</b>, so the change persists across restarts.
        A rebuild clears in-container state (installed packages, /tmp); files in writable
        roots are on the host and stay put.
      </div>

      <div id="profile-bar">
        <label for="profile-name">Name</label>
        <input id="profile-name" list="profile-names" placeholder="profile name"
               autocomplete="off" spellcheck="false">
        <datalist id="profile-names"></datalist>
        <label for="profile-select">Saved</label>
        <select id="profile-select"><option value="">(none)</option></select>
        <button id="profile-apply">Apply &amp; rebuild</button>
        <button id="profile-save">Save current as profile</button>
        <button id="profile-new">Save as new&hellip;</button>
        <button id="profile-delete">Delete</button>
      </div>

      <div id="roots-error"></div>

      <div class="root-group">
        <h3>Writable roots</h3>
        <div id="roots-writable"></div>
        <div class="root-add">
          <input id="add-writable" placeholder="/absolute/host/path">
          <button data-add="writable">Add</button>
        </div>
      </div>

      <div class="root-group">
        <h3>Read-only roots</h3>
        <div id="roots-readonly"></div>
        <div class="root-add">
          <input id="add-readonly" placeholder="/absolute/host/path">
          <button data-add="readonly">Add</button>
        </div>
      </div>

      <div class="picker" id="picker"></div>

      <div style="margin-top:14px">
        <button id="roots-save" style="padding:8px 16px;border:0;border-radius:4px;background:#16a34a;color:white;cursor:pointer">Save &amp; rebuild sandbox</button>
        <span id="roots-msg" style="margin-left:10px;color:#475569"></span>
      </div>
    </div>
  </div>
  <div id="input-area">
    <textarea id="msg-input" placeholder="Tell the agent what to do..." disabled></textarea>
    <button id="send-btn" disabled>Send</button>
    <button id="cancel-btn">Stop</button>
  </div>
</main>

<script>
const chatLog  = document.getElementById('chat-log');
const agentLog = document.getElementById('agent-log');
const toolsLog = document.getElementById('tools-log');
const msgInput = document.getElementById('msg-input');
const sendBtn  = document.getElementById('send-btn');
const cancelBtn= document.getElementById('cancel-btn');
const sessionsEl = document.getElementById('sessions');
const statusEl = document.getElementById('status');

let currentSid = null;
let eventSource = null;
let running = false;
let draft = {writable: [], readonly: []};
let pickerTarget = null;
let profiles = [];
let activeProfileName = '';

document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.onclick = () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById(btn.dataset.tab).classList.add('active');
  };
});

function escapeHtml(s){
  return String(s==null?'':s).replace(/[&<>"']/g,c=>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function append(container, cls, html){
  const d = document.createElement('div');
  d.className = 'msg ' + cls;
  d.innerHTML = html;
  container.appendChild(d);
  container.scrollTop = container.scrollHeight;
}
function toolCallLine(d){
  return `<strong>${escapeHtml(d.name||'tool')}</strong>(${escapeHtml(JSON.stringify(d.arguments||{}))})`;
}
function toolResultLine(d){
  return `<strong>${escapeHtml(d.name||'tool')}</strong>\n${escapeHtml(String(d.result||'')).slice(0,4000)}`;
}

function renderLive(evt){
  const t = evt.type, d = evt.data || {};
  if (t === 'user') {
    append(chatLog, 'user', escapeHtml(d.content));
    append(agentLog, 'user', escapeHtml(d.content));
  } else if (t === 'assistant') {
    append(chatLog, 'assistant', escapeHtml(d.content));
    append(agentLog, 'assistant', escapeHtml(d.content));
  } else if (t === 'tool_call') {
    append(toolsLog, 'tool_call', toolCallLine(d));
    append(agentLog, 'tool_call', toolCallLine(d));
  } else if (t === 'tool_result') {
    append(toolsLog, 'tool_result', toolResultLine(d));
    append(agentLog, 'tool_result', toolResultLine(d));
  } else if (t === 'error') {
    append(chatLog, 'error', escapeHtml(d.message));
    append(agentLog, 'error', escapeHtml(d.message));
  } else if (t === 'compaction') {
    append(agentLog, 'assistant', `<em>[compacted: ${d.messages} messages]</em>`);
  } else if (t === 'hook_block') {
    append(toolsLog, 'error', `[blocked] ${escapeHtml(d.name)}: ${escapeHtml(d.reason)}`);
  }
}

function selectSession(sid){
  currentSid = sid;
  chatLog.innerHTML = agentLog.innerHTML = toolsLog.innerHTML = '';
  document.querySelectorAll('.session').forEach(el =>
    el.classList.toggle('active', el.dataset.sid === sid));
  fetch('/api/sessions/'+sid+'/messages').then(r=>r.json()).then(msgs => {
    msgs.forEach(m => {
      if (m.role === 'user') append(chatLog, 'user', escapeHtml(m.content));
      else if (m.role === 'assistant') append(chatLog, 'assistant', escapeHtml(m.content));
    });
  });
  msgInput.disabled = false; sendBtn.disabled = false;
  if (!eventSource && running) connectStream(sid);
}

async function connectStream(sid){
  if (eventSource) eventSource.close();
  eventSource = new EventSource('/stream/'+sid);
  eventSource.onmessage = (e) => {
    const evt = JSON.parse(e.data);
    renderLive(evt);
    if (evt.type === 'done') {
      eventSource.close(); eventSource = null;
      running = false; unlockInput(); loadSessions();
    }
  };
  eventSource.onerror = () => { if (running) setTimeout(()=>connectStream(sid), 1000); };
}

async function loadSessions(){
  const data = await fetch('/api/sessions').then(r=>r.json());
  sessionsEl.innerHTML = '';
  data.forEach(s => {
    const d = document.createElement('div');
    d.className = 'session' + (s.id===currentSid?' active':'') + (s.running?' running':'');
    d.dataset.sid = s.id;
    d.innerHTML = `<div>${s.id.slice(0,8)}${s.running?' &middot; running':''}</div>
                   <div class="sid">${s.id}</div>`;
    d.onclick = () => selectSession(s.id);
    sessionsEl.appendChild(d);
  });
}

async function createSession(){
  const r = await fetch('/api/sessions', {method:'POST'});
  const d = await r.json();
  if (!r.ok || d.error) { alert(d.error || 'failed to create session'); return; }
  await selectSession(d.session_id);
}

async function send(){
  if (!currentSid || running) return;
  const m = msgInput.value.trim();
  if (!m) return;
  msgInput.value = '';
  lockInput();
  const r = await fetch('/api/sessions/'+currentSid+'/chat', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({message: m}),
  });
  const d = await r.json();
  if (d.status === 'started') { running = true; connectStream(currentSid); loadSessions(); }
  else { alert('Error: ' + (d.error || 'unknown')); unlockInput(); }
}

async function cancelRun(){
  if (!currentSid) return;
  await fetch('/api/sessions/'+currentSid+'/cancel', {method:'POST'});
}

function lockInput(){ msgInput.disabled = true; sendBtn.disabled = true; cancelBtn.style.display='inline-block'; }
function unlockInput(){ msgInput.disabled = !currentSid; sendBtn.disabled = !currentSid; cancelBtn.style.display='none'; }

async function pollStatus(){
  const d = await fetch('/api/status').then(r=>r.json());
  statusEl.textContent = d.state + (d.error ? ': ' + d.error : '');
}

/* ---- Roots tab ---- */

function rootsError(msg){
  const el = document.getElementById('roots-error');
  if (!msg) { el.style.display = 'none'; el.textContent = ''; return; }
  el.style.display = 'block'; el.textContent = msg;
}

function sameSet(a, b){
  if (a.length !== b.length) return false;
  const x = a.slice().sort(), y = b.slice().sort();
  return x.every((v, i) => v === y[i]);
}

function renderRoots(){
  ['writable', 'readonly'].forEach(kind => {
    const container = document.getElementById('roots-' + kind);
    container.innerHTML = '';
    if (!draft[kind].length) {
      const d = document.createElement('div');
      d.className = 'empty';
      d.textContent = '(none configured)';
      container.appendChild(d);
      return;
    }
    draft[kind].forEach((path, idx) => {
      const row = document.createElement('div');
      row.className = 'root-row';
      row.innerHTML = `<span class="sandbox">/workspace/${kind === 'writable' ? 'write' : 'read'}/root${idx}</span>
                       <span class="host">${escapeHtml(path)}</span>`;
      const btn = document.createElement('button');
      btn.textContent = 'remove';
      btn.onclick = () => { draft[kind].splice(idx, 1); renderRoots(); };
      row.appendChild(btn);
      container.appendChild(row);
    });
  });
}

function renderProfiles(){
  const sel = document.getElementById('profile-select');
  sel.innerHTML = '<option value="">(none)</option>';
  profiles.forEach(p => {
    const opt = document.createElement('option');
    opt.value = p.name;
    opt.textContent = p.name;
    sel.appendChild(opt);
  });
  sel.value = profiles.some(p => p.name === activeProfileName) ? activeProfileName : '';
  const dl = document.getElementById('profile-names');
  dl.innerHTML = '';
  profiles.forEach(p => {
    const opt = document.createElement('option');
    opt.value = p.name;
    dl.appendChild(opt);
  });
  document.getElementById('profile-delete').disabled = !sel.value;
}

async function loadProfiles(){
  const r = await fetch('/api/roots/profiles');
  const d = await r.json();
  if (!r.ok || d.error) { rootsError(d.error || 'failed to load profiles'); return; }
  profiles = d.profiles || [];
  renderProfiles();
}

async function loadRoots(){
  const r = await fetch('/api/roots');
  const d = await r.json();
  if (!r.ok || d.error) { rootsError(d.error || 'failed to load roots'); return; }
  draft.writable = d.roots.writable.slice();
  draft.readonly = d.roots.readonly.slice();
  renderRoots();
  rootsError('');
}

function addRoot(kind){
  const input = document.getElementById('add-' + kind);
  const value = input.value.trim();
  if (!value) return;
  if (!value.startsWith('/')) { rootsError('Path must be absolute (start with /).'); return; }
  draft[kind].push(value);
  input.value = '';
  rootsError('');
  renderRoots();
}

async function saveRoots(){
  const msg = document.getElementById('roots-msg');
  msg.textContent = 'rebuilding...';
  rootsError('');
  const r = await fetch('/api/roots', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({writable: draft.writable, readonly: draft.readonly}),
  });
  const d = await r.json();
  if (!r.ok || d.error) {
    rootsError(d.error || 'failed to save');
    msg.textContent = '';
    await loadRoots();
    return;
  }
  msg.textContent = 'saved';
  setTimeout(() => { msg.textContent = ''; }, 2500);
  await loadRoots();
}

/* Load the selected profile's paths into the editable draft. This does NOT
   rebuild anything: the operator can inspect or tweak, then apply. */
function selectProfile(name){
  activeProfileName = name;
  const p = profiles.find(x => x.name === name);
  if (p) {
    draft.writable = p.writable.slice();
    draft.readonly = p.readonly.slice();
    renderRoots();
    document.getElementById('profile-name').value = name;
  }
  document.getElementById('profile-delete').disabled = !name;
}

async function applyProfile(){
  const name = document.getElementById('profile-select').value;
  const msg = document.getElementById('roots-msg');
  if (!name) { rootsError('Select a profile first.'); return; }
  msg.textContent = 'rebuilding...';
  rootsError('');
  const r = await fetch('/api/roots/profiles/apply', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({name}),
  });
  const d = await r.json();
  if (!r.ok || d.error) { rootsError(d.error || 'failed to apply profile'); msg.textContent = ''; return; }
  msg.textContent = 'applied ' + name;
  setTimeout(() => { msg.textContent = ''; }, 2500);
  await loadRoots();
}

function currentProfileName(){
  return (document.getElementById('profile-name').value || '').trim();
}

/* Save the current draft under the name shown in the name field. Saves over an
   existing profile of that name, or creates it if new. */
async function saveCurrentAsProfile(){
  const name = currentProfileName();
  if (!name) {
    rootsError('Enter a profile name first, then Save.');
    document.getElementById('profile-name').focus();
    return;
  }
  await putProfile(name, draft.writable, draft.readonly);
}

/* Clear the name field so the operator can type a fresh name. */
function saveAsNewProfile(){
  const field = document.getElementById('profile-name');
  if (profiles.some(p => p.name === currentProfileName())) {
    field.value = '';
    activeProfileName = '';
    renderProfiles();
  }
  field.focus();
}

async function putProfile(name, writable, readonly){
  rootsError('');
  const r = await fetch('/api/roots/profiles', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({name, writable, readonly}),
  });
  const d = await r.json();
  if (!r.ok || d.error) { rootsError(d.error || 'failed to save profile'); return; }
  profiles = d.profiles || [];
  activeProfileName = name;
  renderProfiles();
  document.getElementById('profile-name').value = name;
  rootsError('');
  const msg = document.getElementById('roots-msg');
  msg.textContent = 'profile "' + name + '" saved';
  setTimeout(() => { msg.textContent = ''; }, 2500);
}

async function deleteProfile(){
  const name = document.getElementById('profile-select').value;
  if (!name) return;
  if (!confirm('Delete profile "' + name + '"?')) return;
  rootsError('');
  const r = await fetch('/api/roots/profiles/' + encodeURIComponent(name), {method: 'DELETE'});
  const d = await r.json();
  if (!r.ok || d.error) { rootsError(d.error || 'failed to delete profile'); return; }
  profiles = d.profiles || [];
  if (activeProfileName === name) {
    activeProfileName = '';
    if (currentProfileName() === name) document.getElementById('profile-name').value = '';
  }
  renderProfiles();
}

async function pickerTo(path){
  const picker = document.getElementById('picker');
  const url = path ? '/api/browse?path=' + encodeURIComponent(path) : '/api/browse';
  const d = await fetch(url).then(r => r.json());
  if (d.error) { rootsError(d.error); return; }
  picker.innerHTML = '';
  if (d.parent) {
    const up = document.createElement('div');
    up.className = 'row up';
    up.textContent = '.. ' + d.parent;
    up.onclick = () => pickerTo(d.parent);
    picker.appendChild(up);
  }
  const head = document.createElement('div');
  head.className = 'row';
  head.innerHTML = `<b>${escapeHtml(d.path)}</b>`;
  head.onclick = () => {
    document.getElementById('add-' + pickerTarget).value = d.path;
    picker.style.display = 'none';
  };
  picker.appendChild(head);
  d.directories.forEach(entry => {
    const row = document.createElement('div');
    row.className = 'row';
    row.textContent = entry.name + '/';
    row.onclick = () => pickerTo(entry.path);
    picker.appendChild(row);
  });
}

document.getElementById('new').onclick = createSession;
sendBtn.onclick = send;
cancelBtn.onclick = cancelRun;
document.getElementById('roots-save').onclick = saveRoots;
document.getElementById('profile-select').onchange = e => selectProfile(e.target.value);
document.getElementById('profile-apply').onclick = applyProfile;
document.getElementById('profile-save').onclick = saveCurrentAsProfile;
document.getElementById('profile-new').onclick = saveAsNewProfile;
document.getElementById('profile-delete').onclick = deleteProfile;
document.querySelectorAll('[data-add]').forEach(btn => {
  btn.onclick = () => addRoot(btn.dataset.add);
});
['writable', 'readonly'].forEach(kind => {
  document.getElementById('add-' + kind).addEventListener('keydown', e => {
    if (e.key === 'Enter') addRoot(kind);
  });
});
msgInput.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
});

(async function init(){
  pollStatus();
  const data = await fetch('/api/sessions').then(r=>r.json());
  if (data.length) await selectSession(data[0].id);
  else await createSession();
  await loadRoots();
  await loadProfiles();
  setInterval(loadSessions, 3000);
  setInterval(pollStatus, 2000);
})();
</script>
</body>
</html>
"""


def main() -> None:
    global _root_manager, _profile_store
    import os
    if not os.getenv("DEEPSEEK_API_KEY"):
        raise SystemExit("DEEPSEEK_API_KEY is required in .env")

    _root_manager = RootManager(
        APP_DIR / "roots.txt",
        APP_DIR / "readonly_roots.txt",
        IMAGE,
        writable_header="# One absolute host directory per line.\n"
                        "# Each is mounted read/write as /workspace/write/rootN.",
        readonly_header="# Optional read-only reference directories, one absolute host directory per line.\n"
                        "# Each is mounted read-only as /workspace/read/rootN.",
    )
    # Per-container state is stale after a rebuild: clear every Excel bridge's
    # install flag so the engine is re-shipped into the fresh container.
    _root_manager.on_rebuild(_invalidate_container_caches)

    # Saved root profiles live beside the root files, in the app directory
    # (which is writable by the host app and read-only inside the sandbox).
    _profile_store = ProfileStore(APP_DIR / "roots_profiles.json")

    threading.Thread(target=_start_workspace, daemon=True).start()
    app.run(host=HOST, port=PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
