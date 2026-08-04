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
        """Render a focused, dark, modern agent workspace.

        Empty sessions get a centered welcome panel; active sessions switch to a
        clean transcript with a fixed composer, blue accent rail, and muted metadata.
        """
        self.s.erase(); h, w = self.s.getmaxyx(); w = max(w, 52)
        blue, white, muted, amber, magenta = 2, 3, 4, 4, 5
        def put(y: int, x: int, text: str, pair: int = 0, maxw: int | None = None, attrs: int = 0) -> None:
            try:
                if maxw is not None: text = text[:maxw]
                self.s.addstr(max(0, y), max(0, x), text, curses.color_pair(pair) | attrs)
            except curses.error: pass
        def center(y: int, text: str, pair: int = 0) -> None: put(y, max(0, (w - len(text)) // 2), text, pair)
        # Global chrome: almost-black canvas, tiny brand mark, quiet footer.
        put(1, 2, "◆", blue); put(1, 4, "CIPROCODE", white); put(1, 14, "CLI", muted)
        put(1, w - 21, self.cfg.model, muted, 18); put(1, w - 2, "·", blue)
        if not self.view:
            logo = [" ██████╗██╗██████╗ ██████╗  ██████╗  ██████╗ ██████╗ ██████╗ ███████╗",
                    "██╔════╝██║██╔══██╗██╔══██╗██╔═══██╗██╔════╝██╔══██╗██╔══██╗██╔════╝",
                    "██║     ██║██████╔╝██████╔╝██║   ██║██║     ██████╔╝██║  ██║█████╗  ",
                    "██║     ██║██╔═══╝ ██╔══██╗██║   ██║██║     ██╔══██╗██║  ██║██╔══╝  ",
                    "╚██████╗██║██║     ██║  ██║╚██████╔╝╚██████╗██║  ██║██████╔╝███████╗",
                    " ╚═════╝╚═╝╚═╝     ╚═╝  ╚═╝ ╚═════╝  ╚═════╝╚═╝  ╚═╝╚═════╝ ╚══════╝"]
            # Compact wordmark fallback if terminal is too narrow.
            if w >= 84:
                for i, line in enumerate(logo): center(5 + i, line, white if i > 1 else muted)
                panel_y, panel_w = 13, min(680, w - 18)
            else:
                center(6, "CIPROCODE CLI", white); panel_y, panel_w = 9, min(680, w - 10)
            left = max(3, (w - panel_w) // 2)
            # Input card with a single electric-blue left rail.
            for y in range(panel_y, panel_y + 3):
                put(y, left, "▌", blue); put(y, left + 2, " " * max(1, panel_w - 5), 0, panel_w - 5, curses.A_REVERSE)
            put(panel_y + 1, left + 3, self.input or 'Ask anything…  Try "explain this project"', white if self.input else muted, panel_w - 8)
            put(panel_y + 3, left + 3, "Build", blue); put(panel_y + 3, left + 10, "·", muted); put(panel_y + 3, left + 13, self.cfg.model, white, panel_w - 16)
            center(panel_y + 5, "enter  send     ctrl+l  clear     ctrl+p  commands", muted)
            center(panel_y + 8, "●", amber); put(panel_y + 8, (w // 2) + 3, "Tip  Keep your API key in .env — never commit secrets", amber, w // 2 - 5)
            put(h - 2, 2, str(Path.cwd()), muted, max(10, w - 30)); put(h - 2, w - 15, f"v{VERSION}", muted); put(h - 2, w - 2, "·", blue)
        else:
            # Chat mode: transcript fills the canvas; the input card remains anchored.
            rows: list[tuple[str, int]] = []
            for m in self.view:
                label = {"user":"YOU", "assistant":"CIPROCODE", "tool":"TOOL"}.get(m.role, m.role.upper())
                pair = blue if m.role == "user" else (magenta if m.role == "tool" else white)
                rows.append((f"{label}  ·  {m.timestamp}", pair))
                for line in m.content.splitlines() or [""]:
                    rows.extend(("  " + part, 0) for part in textwrap.wrap(line, max(12, w - 9), replace_whitespace=False) or [""])
                rows.append(("", 0))
            visible = max(1, h - 9); start = max(0, len(rows) - visible - self.scroll)
            for y, (line, pair) in enumerate(rows[start:start + visible], 3): put(y, 4, line, pair, w - 8)
            # Scroll indicator on the far right.
            if len(rows) > visible:
                track = max(1, h - 11); thumb = max(1, track * visible // len(rows)); pos = min(track - thumb, track * self.scroll // max(1, len(rows)))
                for i in range(track): put(3 + i, w - 2, "█" if pos <= i < pos + thumb else "│", blue if pos <= i < pos + thumb else muted)
            put(h - 6, 2, "▌", blue); put(h - 6, 4, self.input or "Ask anything…", white if self.input else muted, w - 8)
            put(h - 5, 4, self.status, amber, w - 8)
            put(h - 3, 3, str(Path.cwd()), muted, w - 28); put(h - 3, w - 23, "8.7K", muted); put(h - 3, w - 11, "ctrl+p commands", white)
        self.s.refresh()
    def loop(self) -> None:
        curses.curs_set(1); self.s.timeout(100); curses.start_color(); curses.use_default_colors()
        for i,c in enumerate(((-1,-1),(curses.COLOR_BLUE,-1),(curses.COLOR_WHITE,-1),(curses.COLOR_YELLOW,-1),(curses.COLOR_MAGENTA,-1)),1): curses.init_pair(i,*c)
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
