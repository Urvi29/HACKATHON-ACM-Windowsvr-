"""Live index updates: watchdog (inotify) events -> debounced batch -> crawler.index_path.
If inotify cannot watch the tree (watch limit), falls back to a periodic incremental rescan."""
import os
import threading
import time

from . import crawler, db, embed, extract
from .util import IGNORE_DIRS_LOWER


def lower_priority():
    try:
        os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)
    except Exception:
        pass


class Watcher:
    def __init__(self, src, debounce: float = 1.5, rescan_every: float = 120):
        self.src, self.debounce, self.rescan_every = src, debounce, rescan_every
        self.pending: set[str] = set()
        self.lock = threading.Lock()
        self.stop_evt = threading.Event()
        self.mode = "stopped"
        self.obs = None
        self.thread = None

    def _ignored(self, p: str) -> bool:
        data_dir = str(db.DB_PATH.parent)
        if p == data_dir or p.startswith(data_dir + os.sep):
            return True  # the app's own data folder (database, logs, synthetic test data) is never indexed
        if any(part.lower() in IGNORE_DIRS_LOWER or (part.startswith('.') and part not in ('', '.', '..')) for part in p.split(os.sep)):
            return True
        return os.path.dirname(p) == str(db.DB_PATH.parent) and os.path.basename(p).startswith(db.DB_PATH.name)

    def _on_event(self, event):
        paths = [getattr(event, "src_path", None), getattr(event, "dest_path", None)]
        with self.lock:
            for p in paths:
                if p and not self._ignored(os.fsdecode(p)):
                    self.pending.add(os.fsdecode(p))

    def start(self):
        if not getattr(self.src, "watchable", False):
            self.mode = "polling"  # remote sources: periodic rescan only
        else:
            try:
                from watchdog.events import FileSystemEventHandler
                from watchdog.observers import Observer

                outer = self

                class H(FileSystemEventHandler):
                    def on_any_event(self, event):
                        if event.event_type in ("created", "modified", "deleted", "moved"):
                            outer._on_event(event)

                self.obs = Observer()
                self.obs.schedule(H(), self.src.root, recursive=True)
                self.obs.start()
                self.mode = "inotify"
            except Exception:
                self.mode = "polling"
        self.thread = threading.Thread(target=self._loop, daemon=True, name=f"watch-{self.src.id}")
        self.thread.start()

    def stop(self):
        self.stop_evt.set()
        if self.obs:
            self.obs.stop()

    def _loop(self):
        lower_priority()
        conn = db.connect()
        last_scan = time.time()
        while not self.stop_evt.wait(self.debounce):
            with self.lock:
                batch, self.pending = self.pending, set()
            if batch:
                for p in sorted(batch):
                    try:
                        crawler.index_path(conn, self.src, p)
                    except Exception:
                        pass
                extract.run_pending(conn, limit=500, should_stop=self.stop_evt.is_set)
                embed.run_pending(conn, limit=200, should_stop=self.stop_evt.is_set)
            if self.mode == "polling" and time.time() - last_scan > self.rescan_every:
                crawler.scan(conn, self.src, should_stop=self.stop_evt.is_set)
                extract.run_pending(conn, should_stop=self.stop_evt.is_set)
                embed.run_pending(conn, should_stop=self.stop_evt.is_set)
                last_scan = time.time()
