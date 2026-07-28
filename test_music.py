"""Self-check for the queue logic. Run: python test_music.py"""
from packages.music import Player


def track(name):
    return {"title": name, "url": name}


p = Player()
p.queue = [track(c) for c in "abcd"]

# natural advance
p.index = 0
assert p.step() == 1

# /back via jump
p.index = 2
p.jump = 1
assert p.step() == 1
assert p.jump is None  # jump consumed

# end of queue
p.index = 3
assert p.step() is None

# move within upcoming only
p.index = 1
assert p.move(2, 1)  # c <-> d
assert [t["title"] for t in p.queue] == ["a", "b", "d", "c"]
assert not p.move(2, -1)  # would move into current slot
assert not p.move(3, 1)  # out of bounds

# remove upcoming only
assert not p.remove(1)  # current track
assert p.remove(3)
assert [t["title"] for t in p.queue] == ["a", "b", "d"]

# clear keeps current
p.clear()
assert [t["title"] for t in p.queue] == ["b"]
assert p.index == 0

# clear when idle
p2 = Player()
p2.clear()
assert p2.queue == [] and p2.index == -1 and p2.current is None

print("ok")
