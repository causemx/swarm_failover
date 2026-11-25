
from __future__ import annotations

import asyncio
import time

from models import Envelope, MsgType


class GcsRelay:
    def __init__(self, node_id: int, net):
        self.node_id = node_id
        self.net = net
        self._seq = 0

    def emit(self, event_type: str, reason: str, term: int) -> None:
        """Sync-looking convenience wrapper; schedules the actual async send
        so callers in hot paths (state transitions) don't need to await it."""
        self._seq += 1
        env = Envelope(
            msg_type=MsgType.SWARM_LOG.value,
            node_id=self.node_id,
            term=term,
            seq=self._seq,
            sent_at_mono=time.monotonic(),
            payload={
                "event_type": event_type,
                "reason": reason,
                "term": term,
                "local_ts_mono": time.monotonic(),
            },
        )
        asyncio.get_event_loop().create_task(self.net.send(env))
