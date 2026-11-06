"""Logging setup with an in-memory ring buffer for the live log page."""

import logging
from collections import deque

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


class RingBuffer(logging.Handler):
    def __init__(self, size: int = 2000):
        super().__init__()
        self.lines: deque[tuple[int, str]] = deque(maxlen=size)
        self.seq = 0
        self.setFormatter(logging.Formatter(FORMAT, "%Y-%m-%d %H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        self.seq += 1
        self.lines.append((self.seq, self.format(record)))

    def since(self, seq: int) -> list[tuple[int, str]]:
        return [x for x in self.lines if x[0] > seq]


BUFFER = RingBuffer()


def setup(level: str = "INFO") -> None:
    root = logging.getLogger()
    if BUFFER not in root.handlers:
        logging.basicConfig(format=FORMAT, level=level)
        root.addHandler(BUFFER)
    set_level(level)
    # these log request URLs (which can carry auth tokens) or are very chatty
    for name in ("httpx", "httpcore", "botocore", "boto3", "s3transfer", "urllib3", "websockets", "apscheduler"):
        logging.getLogger(name).setLevel(logging.WARNING)


def set_level(level: str) -> None:
    logging.getLogger().setLevel(level)
