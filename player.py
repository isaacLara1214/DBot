"""Pure queue logic, no Discord dependency so it stays testable.

Lives at the top level rather than in packages/ because every module in
packages/ is auto-loaded as a discord.py extension and must define setup().
"""

import asyncio


class Player:
    """Per-guild queue. Played tracks stay in the list so /back and /replay work;
    index points at the current track."""

    def __init__(self):
        self.queue: list[dict] = []
        self.index = -1
        self.jump: int | None = None
        # Serialises playback starts: resolving a stream URL awaits, and two
        # overlapping starts would both reach vc.play(). See Music.play_index.
        self.lock = asyncio.Lock()

    def find(self, track_id: str) -> int | None:
        """Current index of a track by id. Positions shift as the queue is
        edited, so UI callbacks must resolve an id rather than trust an index."""
        return next(
            (i for i, t in enumerate(self.queue) if t.get("id") == track_id), None
        )

    @property
    def current(self):
        return self.queue[self.index] if 0 <= self.index < len(self.queue) else None

    def step(self) -> int | None:
        """Next index to play when a track ends (honours a pending jump)."""
        i = self.jump if self.jump is not None else self.index + 1
        self.jump = None
        return i if 0 <= i < len(self.queue) else None

    def clear(self):
        cur = self.current
        self.queue = [cur] if cur else []
        self.index = 0 if cur else -1
        self.jump = None

    def move(self, i: int, delta: int) -> bool:
        j = i + delta
        if self.index < min(i, j) and max(i, j) < len(self.queue):
            self.queue[i], self.queue[j] = self.queue[j], self.queue[i]
            return True
        return False

    def remove(self, i: int) -> bool:
        if self.index < i < len(self.queue):
            self.queue.pop(i)
            return True
        return False
