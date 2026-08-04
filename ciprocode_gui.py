#!/usr/bin/env python3
"""Ciprocode GUI — responsive desktop client for the coding agent."""
from __future__ import annotations
import json, queue, threading, tkinter as tk
from tkinter import messagebox, ttk
from pathlib import Path
from typing import Any

from agentchat_tui import App as TuiApp  # only for shared data/model helpers
from agentchat_tui import AgentClient, Config, Message, SessionStore, execute, specs

BG = "#0b0d10"; PANEL = "#12161c"; CARD = "#171c23"; BORDER = "#293241"
TEXT = "#e7edf5"; MUTED = "#7f8b9b"; BLUE = "#4e9cff"; CYAN = "#6de4ff"
GREEN = "#7ee787"; AMBER = "#f2c14e"; MAGENTA = "#d28cff"

class CiprocodeGUI(tk.Tk):
    def __init__(self):
        super().__init__(); self.title("Ciprocode CLI · Coding Agent"); self.geometry("1180x760"); self.minsize(760, 520); self.configure(bg=BG)
        self.cfg = Config.from_env(); self.store = SessionStore(); self.sid = None; self.title_name = "New session"
        self.view: list[Message] = []; self.api: list[dict[str, Any]] = [{"role":"system","content":self.cfg.system}]
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue(); self.busy = False; self.approval: queue.Queue[str] | None = None
        self._build_style(); self._build_ui(); self.after(80, self._poll)

    def _build_style(self):
        style = ttk.Style(self); style.theme_use("clam")
        style.configure("TFrame", background=BG); style.configure("Panel.TFrame", background=PANEL)
        style.configure("TButton", background=CARD, foreground=TEXT, borderwidth=0, padding=(10, 7)); style.map("TButton", background=[("active", BORDER)])
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT, borderwidth=0, rowheight=30)
        style.configure("Treeview.Heading", background=PANEL, foreground=MUTED, borderwidth=0)

    def _build_ui(self):
        self.grid_rowconfigure(1, weight=1); self.grid_columnconfigure(1, weight=1)
        header = tk.Frame(self, bg=BG, height=58); header.grid(row=0, column=0, columnspan=2, sticky="ew"); header.grid_propagate(False)
        tk.Label(header, text="◆", fg=BLUE, bg=BG, font=("Consolas", 16, "bold")).pack(side="left", padx=(22, 7))
        tk.Label(header, text="CIPROCODE", fg=TEXT, bg=BG, font=("Consolas", 15, "bold")).pack(side="left")
        tk.Label(header, text="  CODING AGENT", fg=MUTED, bg=BG, font=("Consolas", 10)).pack(side="left")
        self.model_label = tk.Label(header, text=self.cfg.model, fg=AMBER, bg=BG, font=("Consolas", 10)); self.model_label.pack(side="right", padx=24)
        # Responsive sidebar
        side = tk.Frame(self, bg=PANEL, width=235, highlightbackground=BORDER, highlightthickness=1); side.grid(row=1, column=0, sticky="nsew"); side.grid_propagate(False)
        tk.Label(side, text="WORKSPACE", fg=MUTED, bg=PANEL, font=("Consolas", 9, "bold"), anchor="w").pack(fill="x", padx=16, pady=(20, 7))
        tk.Label(side, text=Path.cwd().name, fg=TEXT, bg=PANEL, font=("Consolas", 10), anchor="w").pack(fill="x", padx=16)
        tk.Button(side, text="＋  New session", command=self.new_session, bg=BLUE, fg="#07101d", activebackground=CYAN, relief="flat", padx=8, pady=8).pack(fill="x", padx=14, pady=18)
        tk.Label(side, text="SAVED SESSIONS", fg=MUTED, bg=PANEL, font=("Consolas", 9, "bold"), anchor="w").pack(fill="x", padx=16)
        self.sessions = ttk.Treeview(side, columns=("title",), show="headings", height=10); self.sessions.heading("title", text="Sessions"); self.sessions.column("title", width=200, anchor="w"); self.sessions.pack(fill="both", expand=True, padx=10, pady=8); self.sessions.bind("<Double-1>", self.load_selected)
        tk.Button(side, text="↻  Reload .env", command=self.reload_config, bg=PANEL, fg=MUTED, activebackground=BORDER, relief="flat").pack(fill="x", padx=14, pady=(4, 16))
        # Main chat column
        main = tk.Frame(self, bg=BG); main.grid(row=1, column=1, sticky="nsew"); main.grid_rowconfigure(0, weight=1); main.grid_columnconfigure(0, weight=1)
        self.chat = tk.Text(main, bg=BG, fg=TEXT, insertbackground=TEXT, relief="flat", wrap="word", padx=28, pady=22, state="disabled", font=("Consolas", 11), spacing3=5)
        scroll = ttk.Scrollbar(main, orient="vertical", command=self.chat.yview); self.chat.configure(yscrollcommand=scroll.set); self.chat.grid(row=0, column=0, sticky="nsew"); scroll.grid(row=0, column=1, sticky="ns")
        self.chat.tag_configure("you_head", foreground=BLUE, font=("Consolas", 10, "bold")); self.chat.tag_configure("agent_head", foreground=GREEN, font=("Consolas", 10, "bold")); self.chat.tag_configure("tool_head", foreground=MAGENTA, font=("Consolas", 10, "bold")); self.chat.tag_configure("body", foreground=TEXT, lmargin1=18, lmargin2=18); self.chat.tag_configure("muted", foreground=MUTED); self.chat.tag_configure("thinking", foreground=AMBER, font=("Consolas", 10, "italic"))
        composer = tk.Frame(main, bg=PANEL, highlightbackground=BLUE, highlightthickness=1); composer.grid(row=1, column=0, columnspan=2, sticky="ew", padx=22, pady=(0, 20)); composer.grid_columnconfigure(0, weight=1)
        self.entry = tk.Text(composer, height=3, bg=PANEL, fg=TEXT, insertbackground=TEXT, relief="flat", wrap="word", font=("Consolas", 11), padx=12, pady=10); self.entry.grid(row=0, column=0, sticky="ew"); self.entry.bind("<Control-Return>", self.send); self.entry.bind("<Return>", lambda e: "break")
        self.send_btn = tk.Button(composer, text="Send  Ctrl+Enter", command=self.send, bg=BLUE, fg="#07101d", activebackground=CYAN, relief="flat", padx=14); self.send_btn.grid(row=0, column=1, padx=12, pady=10)
        self.status = tk.Label(main, text="Ready · Coding agent workspace", bg=BG, fg=MUTED, anchor="w", font=("Consolas", 9)); self.status.grid(row=2, column=0, columnspan=2, sticky="ew", padx=28, pady=(0, 8))
        self._welcome(); self.refresh_sessions()

    def _welcome(self):
        self._clear_chat(); self._add_line("CIPROCODE CODING AGENT", "agent_head"); self._add_line("Inspect directories, trace symbols, debug code, run tests, and explain every step.\n", "body"); self._add_line("Tip  Use Ctrl+Enter to send · /help for commands", "muted")
    def _clear_chat(self): self.chat.configure(state="normal"); self.chat.delete("1.0", "end"); self.chat.configure(state="disabled")
    def _add_line(self, text: str, tag: str = "body"):
        self.chat.configure(state="normal"); self.chat.insert("end", text + "\n", tag); self.chat.configure(state="disabled"); self.chat.see("end")
    def add_message(self, m: Message):
        tag = "you_head" if m.role == "user" else ("tool_head" if m.role == "tool" else "agent_head"); label = "YOU" if m.role == "user" else ("TOOL" if m.role == "tool" else "CIPROCODE")
        self._add_line(f"{label}  ·  {m.timestamp}", tag); self._add_line(m.content, "body")
    def add(self, role: str, content: str, **kw: Any):
        m=Message(role, content, __import__('datetime').datetime.now().strftime("%H:%M"), **kw); self.view.append(m); self.add_message(m)
        if role != "system": self.api.append({"role":role,"content":content, **({"tool_call_id":m.tool_call_id} if m.tool_call_id else {})})
    def set_busy(self, value: bool): self.busy=value; self.send_btn.configure(state="disabled" if value else "normal"); self.entry.configure(state="disabled" if value else "normal")
    def send(self, _event=None):
        if self.busy: return "break"
        text=self.entry.get("1.0","end").strip()
        if not text: return "break"
        self.entry.delete("1.0","end")
        if text.startswith("/"):
            self.add("user", text); self.command(text); return "break"
        self.add("user", text); self.title_name=text[:48]; self.set_busy(True); self.status.configure(text="|  CIPROCODE is thinking…", fg=AMBER); threading.Thread(target=self._agent, daemon=True).start(); return "break"
    def _agent(self):
        try:
            client=AgentClient(self.cfg, self.events); steps=0
            while steps < self.cfg.max_steps:
                result=client.stream_answer(self.api, specs()); msg=result["choices"][0]["message"]; calls=msg.get("tool_calls") or []
                if not calls: self.events.put(("answer", msg.get("content") or "(empty response)")); return
                self.api.append(msg)
                for call in calls:
                    steps += 1; fn=call.get("function",{}); name=fn.get("name",""); args=json.loads(fn.get("arguments") or "{}")
                    self.events.put(("approval", (name,args,call.get("id",""))))
                    answer=self.events.get()[1]
                    out="Denied by user." if answer != "y" else execute(name,args)
                    self.api.append({"role":"tool","tool_call_id":call.get("id",""),"content":out}); self.events.put(("tool", f"{name}: {out[:400]}"))
            self.events.put(("answer", "Stopped: maximum agent steps reached."))
        except Exception as e: self.events.put(("error", f"{type(e).__name__}: {e}"))
    def _poll(self):
        try:
            while True:
                event,data=self.events.get_nowait()
                if event=="answer": self.add("assistant",data); self.status.configure(text="Ready", fg=MUTED); self.set_busy(False); self.save()
                elif event=="token": self.status.configure(text="Receiving response…", fg=AMBER)
                elif event=="approval": self.after(0, self.ask_approval, data)
                elif event=="tool": self.add("tool",data); self.status.configure(text="Tool complete", fg=MAGENTA)
                elif event=="error": self.add("assistant","Error: "+data); self.status.configure(text="Error", fg="#ff7070"); self.set_busy(False)
        except queue.Empty: pass
        self.after(80, self._poll)
    def ask_approval(self, data):
        name,args,_=data; answer=messagebox.askyesno("Ciprocode tool approval", f"Allow tool: {name}?\n\n{json.dumps(args, indent=2)}", parent=self); self.events.put(("approval_result", "y" if answer else "n")); self.status.configure(text=f"Running tool: {name}", fg=AMBER)
    def save(self):
        if self.view: self.sid=self.store.save(self.title_name,self.view,self.sid); self.refresh_sessions()
    def refresh_sessions(self):
        for x in self.sessions.get_children(): self.sessions.delete(x)
        for sid,title,updated in self.store.list(): self.sessions.insert("","end",iid=str(sid),values=(title[:28],))
    def new_session(self): self.view=[]; self.api=[{"role":"system","content":self.cfg.system}]; self.sid=None; self.title_name="New session"; self._welcome(); self.status.configure(text="New session", fg=MUTED)
    def load_selected(self,_=None):
        sel=self.sessions.selection()
        if not sel:return
        self.view=self.store.load(int(sel[0])); self.api=[{"role":"system","content":self.cfg.system}]+[{"role":m.role,"content":m.content} for m in self.view]; self.sid=int(sel[0]); self._clear_chat(); [self.add_message(m) for m in self.view]
    def reload_config(self): self.cfg=Config.from_env(); self.model_label.configure(text=self.cfg.model); self.status.configure(text="Configuration reloaded from .env", fg=MUTED)
    def command(self,text):
        if text.strip()=="/clear": self.new_session()
        elif text.strip()=="/help": self.add("assistant", "/clear  new session\n/sessions  show saved sessions\n/model NAME  change model\n/approve  tools require approval in GUI")
        else: self.add("assistant", "Unknown command. Try /help.")

if __name__ == "__main__": CiprocodeGUI().mainloop()
