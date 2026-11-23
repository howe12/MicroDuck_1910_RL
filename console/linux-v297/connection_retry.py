"""A failed connection stays stopped until an explicit new connection session.

The historical class name is retained for callers, but there is no retry timer
or automatic reset after a stable connection.
"""
import time


class RetryStopped(OSError):
    pass


class ConnectionRetry:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.failures = 0
        self.until = 0
        self.connected_at = None

    @property
    def exhausted(self):return self.failures >= 1

    def connected(self):
        if self.connected_at is None:self.connected_at = self.clock()

    def failed(self):
        self.connected_at = None
        self.failures += 1
        return 0

    def remaining(self):return 0

    def message(self):
        return '连接已停止；请点击顶部 SSH 连接重新连接'

    def check(self):
        if self.exhausted:raise RetryStopped(self.message())
