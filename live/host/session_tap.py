"""SessionTap：内部 FrameSink，只收事件不收帧，把会话事件转发为总线主题 session.event（主持人据此知道一句说完/被打断）。"""
from __future__ import annotations

from live.sinks import FrameSink

FORWARD_TYPES = {"speak_done", "interrupted", "playback", "speak_queued", "error", "closed"}


class SessionTap(FrameSink):
    def __init__(self, bus, session_id: str):
        self.bus = bus
        self.session_id = session_id

    def on_frame(self, frame) -> None:
        pass

    def on_event(self, event: dict) -> None:
        if event.get("type") in FORWARD_TYPES:
            self.bus.publish("session.event", dict(event, session_id=event.get("session_id") or self.session_id))
