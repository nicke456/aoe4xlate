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

TRANSPARENT  = '#000001'  # color-keyed to a hole by Windows; must not appear in UI content
MSG_BG       = '#111111'  # message bubble background (must differ from TRANSPARENT)
DRAG_BG      = '#161616'  # drag bar — non-transparent so it can receive mouse events
GOLD         = '#c8a84b'
BLUE         = '#5aa0d6'
GREY_BORDER  = '#555555'
TEXT_DIM     = '#888888'
TEXT_MAIN    = '#f0e6c8'

HOLD_MS       = 5000   # stay fully visible after last message
FADE_MS       = 2000   # total fade-out duration
FADE_STEPS    = 40     # steps in fade-out (40 × 50 ms = 2 s)
FADE_INTERVAL = FADE_MS // FADE_STEPS
MIN_ALPHA     = 0.30   # never fade below this — keeps the drag bar findable
HOVER_ALPHA   = 0.92   # snap to this when mouse enters the window

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
    def __init__(self, ws_port: int = 8765, max_messages: int = 30):
        self.ws_port      = ws_port
        self.max_messages = max_messages
        self._frames: list[tk.Frame] = []
        self._hold_job = None
        self._fade_job = None
        self._alpha    = MIN_ALPHA

        root = self.root = tk.Tk()
        root.overrideredirect(True)            # frameless
        root.wm_attributes('-topmost', True)
        root.wm_attributes('-transparentcolor', TRANSPARENT)
        root.wm_attributes('-alpha', MIN_ALPHA)  # barely visible — shows drag bar location
        root.configure(bg=TRANSPARENT)

        screen_h = root.winfo_screenheight()
        saved = _load_position()
        x = saved.get('x', 20)
        y = saved.get('y', max(0, screen_h - 420))
        root.geometry(f'520x350+{x}+{y}')

        self._build_ui()
        root.bind('<Enter>', self._on_enter)
        root.bind('<Leave>', self._on_leave)
        root.bind('<ButtonRelease-1>', self._on_drag_end)
        root.protocol('WM_DELETE_WINDOW', self._on_close)
        threading.Thread(target=self._ws_thread, daemon=True).start()

    # ── UI construction ────────────────────────────────────────────────────

    def _build_ui(self):
        outer = tk.Frame(self.root, bg=TRANSPARENT, padx=8, pady=4)
        outer.pack(fill=tk.BOTH, expand=True)

        # Drag bar — must NOT use TRANSPARENT bg or it won't receive mouse clicks
        drag = tk.Frame(outer, bg=DRAG_BG, height=14)
        drag.pack(fill=tk.X, pady=(0, 4))
        drag.pack_propagate(False)

        tk.Label(drag, text='AOE4 CHAT', fg='#333333', bg=DRAG_BG,
                 font=('Segoe UI', 7), padx=4).pack(side=tk.LEFT, pady=1)

        close = tk.Label(drag, text='✕', fg='#333333', bg=DRAG_BG,
                         font=('Segoe UI', 9), padx=4, cursor='hand2')
        close.pack(side=tk.RIGHT)
        close.bind('<Button-1>', lambda _: self.root.destroy())

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
            outer, bg=TRANSPARENT, highlightthickness=0, bd=0,
            height=310, width=504,
        )
        self._canvas.pack(fill=tk.X)

        self.chat_frame = tk.Frame(self._canvas, bg=TRANSPARENT)
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

    def _on_enter(self, event):
        """Mouse entered a non-transparent part of the window — boost opacity."""
        if self._fade_job:
            self.root.after_cancel(self._fade_job)
            self._fade_job = None
        if self._alpha < HOVER_ALPHA:
            self._alpha = HOVER_ALPHA
            self.root.wm_attributes('-alpha', HOVER_ALPHA)

    def _on_leave(self, event):
        """Mouse left the window — resume fade if the hold period has already expired."""
        if not self._hold_job:
            # Hold already fired; fade back down from wherever we are
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
                     font=('Segoe UI', 9), wraplength=460,
                     justify=tk.LEFT, anchor='w').pack(fill=tk.X)
            tk.Label(inner_f, text=f'→ {xlat}', fg=TEXT_MAIN, bg=MSG_BG,
                     font=('Segoe UI', 10), wraplength=460,
                     justify=tk.LEFT, anchor='w').pack(fill=tk.X)
        else:
            tk.Label(inner_f, text=raw, fg=TEXT_MAIN, bg=MSG_BG,
                     font=('Segoe UI', 10), wraplength=460,
                     justify=tk.LEFT, anchor='w').pack(fill=tk.X)

        self._frames.append(outer_f)
        while len(self._frames) > self.max_messages:
            self._frames.pop(0).destroy()

        self._show()

    # ── Fade logic ─────────────────────────────────────────────────────────

    def _show(self):
        """Snap to fully visible and restart the hold + fade timer."""
        if self._hold_job:
            self.root.after_cancel(self._hold_job)
        if self._fade_job:
            self.root.after_cancel(self._fade_job)
        self._alpha = 1.0
        self.root.wm_attributes('-alpha', 1.0)
        self._hold_job = self.root.after(HOLD_MS, self._fade_start)

    def _fade_start(self):
        step = 1.0 / FADE_STEPS
        self._fade_tick(step)

    def _fade_tick(self, step: float):
        self._alpha = max(0.0, self._alpha - step)
        self.root.wm_attributes('-alpha', self._alpha)
        if self._alpha > MIN_ALPHA:
            self._fade_job = self.root.after(FADE_INTERVAL, self._fade_tick, step)

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
