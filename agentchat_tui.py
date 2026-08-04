#!/usr/bin/env python3
"""AgentChat TUI: a secure, batteries-included terminal agent client.

Only Python's standard library is required. The client speaks the OpenAI Chat
Completions protocol, including streaming and tool calls.
"""
from __future__ import annotations
import curses, json, os, queue, shlex, sqlite3, subprocess, textwrap, threading, time, urllib.error, urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

APP = "CIPROCODE CLI"
VERSION = "2.0.0"
DEFAULT_BASE = "https://api.openai.com/v1"
DASHSCOPE_BASE = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
DEFAULT_SYSTEM = """You are a careful, pragmatic software engineering agent.
Use tools when they materially help. Be concise but useful. Never claim an action
succeeded without checking its result. Ask before destructive or irreversible actions.
The current workspace is the project directory."""
DB_PATH = Path.home() / ".agentchat" / "sessions.db"

def load_dotenv() -> None:
    """Load a local .env without overwriting explicitly exported variables."""
    path = Path.cwd() / ".env"
    if not path.exists(): return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        key, value = line.split("=", 1); value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)

@dataclass
class Config:
    base_url: str = DEFAULT_BASE
    model: str = "gpt-4o-mini"
    api_key: str = ""
    temperature: float = 0.2
    max_steps: int = 8
    stream: bool = True
    approve_tools: bool = True
    system: str = DEFAULT_SYSTEM
    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        model = os.getenv("OPENAI_MODEL", os.getenv("DASHSCOPE_MODEL", "gpt-4o-mini"))
        is_qwen = model.lower().startswith("qwen")
        base = os.getenv("OPENAI_BASE_URL", os.getenv("DASHSCOPE_BASE_URL", DASHSCOPE_BASE if is_qwen else DEFAULT_BASE))
        key = os.getenv("OPENAI_API_KEY", os.getenv("DASHSCOPE_API_KEY", ""))
        return cls(base, model, key, float(os.getenv("OPENAI_TEMPERATURE", "0.2")), int(os.getenv("AGENT_MAX_STEPS", "8")), os.getenv("AGENT_STREAM", "1") != "0", os.getenv("AGENT_APPROVE_TOOLS", "1") != "0", os.getenv("AGENT_SYSTEM_PROMPT", DEFAULT_SYSTEM))

@dataclass
class Message:
    role: str
    content: str
    timestamp: str = ""
    name: str = ""
    tool_call_id: str = ""

class SessionStore:
    def __init__(self):
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS sessions (id INTEGER PRIMARY KEY, title TEXT, created TEXT, updated TEXT, messages TEXT)")
        self.db.commit()
    def save(self, title: str, messages: list[Message], sid: int | None = None) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        data = json.dumps([m.__dict__ for m in messages], ensure_ascii=False)
        if sid:
            self.db.execute("UPDATE sessions SET title=?,updated=?,messages=? WHERE id=?", (title, now, data, sid))
        else:
            cur = self.db.execute("INSERT INTO sessions(title,created,updated,messages) VALUES(?,?,?,?)", (title, now, now, data)); sid = cur.lastrowid
        self.db.commit(); return int(sid)
    def list(self) -> list[tuple[int, str, str]]:
        return self.db.execute("SELECT id,title,updated FROM sessions ORDER BY updated DESC LIMIT 30").fetchall()
    def load(self, sid: int) -> list[Message]:
        row = self.db.execute("SELECT messages FROM sessions WHERE id=?", (sid,)).fetchone()
        return [Message(**x) for x in json.loads(row[0])] if row else []
    def delete(self, sid: int) -> None:
        self.db.execute("DELETE FROM sessions WHERE id=?", (sid,)); self.db.commit()

class AgentClient:
    def __init__(self, cfg: Config, event: Any): self.cfg, self.event = cfg, event
    def call(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        payload = {"model": self.cfg.model, "messages": messages, "temperature": self.cfg.temperature, "tools": tools, "tool_choice": "auto"}
        if self.cfg.model.lower().startswith("qwen"): payload["enable_thinking"] = True
        body = json.dumps(payload).encode(); req = urllib.request.Request(self.cfg.base_url.rstrip("/") + "/chat/completions", body, method="POST")
        req.add_header("Authorization", "Bearer " + self.cfg.api_key); req.add_header("Content-Type", "application/json"); req.add_header("User-Agent", f"agentchat-tui/{VERSION}")
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=120) as res:
                    return json.loads(res.read())
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:500]
                if e.code not in (408, 429, 500, 502, 503) or attempt == 2: raise RuntimeError(f"API {e.code}: {detail}")
                time.sleep(2 ** attempt)
        raise RuntimeError("request failed")
    def stream_answer(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        # Streaming is intentionally best-effort: tool calls use the normal endpoint.
        if not self.cfg.stream: return self.call(messages, tools)
        payload = {"model": self.cfg.model, "messages": messages, "temperature": self.cfg.temperature, "tools": tools, "tool_choice": "auto", "stream": True}
        if self.cfg.model.lower().startswith("qwen"): payload["enable_thinking"] = True
        req = urllib.request.Request(self.cfg.base_url.rstrip("/") + "/chat/completions", json.dumps(payload).encode(), method="POST")
        req.add_header("Authorization", "Bearer " + self.cfg.api_key); req.add_header("Content-Type", "application/json")
        text = ""; tool_calls: dict[int, dict[str, Any]] = {}; usage = {}
        try:
            with urllib.request.urlopen(req, timeout=120) as res:
                for raw in res:
                    line = raw.decode(errors="replace").strip()
                    if not line.startswith("data:"): continue
                    chunk = line[5:].strip()
                    if chunk == "[DONE]": break
                    try: delta = json.loads(chunk).get("choices", [{}])[0].get("delta", {})
                    except json.JSONDecodeError: continue
                    if delta.get("content"):
                        text += delta["content"]; self.event.put(("token", delta["content"]))
                    for call in delta.get("tool_calls", []):
                        i = call.get("index", 0); item = tool_calls.setdefault(i, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                        item["id"] += call.get("id", "") or ""; fn = call.get("function", {}); item["function"]["name"] += fn.get("name", "") or ""; item["function"]["arguments"] += fn.get("arguments", "") or ""
            return {"choices": [{"message": {"role": "assistant", "content": text, **({"tool_calls": list(tool_calls.values())} if tool_calls else {})}}], "usage": usage}
        except Exception:
            return self.call(messages, tools)

def specs() -> list[dict[str, Any]]:
    def f(name, description, properties, required): return {"type":"function","function":{"name":name,"description":description,"parameters":{"type":"object","properties":properties,"required":required}}}
    return [f("list_files", "List project files (hidden/build/cache files are omitted).", {"path":{"type":"string"}}, []), f("read_file", "Read a UTF-8 project file, capped at 30 KB.", {"path":{"type":"string"}}, ["path"]), f("run_command", "Run a non-destructive shell command in the project. Always explain why first.", {"command":{"type":"string"}}, ["command"])]

def inside(raw: str) -> Path:
    root = Path.cwd().resolve(); p = (root / (raw or ".")).resolve()
    if p != root and root not in p.parents: raise ValueError("path escapes the workspace")
    return p

def execute(name: str, args: dict[str, Any]) -> str:
    try:
        if name == "list_files":
            p = inside(args.get("path", ".")); rows = []
            for x in sorted(p.rglob("*")):
                if any(part in {".git", ".venv", "__pycache__", "node_modules"} for part in x.parts): continue
                rows.append(str(x.relative_to(Path.cwd())) + ("/" if x.is_dir() else ""))
                if len(rows) >= 300: break
            return "\n".join(rows) or "(empty)"
        if name == "read_file": return inside(args["path"]).read_text(encoding="utf-8")[:30000]
        if name == "run_command":
            cmd = args["command"].strip(); low = cmd.lower()
            bad = ("rm -rf", "sudo ", "shutdown", "reboot", "mkfs", ":(){", "dd if=", "git push --force")
            if any(x in low for x in bad): return "Rejected by safety policy: potentially destructive command."
            r = subprocess.run(cmd, shell=True, cwd=Path.cwd(), capture_output=True, text=True, timeout=45)
            return (r.stdout + r.stderr)[-16000:] or f"(exit {r.returncode}, no output)"
        return "Unknown tool"
    except Exception as e: return f"Tool error: {type(e).__name__}: {e}"

class App:
    def __init__(self, screen: Any):
        self.s = screen; self.cfg = Config.from_env(); self.store = SessionStore(); self.sid = None; self.title = "New session"
        self.view: list[Message] = []; self.api: list[dict[str, Any]] = [{"role":"system","content":self.cfg.system}]; self.input = ""; self.status = "Ready"; self.busy = False; self.scroll = 0; self.events: queue.Queue = queue.Queue(); self.started = time.time()
    def add(self, role: str, text: str, **kw: Any) -> None:
        m = Message(role, text, datetime.now().strftime("%H:%M"), **kw); self.view.append(m)
        if role != "system": self.api.append({"role": role, "content": text, **({"name":m.name} if m.name else {}), **({"tool_call_id":m.tool_call_id} if m.tool_call_id else {})})
    def save(self) -> None:
        if self.view: self.sid = self.store.save(self.title, self.view, self.sid)
    def agent(self) -> None:
        try:
            calls_done = 0; client = AgentClient(self.cfg, self.events)
            while calls_done < self.cfg.max_steps:
                result = client.stream_answer(self.api, specs()); msg = result["choices"][0]["message"]; calls = msg.get("tool_calls") or []
                if not calls:
                    self.events.put(("answer", msg.get("content") or "(empty response)")); self.events.put(("done", None)); return
                self.api.append(msg)
                for call in calls:
                    calls_done += 1; fn = call.get("function", {}); name = fn.get("name", ""); args = json.loads(fn.get("arguments") or "{}")
                    if self.cfg.approve_tools:
                        self.events.put(("approval", f"Tool {name} wants to run with {json.dumps(args, ensure_ascii=False)}. Press y to allow / n to deny."))
                        while True:
                            approval_event, answer = self.events.get()
                            if approval_event == "approval_result" and answer in ("y", "n"): break
                        if answer == "n": out = "Denied by user."
                        else: out = execute(name, args)
                    else: out = execute(name, args)
                    self.api.append({"role":"tool","tool_call_id":call.get("id", ""),"content":out}); self.events.put(("tool", f"{name}: {out[:160].replace(chr(10),' ')}"))
            self.events.put(("answer", "Stopped: maximum agent steps reached.")); self.events.put(("done", None))
        except Exception as e: self.events.put(("error", f"{type(e).__name__}: {e}")); self.events.put(("done", None))
    def command(self, text: str) -> bool:
        parts = shlex.split(text); cmd = parts[0].lower() if parts else ""
        if cmd in ("/quit", "/exit"): return False
        if cmd == "/help": self.add("assistant", "Commands: /help /clear /save [title] /sessions /load ID /model NAME /provider URL /export FILE /approve on|off /retry /quit"); return True
        if cmd == "/clear": self.view.clear(); self.api = [{"role":"system","content":self.cfg.system}]; self.title="New session"; self.status="Cleared"; return True
        if cmd == "/save": self.title = " ".join(parts[1:]) or self.title; self.save(); self.status=f"Saved session #{self.sid}"; return True
        if cmd == "/sessions": self.add("assistant", "\n".join(f"{i}  {t}  ({u})" for i,t,u in self.store.list()) or "No saved sessions."); return True
        if cmd == "/load" and len(parts)>1:
            self.view=self.store.load(int(parts[1])); self.api=[{"role":"system","content":self.cfg.system}]+[{"role":m.role,"content":m.content} for m in self.view]; self.sid=int(parts[1]); self.status="Loaded"; return True
        if cmd == "/model" and len(parts)>1: self.cfg.model=parts[1]; self.status=f"Model: {self.cfg.model}"; return True
        if cmd == "/provider" and len(parts)>1: self.cfg.base_url=parts[1]; self.status="Provider updated"; return True
        if cmd == "/approve" and len(parts)>1: self.cfg.approve_tools=parts[1].lower() in ("on","yes","true"); self.status=f"Tool approval {'on' if self.cfg.approve_tools else 'off'}"; return True
        if cmd == "/export" and len(parts)>1:
            Path(parts[1]).write_text("\n\n".join(f"## {m.role.upper()} · {m.timestamp}\n{m.content}" for m in self.view), encoding="utf-8"); self.status=f"Exported {parts[1]}"; return True
        if cmd == "/retry" and self.view and not self.busy:
            last=next((m for m in reversed(self.view) if m.role=="user"),None); 
            if last: self.busy=True; threading.Thread(target=self.agent,daemon=True).start()
            return True
        self.add("assistant", "Unknown command. Try /help."); return True
    def render(self) -> None:
        self.s.erase(); h, w = self.s.getmaxyx(); w = max(w, 48)
        sidebar = 26 if w >= 90 else 0
        main_x = sidebar + 1 if sidebar else 0
        main_w = w - main_x
        def put(y: int, x: int, text: str, pair: int = 0, maxw: int | None = None) -> None:
            try:
                if maxw is not None: text = text[:maxw]
                self.s.addstr(y, x, text, curses.color_pair(pair))
            except curses.error: pass
        # Header: deliberately compact, similar to modern agent CLIs.
        put(0, 0, "╭" + "─" * (w - 2) + "╮", 1)
        put(1, 0, "│", 1); put(1, 2, "◆ CIPROCODE CLI", 1); put(1, 21, f"v{VERSION}", 4)
        put(1, main_x + 2, f"{self.cfg.model}  ·  {'● connected' if self.cfg.api_key else '○ API key missing'}", 4, main_w - 4); put(1, w - 2, "│", 1)
        put(2, 0, "├" + "─" * (w - 2) + "┤", 1)
        # Left rail: session context and quick controls.
        if sidebar:
            for y in range(3, h - 4): put(y, sidebar, "│", 4)
            put(3, 2, "WORKSPACE", 4); put(4, 2, "◆ current project", 3, sidebar - 4)
            put(6, 2, "SESSION", 4); put(7, 2, (self.title or "New session")[:sidebar-4], 2)
            put(9, 2, "SHORTCUTS", 4)
            for y, text in enumerate(("Enter   send message", "↑ ↓     scroll chat", "Ctrl+L  clear", "Ctrl+S  reload .env", "Ctrl+Q  quit"), 10): put(y, 2, text, 0, sidebar - 4)
            put(h-6, 2, "AGENT", 4); put(h-5, 2, "tools: " + ("approval" if self.cfg.approve_tools else "auto"), 0, sidebar-4); put(h-4, 2, "steps: " + str(self.cfg.max_steps), 0, sidebar-4)
        # Conversation viewport.
        rows: list[tuple[str, int]] = []
        for m in self.view:
            label = {"user":"YOU", "assistant":"CIPROCODE", "tool":"TOOL"}.get(m.role, m.role.upper())
            color = {"user":2, "assistant":3, "tool":5}.get(m.role, 0)
            rows.append((f"{label}  ·  {m.timestamp}", color))
            for line in m.content.splitlines() or [""]:
                wrapped = textwrap.wrap(line, max(10, main_w - 8), replace_whitespace=False) or [""]
                rows.extend(("  " + part, 0) for part in wrapped)
            rows.append(("", 0))
        visible = max(1, h - 8); start = max(0, len(rows) - visible - self.scroll)
        for y, (line, pair) in enumerate(rows[start:start + visible], 3): put(y, main_x + 3, line, pair, main_w - 6)
        # Composer and footer.
        put(h-4, 0, "├" + "─" * (w - 2) + "┤", 1)
        put(h-3, 0, "│", 1); put(h-3, main_x + 2, "❯ " + (self.input or "Ask anything…"), 2 if self.input else 4, main_w - 4); put(h-3, w - 2, "│", 1)
        put(h-2, 0, "│", 1); put(h-2, main_x + 2, self.status, 4, main_w - 4); put(h-2, w - 2, "│", 1)
        put(h-1, 0, "╰" + "─" * (w - 2) + "╯", 1); self.s.refresh()
    def loop(self) -> None:
        curses.curs_set(1); self.s.timeout(100); curses.start_color(); curses.use_default_colors()
        for i,c in enumerate(((-1,curses.COLOR_CYAN),(curses.COLOR_CYAN,-1),(curses.COLOR_GREEN,-1),(curses.COLOR_YELLOW,-1),(curses.COLOR_MAGENTA,-1)),1): curses.init_pair(i,*c)
        while True:
            while True:
                try: event,data=self.events.get_nowait()
                except queue.Empty: break
                if event=="answer": self.add("assistant",data); self.status="Ready"; self.save()
                elif event=="token": self.status="Receiving response…"
                elif event=="tool": self.add("tool",data); self.status="Tool complete"
                elif event=="approval": self.status=data
                elif event=="approval_result": pass
                elif event=="error": self.add("assistant","⚠ "+data); self.status="Error"
                elif event=="done": self.busy=False
            self.render(); key=self.s.getch()
            if key in (3,17): self.save(); return
            if key==12: self.command("/clear")
            elif key==19: self.cfg=Config.from_env(); self.status="Environment settings reloaded"
            elif key in (curses.KEY_UP,): self.scroll+=1
            elif key in (curses.KEY_DOWN,): self.scroll=max(0,self.scroll-1)
            elif key in (ord('y'),ord('Y')) and self.status.startswith("Tool "): self.events.put(("approval_result","y")); self.status="Approved; running tool…"
            elif key in (ord('n'),ord('N')) and self.status.startswith("Tool "): self.events.put(("approval_result","n")); self.status="Denied; continuing…"
            elif key in (10,13) and self.input.strip() and not self.busy:
                text=self.input.strip(); self.input=""
                if text.startswith("/"): self.command(text); continue
                self.add("user",text); self.title=(text[:48] + "…") if len(text)>48 else text
                if not self.cfg.api_key: self.add("assistant","⚠ Set OPENAI_API_KEY, then press Ctrl+S."); continue
                self.busy=True; self.status="Thinking…"; threading.Thread(target=self.agent,daemon=True).start()
            elif key in (curses.KEY_BACKSPACE,127,8): self.input=self.input[:-1]
            elif 32<=key<=126: self.input+=chr(key)

def main() -> None:
    curses.wrapper(lambda screen: App(screen).loop())
if __name__ == "__main__": main()
