#!/usr/bin/env python3
"""AgentChat TUI — a small, dependency-free terminal UI for an OpenAI-compatible agent."""
from __future__ import annotations

import curses
import json
import os
import queue
import subprocess
import threading
import textwrap
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

APP = "AgentChat TUI"
DEFAULT_BASE = "https://api.openai.com/v1"
SYSTEM_PROMPT = """You are a careful, pragmatic software engineering agent.
Use tools when they materially help. Explain actions briefly, never claim a tool action
succeeded without checking its result, and ask before destructive operations."""


@dataclass
class Message:
    role: str
    content: str
    timestamp: str = ""


class AgentClient:
    def __init__(self, base_url: str, api_key: str, model: str, tools: list[dict[str, Any]]):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.tools = tools

    def complete(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        body = json.dumps({"model": self.model, "messages": messages, "tools": self.tools, "tool_choice": "auto"}).encode()
        req = urllib.request.Request(self.base_url + "/chat/completions", data=body, method="POST")
        req.add_header("Authorization", "Bearer " + self.api_key)
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=120) as response:
            return json.loads(response.read())


def tool_specs() -> list[dict[str, Any]]:
    return [
        {"type": "function", "function": {"name": "list_files", "description": "List files in a directory.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": []}}},
        {"type": "function", "function": {"name": "read_file", "description": "Read a UTF-8 text file, capped at 20 KB.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
        {"type": "function", "function": {"name": "run_command", "description": "Run a shell command in the project directory. Use only safe, non-destructive commands.", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
    ]


def safe_path(raw: str) -> Path:
    root = Path.cwd().resolve()
    path = (root / (raw or ".")).resolve()
    if root not in path.parents and path != root:
        raise ValueError("Path must stay inside the current project directory")
    return path


def execute_tool(name: str, args: dict[str, Any]) -> str:
    try:
        if name == "list_files":
            p = safe_path(args.get("path", "."))
            return "\n".join(str(x.relative_to(Path.cwd())) for x in sorted(p.iterdir())[:200])
        if name == "read_file":
            return safe_path(args["path"]).read_text(encoding="utf-8")[:20000]
        if name == "run_command":
            command = args["command"]
            forbidden = ["rm ", "sudo", "shutdown", "reboot", "mkfs", ":(){"]
            if any(x in command.lower() for x in forbidden):
                return "Rejected: potentially destructive command."
            result = subprocess.run(command, shell=True, cwd=Path.cwd(), capture_output=True, text=True, timeout=30)
            return (result.stdout + result.stderr)[-12000:] or "(no output)"
        return "Unknown tool"
    except Exception as exc:
        return f"Tool error: {exc}"


class App:
    def __init__(self, stdscr: Any):
        self.stdscr = stdscr
        self.messages: list[Message] = []
        self.api_messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.input = ""
        self.status = "Ready · Ctrl+S settings · Ctrl+L clear · Ctrl+C quit"
        self.scroll = 0
        self.busy = False
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.base_url = os.getenv("OPENAI_BASE_URL", DEFAULT_BASE)
        self.model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        self.api_key = os.getenv("OPENAI_API_KEY", "")

    def add(self, role: str, content: str) -> None:
        self.messages.append(Message(role, content, datetime.now().strftime("%H:%M")))
        self.api_messages.append({"role": role, "content": content})

    def agent(self, prompt: str) -> None:
        try:
            while True:
                result = AgentClient(self.base_url, self.api_key, self.model, tool_specs()).complete(self.api_messages)
                msg = result["choices"][0]["message"]
                calls = msg.get("tool_calls") or []
                if not calls:
                    content = msg.get("content") or "(empty response)"
                    self.events.put(("answer", content))
                    return
                self.api_messages.append(msg)
                for call in calls:
                    name = call["function"]["name"]
                    args = json.loads(call["function"].get("arguments") or "{}")
                    self.events.put(("status", f"Running {name}…"))
                    output = execute_tool(name, args)
                    self.api_messages.append({"role": "tool", "tool_call_id": call["id"], "content": output})
        except Exception as exc:
            self.events.put(("error", f"{type(exc).__name__}: {exc}"))

    def draw(self) -> None:
        self.stdscr.erase()
        h, w = self.stdscr.getmaxyx()
        self.stdscr.attron(curses.color_pair(1)); self.stdscr.addstr(0, 0, f"  {APP}  "); self.stdscr.attroff(curses.color_pair(1))
        self.stdscr.addstr(0, 18, f"{self.model}  ·  {'configured' if self.api_key else 'API key missing'}")
        lines: list[tuple[str, int]] = []
        for m in self.messages:
            label = "YOU" if m.role == "user" else "AGENT"
            color = 2 if m.role == "user" else 3
            lines.append((f"{label}  {m.timestamp}", color))
            for line in m.content.splitlines() or [""]:
                lines += [("  " + x, 0) for x in textwrap.wrap(line, max(12, w - 6)) or [""]]
            lines.append(("", 0))
        visible = h - 5
        start = max(0, len(lines) - visible - self.scroll)
        for y, (line, color) in enumerate(lines[start:start + visible], 2):
            try: self.stdscr.addstr(y, 2, line[:w - 4], curses.color_pair(color))
            except curses.error: pass
        self.stdscr.attron(curses.color_pair(4)); self.stdscr.addstr(h - 3, 0, "─" * (w - 1)); self.stdscr.attroff(curses.color_pair(4))
        self.stdscr.addstr(h - 2, 2, "> " + self.input[:w - 6])
        self.stdscr.addstr(h - 1, 2, self.status[:w - 4], curses.color_pair(4))
        self.stdscr.refresh()

    def run(self) -> None:
        curses.curs_set(1); self.stdscr.timeout(100)
        curses.start_color(); curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN); curses.init_pair(2, curses.COLOR_CYAN, -1); curses.init_pair(3, curses.COLOR_GREEN, -1); curses.init_pair(4, curses.COLOR_YELLOW, -1)
        while True:
            self.draw()
            try:
                event, data = self.events.get_nowait()
                if event == "answer": self.add("assistant", data); self.busy = False; self.status = "Ready"
                elif event == "error": self.add("assistant", "⚠ " + data); self.busy = False; self.status = "Error"
                else: self.status = data
            except queue.Empty: pass
            key = self.stdscr.getch()
            if key in (3, 17): return
            if key == 12: self.messages.clear(); self.api_messages = [{"role": "system", "content": SYSTEM_PROMPT}]; self.status = "Conversation cleared"
            elif key == 19:
                self.api_key = os.getenv("OPENAI_API_KEY", self.api_key); self.status = "Settings reloaded from environment"
            elif key in (curses.KEY_UP,): self.scroll = min(self.scroll + 1, max(0, len(self.messages) - 1))
            elif key in (curses.KEY_DOWN,): self.scroll = max(0, self.scroll - 1)
            elif key in (10, 13) and self.input.strip() and not self.busy:
                prompt = self.input.strip(); self.input = ""; self.add("user", prompt)
                if not self.api_key: self.add("assistant", "⚠ Set OPENAI_API_KEY before chatting."); continue
                self.busy = True; self.status = "Thinking…"; threading.Thread(target=self.agent, args=(prompt,), daemon=True).start()
            elif key in (curses.KEY_BACKSPACE, 127, 8): self.input = self.input[:-1]
            elif 32 <= key <= 126: self.input += chr(key)


def main() -> None:
    curses.wrapper(App)

if __name__ == "__main__": main()
