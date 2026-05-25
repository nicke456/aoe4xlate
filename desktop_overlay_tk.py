"""
Tkinter-based always-on-top transparent overlay.
GDI rendering (used by tkinter) supports Windows color-key transparency natively,
unlike WebView2/DirectComposition which ignores SetLayeredWindowAttributes.
"""

import asyncio
import json
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path

POSITION_FILE = Path(__file__).parent / '.overlay_position.json'

TRANSPARENT  = '#000001'  # registered as color key — kept to avoid accidental matches
BG_COLOR     = '#020202'  # window fill: not keyed, so whole area receives mouse events;
                           # at MIN_ALPHA it's imperceptibly faint
MSG_BG       = '#111111'  # message bubble background
DRAG_BG      = '#1c1c1c'  # drag / title bar
GOLD         = '#c8a84b'
BLUE         = '#5aa0d6'
GREY_BORDER  = '#555555'
TEXT_DIM     = '#888888'
TEXT_MAIN    = '#f0e6c8'

HOLD_MS       = 5000   # stay fully visible after last message
FADE_MS       = 2000   # total fade-out duration
FADE_STEPS    = 40     # steps in fade-out (40 × 50 ms = 2 s)
FADE_INTERVAL = FADE_MS // FADE_STEPS
MIN_ALPHA      = 0.30   # never fade below this — keeps the drag bar findable
HOVER_ALPHA    = 0.92   # opacity when hover is activated
HOVER_DWELL_MS = 750    # mouse must be still for this long before hover activates

CHANNEL_LABELS = {0: 'POST', 16: 'ALL'}


def _load_position() -> dict:
    try:
        return json.loads(POSITION_FILE.read_text())
    except Exception:
        return {}


def _save_position(x: int, y: int):
    try:
        POSITION_FILE.write_text(json.dumps({'x': x, 'y': y}))
    except Exception:
        pass


class OverlayWindow:
    def __init__(self, ws_port: int = 8765, max_messages: int = 30, hotkey: str = "",
                 fade_enabled: bool = False, fade_delay: float = 5.0,
                 fade_opacity: float = 0.30):
        self.ws_port        = ws_port
        self.max_messages   = max_messages
        self._frames: list[tk.Frame] = []
        self._hold_job      = None
        self._fade_job      = None
        self._hover_job     = None
        self._hidden        = False
        self._fade_enabled  = fade_enabled
        self._fade_delay_ms = int(fade_delay * 1000)
        self._fade_opacity  = fade_opacity
        # When fade is off the overlay is always fully visible; when on, start
        # at the fade floor so it only lights up when a message arrives.
        self._alpha = fade_opacity if fade_enabled else 1.0

        root = self.root = tk.Tk()
        root.overrideredirect(True)            # frameless
        root.wm_attributes('-topmost', True)
        root.wm_attributes('-transparentcolor', TRANSPARENT)
        root.wm_attributes('-alpha', self._alpha)
        root.configure(bg=BG_COLOR)

        screen_h = root.winfo_screenheight()
        saved = _load_position()
        x = saved.get('x', 20)
        y = saved.get('y', max(0, screen_h - 420))
        root.geometry(f'390x350+{x}+{y}')

        self._build_ui()
        root.bind('<Enter>',  self._on_motion)   # treat entry same as motion
        root.bind('<Motion>', self._on_motion)
        root.bind('<Leave>',  self._on_leave)
        root.bind('<ButtonRelease-1>', self._on_drag_end)
        root.protocol('WM_DELETE_WINDOW', self._on_close)
        threading.Thread(target=self._ws_thread, daemon=True).start()

        if hotkey:
            self._start_hotkey(hotkey)

    # ── UI construction ────────────────────────────────────────────────────

    def _build_ui(self):
        outer = tk.Frame(self.root, bg=BG_COLOR, padx=8, pady=4)
        outer.pack(fill=tk.BOTH, expand=True)

        # Title / drag bar
        drag = tk.Frame(outer, bg=DRAG_BG, height=26)
        drag.pack(fill=tk.X, pady=(0, 6))
        drag.pack_propagate(False)

        tk.Label(drag, text='⚔  AoE4 Chat Translator', fg=GOLD, bg=DRAG_BG,
                 font=('Segoe UI', 9, 'bold'), padx=8).pack(side=tk.LEFT, pady=4)

        close = tk.Label(drag, text='✕', fg='#555555', bg=DRAG_BG,
                         font=('Segoe UI', 10), padx=8, cursor='hand2')
        close.pack(side=tk.RIGHT, pady=0)
        close.bind('<Enter>',    lambda _: close.configure(fg='#cc4444'))
        close.bind('<Leave>',    lambda _: close.configure(fg='#555555'))
        close.bind('<Button-1>', lambda _: self._on_close())

        for w in drag.winfo_children():
            if w is not close:
                w.bind('<Button-1>', self._drag_start)
                w.bind('<B1-Motion>', self._drag_move)
        drag.bind('<Button-1>', self._drag_start)
        drag.bind('<B1-Motion>', self._drag_move)

        # Canvas = fixed-height viewport; content frame inside can grow taller.
        # yview_moveto(1.0) keeps the view pinned to the bottom so newest messages
        # are always visible and old ones disappear above the top edge.
        self._canvas = tk.Canvas(
            outer, bg=BG_COLOR, highlightthickness=0, bd=0,
            height=310, width=358,
        )
        self._canvas.pack(fill=tk.X)

        self.chat_frame = tk.Frame(self._canvas, bg=BG_COLOR)
        self._canvas_win = self._canvas.create_window(
            0, 0, anchor='nw', window=self.chat_frame,
        )

        def _on_content_resize(event):
            self._canvas.configure(scrollregion=self._canvas.bbox('all'))
            self._canvas.yview_moveto(1.0)  # always show the bottom (newest)

        def _on_canvas_resize(event):
            self._canvas.itemconfig(self._canvas_win, width=event.width)

        self.chat_frame.bind('<Configure>', _on_content_resize)
        self._canvas.bind('<Configure>', _on_canvas_resize)

    def _on_motion(self, event):
        """Any mouse movement restarts the dwell timer (only relevant when fade is on)."""
        if not self._fade_enabled:
            return
        if self._hover_job:
            self.root.after_cancel(self._hover_job)
        self._hover_job = self.root.after(HOVER_DWELL_MS, self._activate_hover)

    def _activate_hover(self):
        """Dwell timer fired — mouse has been still long enough."""
        self._hover_job = None
        if not self._fade_enabled:
            return  # always fully visible; nothing to restore
        if self._fade_job:
            self.root.after_cancel(self._fade_job)
            self._fade_job = None
        if self._alpha < HOVER_ALPHA:
            self._alpha = HOVER_ALPHA
            self.root.wm_attributes('-alpha', HOVER_ALPHA)

    def _on_leave(self, event):
        """Mouse left — cancel dwell, resume fade if hold period has expired."""
        # tkinter fires <Leave> on the root when the pointer moves into a child
        # widget (each widget is its own HWND on Windows).  Ignore those crossings
        # by checking whether the pointer is still within the window's bounds.
        rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
        rw, rh = self.root.winfo_width(), self.root.winfo_height()
        if rx <= event.x_root < rx + rw and ry <= event.y_root < ry + rh:
            return  # still inside — this was just a root→child crossing
        if self._hover_job:
            self.root.after_cancel(self._hover_job)
            self._hover_job = None
        if self._fade_enabled and not self._hold_job:
            self._fade_start()

    def _drag_start(self, event):
        self._ox = event.x_root - self.root.winfo_x()
        self._oy = event.y_root - self.root.winfo_y()

    def _drag_move(self, event):
        self.root.geometry(f'+{event.x_root - self._ox}+{event.y_root - self._oy}')

    def _on_drag_end(self, event):
        _save_position(self.root.winfo_x(), self.root.winfo_y())

    def _on_close(self):
        _save_position(self.root.winfo_x(), self.root.winfo_y())
        self.root.destroy()

    # ── Global hotkey ──────────────────────────────────────────────────────

    def _start_hotkey(self, hotkey_str: str):
        """Register a global hotkey in a background thread (keyboard hooks need a thread)."""
        def _register():
            try:
                import keyboard
                keyboard.add_hotkey(hotkey_str, self._toggle_hidden)
                print(f"[overlay] Hotkey registered: {hotkey_str} — show/hide overlay")
                keyboard.wait()  # block this thread so the hook stays alive
            except Exception as e:
                print(f"[overlay] Hotkey registration failed ({hotkey_str}): {e}")

        threading.Thread(target=_register, daemon=True).start()

    def _toggle_hidden(self):
        """Called from the keyboard hook thread — post to the GUI thread."""
        self.root.after(0, self._do_toggle)

    def _do_toggle(self):
        """Runs on the GUI thread: withdraw or restore the window."""
        if self._hidden:
            self._hidden = False
            self.root.deiconify()
            self.root.wm_attributes('-alpha', self._alpha)
        else:
            self._hidden = True
            self.root.withdraw()

    # ── Message rendering ──────────────────────────────────────────────────

    def add_message(self, data: dict):
        """Thread-safe: schedules on the GUI thread via after()."""
        self.root.after(0, self._add_message_ui, data)

    def _add_message_ui(self, data: dict):
        player  = data.get('player_name') or str(data.get('sender_id', '?'))
        raw     = data.get('raw_message', '')
        xlat    = data.get('translated')
        is_own  = data.get('is_own', False)
        ch      = data.get('channel', 16)
        try:
            ts = datetime.fromisoformat(data.get('timestamp', ''))
            ts_text = ts.strftime('%H:%M')
        except Exception:
            ts_text = ''

        border   = BLUE if is_own else (GOLD if xlat else GREY_BORDER)
        ch_label = CHANNEL_LABELS.get(ch, f'CH{ch}')

        # Outer frame provides the colored left-border effect
        outer_f = tk.Frame(self.chat_frame, bg=border)
        outer_f.pack(fill=tk.X, pady=2)

        inner_f = tk.Frame(outer_f, bg=MSG_BG, padx=8, pady=5)
        inner_f.pack(fill=tk.X, padx=(3, 0))  # 3 px left = border width

        # Header: player name + channel + timestamp
        hdr = tk.Frame(inner_f, bg=MSG_BG)
        hdr.pack(fill=tk.X)
        tk.Label(hdr, text=player, fg=border, bg=MSG_BG,
                 font=('Segoe UI', 9, 'bold')).pack(side=tk.LEFT)
        tk.Label(hdr, text=f'  {ch_label}', fg=TEXT_DIM, bg=MSG_BG,
                 font=('Segoe UI', 7)).pack(side=tk.LEFT)
        tk.Label(hdr, text=ts_text, fg='#444444', bg=MSG_BG,
                 font=('Segoe UI', 7)).pack(side=tk.RIGHT)

        # Message body
        if xlat:
            tk.Label(inner_f, text=raw, fg=TEXT_DIM, bg=MSG_BG,
                     font=('Segoe UI', 9), wraplength=330,
                     justify=tk.LEFT, anchor='w').pack(fill=tk.X)
            tk.Label(inner_f, text=f'→ {xlat}', fg=TEXT_MAIN, bg=MSG_BG,
                     font=('Segoe UI', 10), wraplength=330,
                     justify=tk.LEFT, anchor='w').pack(fill=tk.X)
        else:
            tk.Label(inner_f, text=raw, fg=TEXT_MAIN, bg=MSG_BG,
                     font=('Segoe UI', 10), wraplength=330,
                     justify=tk.LEFT, anchor='w').pack(fill=tk.X)

        self._frames.append(outer_f)
        while len(self._frames) > self.max_messages:
            self._frames.pop(0).destroy()

        self._show()

    # ── Fade logic ─────────────────────────────────────────────────────────

    def _show(self):
        """Snap to fully visible; when fade is on, restart the hold + fade timer."""
        if self._hidden:
            return
        if self._hold_job:
            self.root.after_cancel(self._hold_job)
            self._hold_job = None
        if self._fade_job:
            self.root.after_cancel(self._fade_job)
            self._fade_job = None
        self._alpha = 1.0
        self.root.wm_attributes('-alpha', 1.0)
        if self._fade_enabled:
            self._hold_job = self.root.after(self._fade_delay_ms, self._fade_start)

    def _fade_start(self):
        self._hold_job = None  # hold timer has fired; clear stale reference
        step = 1.0 / FADE_STEPS
        self._fade_tick(step)

    def _fade_tick(self, step: float):
        self._alpha = max(self._fade_opacity, self._alpha - step)
        self.root.wm_attributes('-alpha', self._alpha)
        if self._alpha > self._fade_opacity:
            self._fade_job = self.root.after(FADE_INTERVAL, self._fade_tick, step)
        else:
            self._fade_job = None  # fade complete; clear so _show() doesn't cancel a ghost ID

    # ── WebSocket client ───────────────────────────────────────────────────

    def _ws_thread(self):
        asyncio.run(self._ws_loop())

    async def _ws_loop(self):
        import websockets
        while True:
            try:
                async with websockets.connect(f'ws://localhost:{self.ws_port}') as ws:
                    async for raw in ws:
                        try:
                            data = json.loads(raw)
                            if data.get('type') == 'chat':
                                self.add_message(data)
                        except Exception:
                            pass
            except Exception:
                await asyncio.sleep(2)

    def run(self):
        import signal
        # tkinter's mainloop doesn't yield to Python's SIGINT handler on Windows.
        # Register a handler that posts destroy() to the event queue, and poll
        # every 200 ms so the signal gets a chance to fire.
        signal.signal(signal.SIGINT, lambda *_: self.root.after(0, self.root.destroy))
        self._poll_sigint()
        self.root.mainloop()

    def _poll_sigint(self):
        # Re-schedule itself; the mere act of calling after() lets Python check signals.
        self.root.after(200, self._poll_sigint)
