"""User-owned pause of robot data connections, independent of management SSH."""
import threading
import socket
import time


class ManualRobotdStop:
    def __init__(self):
        self.condition = threading.Condition()
        self.paused = False
        self.revision = 0
        self.stop_revision = 0
        self.peers = set()

    def is_set(self):
        with self.condition:
            return self.paused

    def set(self):
        with self.condition:
            self.paused = True
            self.revision += 1
            self.stop_revision += 1
            peers = tuple(self.peers)
            self.condition.notify_all()
        for peer in peers:
            try: peer.shutdown(socket.SHUT_RDWR)
            except (OSError, AttributeError): pass
            try: peer.close()
            except OSError: pass

    def clear(self):
        # An explicit resume permits one new connection attempt.
        with self.condition:
            self.paused = False
            self.revision += 1
            self.condition.notify_all()

    def wait_running(self, stop):
        with self.condition:
            while self.paused and not stop.is_set():
                self.condition.wait(.2)
            return not stop.is_set()

    def wait_changed(self, revision, stop):
        with self.condition:
            while self.revision == revision and not stop.is_set():
                self.condition.wait(.2)
            return not stop.is_set()

    def wait_delay(self, seconds, revision, stop):
        """Interrupt a planned service-handoff delay on manual stop/resume."""
        deadline = time.monotonic() + seconds
        with self.condition:
            while self.revision == revision and not self.paused and not stop.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0: break
                self.condition.wait(min(.2, remaining))
            return not stop.is_set()

    def attach(self, peer):
        return self.attach_epoch(peer) is not None

    def attach_epoch(self, peer):
        """Atomically own a peer and record which actual pause may close it."""
        with self.condition:
            if not self.paused:
                self.peers.add(peer)
                return self.stop_revision
        peer.close()
        return None

    def detach(self, peer):
        with self.condition:
            self.peers.discard(peer)


class RobotdRestart:
    """A user restart retires only the data peers it actually owns."""
    def __init__(self):
        self.condition = threading.Condition()
        self.active = False
        self.peers = {}

    def is_set(self):
        with self.condition:
            return self.active

    def set(self):
        with self.condition:
            self.active = True
            peers = tuple(self.peers.items())
            for _, retired in peers: retired.set()
        for peer, _ in peers:
            try: peer.close()
            except OSError: pass

    def clear(self):
        with self.condition:
            self.active = False
            self.condition.notify_all()

    def wait_running(self, stop):
        with self.condition:
            while self.active and not stop.is_set():
                self.condition.wait(.2)
            return not stop.is_set()

    def attach(self, peer):
        with self.condition:
            if not self.active:
                retired = threading.Event()
                self.peers[peer] = retired
                return retired
        peer.close()
        return None

    def detach(self, peer):
        with self.condition:
            self.peers.pop(peer, None)
