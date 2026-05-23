"""
Log file watcher: tails warnings.log and emits parsed chat events.
"""

import re
import os
import time
from dataclasses import dataclass
from typing import Callable, Optional

# Matches the WebSocket MatchReceivedChatMessage line — the single canonical source of truth.
# Format: message=[0,"MatchReceivedChatMessage",LOCAL_ID,[SENDER_ID,"raw","filtered",CHANNEL,MATCH_ID]]
_WS_RE = re.compile(
    r'MatchReceivedChatMessage",(\d+),\[(\d+),"((?:[^"\\]|\\.)*)","((?:[^"\\]|\\.)*)",(\d+),(\d+)\]'
)


@dataclass
class ChatMessage:
    local_player_id: int
    sender_id: int
    raw_message: str
    filtered_message: str
    channel: int
    match_id: int
    is_own: bool  # sender_id == local_player_id


def _parse_line(line: str) -> Optional[ChatMessage]:
    m = _WS_RE.search(line)
    if not m:
        return None
    local_id = int(m.group(1))
    sender_id = int(m.group(2))
    raw = m.group(3)
    filtered = m.group(4)
    channel = int(m.group(5))
    match_id = int(m.group(6))
    return ChatMessage(
        local_player_id=local_id,
        sender_id=sender_id,
        raw_message=raw,
        filtered_message=filtered,
        channel=channel,
        match_id=match_id,
        is_own=(sender_id == local_id),
    )


class LogWatcher:
    """
    Polls warnings.log for new lines and calls `callback` for each parsed ChatMessage.
    Starts reading from the end of the file so it only picks up messages from the
    current session (not replaying the whole log on startup).
    """

    def __init__(
        self,
        log_path: str,
        callback: Callable[[ChatMessage], None],
        poll_interval: float = 0.25,
        watch_channels: Optional[list] = None,
    ):
        self.log_path = log_path
        self.callback = callback
        self.poll_interval = poll_interval
        self.watch_channels = set(watch_channels) if watch_channels else set()
        self._pos = 0
        self._inode = None  # for detecting log rotation
        self._running = False

    def _open_at_end(self):
        f = open(self.log_path, "r", encoding="utf-8", errors="replace")
        f.seek(0, 2)  # seek to end
        self._pos = f.tell()
        try:
            st = os.stat(self.log_path)
            self._inode = st.st_ino
        except Exception:
            self._inode = None
        return f

    def _check_rotated(self) -> bool:
        try:
            st = os.stat(self.log_path)
            return self._inode is not None and st.st_ino != self._inode
        except Exception:
            return False

    def run(self):
        """Block and tail the log file, emitting events via callback."""
        self._running = True

        while not os.path.exists(self.log_path):
            print(f"[watcher] Waiting for log file: {self.log_path}")
            time.sleep(2)

        f = self._open_at_end()
        print(f"[watcher] Watching {self.log_path}")

        try:
            while self._running:
                if self._check_rotated():
                    f.close()
                    f = self._open_at_end()
                    print("[watcher] Log rotated, re-opened.")

                line = f.readline()
                if line:
                    self._pos = f.tell()
                    msg = _parse_line(line)
                    if msg is not None:
                        if not self.watch_channels or msg.channel in self.watch_channels:
                            self.callback(msg)
                else:
                    time.sleep(self.poll_interval)
        finally:
            f.close()

    def stop(self):
        self._running = False
