"""Short-lived aggregate reports in RAM only; bounded and isolated per connection."""

from collections import OrderedDict
from copy import deepcopy
from threading import RLock
from time import monotonic


class ReportCache:
    def __init__(self, ttl=300, capacity=32, clock=monotonic):
        self.ttl, self.capacity, self.clock = ttl, capacity, clock
        self.entries = OrderedDict()
        self.lock = RLock()

    @staticmethod
    def key(chat_id, connection, today, view):
        return (
            chat_id,
            connection["generation"],
            connection["counter_id"],
            connection["counter_name"],
            connection["goal_ids"],
            connection.get("visible_sources"),
            today.isoformat(),
            view,
        )

    def prune(self):
        with self.lock:
            now = self.clock()
            for key, (deadline, _) in list(self.entries.items()):
                if deadline <= now:
                    del self.entries[key]

    def get(self, key):
        with self.lock:
            self.prune()
            item = self.entries.get(key)
            return deepcopy(item[1]) if item else None

    def put(self, key, data):
        with self.lock:
            self.prune()
            deadline = self.entries[key][0] if key in self.entries else self.clock() + self.ttl
            self.entries[key] = (deadline, deepcopy(data))
            self.entries.move_to_end(key)
            while len(self.entries) > self.capacity:
                self.entries.popitem(last=False)

    def drop(self, chat_id):
        with self.lock:
            for key in list(self.entries):
                if key[0] == chat_id:
                    del self.entries[key]
