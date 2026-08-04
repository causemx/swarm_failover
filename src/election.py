"""
Pure Bully-election state machine. Depends only on `network` (an object with
async send()/recv()) -- no MAVSDK dependency, so this is fully unit-testable
against FakeNetwork with zero hardware (deliverable 6).

Design recap (full justification in 01_DESIGN.md section 3):
  - Ranking: static priority_rank (primary, unique) > battery_pct (tiebreak)
    > missed_heartbeat count (final tiebreak). Ties should not occur given
    unique priority_rank, but the tiebreak chain is defensive.
  - Backoff: higher-priority nodes wait less before broadcasting candidacy,
    so simultaneous-detection races collapse to one candidate quickly.
  - Quorum: strict majority of the *configured* fleet, not of reachable
    peers (see 01_DESIGN.md section 4 for the partition tradeoff this implies).
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Dict, Optional

from models import Envelope, MsgType, FleetConfig, ElectionResult

log = logging.getLogger("election")

BACKOFF_BASE_S = 0.15
BACKOFF_JITTER_S = 0.15
BACKOFF_PRIORITY_WEIGHT_S = 0.02   # higher priority (lower rank number) waits less
ELECTION_TIMEOUT_S = 1.5           # time to collect quorum votes before retrying
VOTE_COLLECT_GRACE_S = 0.05


class ElectionModule:
    def __init__(
            self, 
            cfg: FleetConfig, 
            net
        ):
        self.cfg = cfg
        self.net = net
        self._votes_received: Dict[int, int] = {}   # term -> vote count (self included)
        self._higher_candidacy_seen_term: Optional[int] = None
        self._higher_candidacy_seen_node: Optional[int] = None

    def _effective_rank_key(
            self, 
            priority_rank: int, 
            battery_pct: Optional[float],
            missed_hb: int
        ) -> tuple:
        # Lower tuple = higher priority. battery sorted descending (more battery
        # wins) so we negate it; missed_hb ascending (fewer misses wins).
        return (priority_rank, -(battery_pct or 0.0), missed_hb)

    async def backoff_before_candidacy(
            self, 
            my_battery_pct: Optional[float],
            my_missed_hb: int
        ) -> Optional[int]:
        """
        Waits the jittered, priority-weighted backoff. Returns the node_id of
        a higher-priority candidate if one is heard during the wait (caller
        should defer instead of proceeding to run_election), else None.
        """
        self._higher_candidacy_seen_term = None
        self._higher_candidacy_seen_node = None
        wait_s = (
            BACKOFF_BASE_S
            + random.uniform(0, BACKOFF_JITTER_S)
            - (self.cfg.priority_rank * BACKOFF_PRIORITY_WEIGHT_S)
        )
        wait_s = max(0.0, wait_s)

        listener = asyncio.create_task(self._listen_for_candidacy_during_backoff())
        await asyncio.sleep(wait_s)
        listener.cancel()
        try:
            await listener
        except asyncio.CancelledError:
            pass
        return self._higher_candidacy_seen_node

    async def _listen_for_candidacy_during_backoff(self) -> None:
        my_key = self._effective_rank_key(self.cfg.priority_rank, None, 0)
        async for env in self.net.recv():
            if env.msg_type != MsgType.CANDIDACY.value:
                continue
            p = env.payload
            other_key = self._effective_rank_key(
                p["priority_rank"], p.get("battery_pct"), p.get("missed_hb_count", 0)
            )
            if other_key < my_key:
                self._higher_candidacy_seen_node = env.node_id
                self._higher_candidacy_seen_term = env.term
                return

    async def send_vote_ack(
            self, 
            for_node_id: int, 
            for_term: int
        ) -> None:
        await self.net.send(Envelope(
            msg_type=MsgType.VOTE_ACK.value,
            node_id=self.cfg.node_id,
            term=for_term,
            seq=0,
            sent_at_mono=time.monotonic(),
            payload={"voted_for_node_id": for_node_id, "for_term": for_term},
        ))

    async def run_election(
            self, 
            current_term: int, 
            my_battery_pct: Optional[float],
            my_missed_hb: int
        ) -> ElectionResult:
        """
        Broadcasts candidacy at current_term+1, collects votes for
        ELECTION_TIMEOUT_S, and declares victory only with quorum. This is
        the "no full Raft, small N Bully" path from 01_DESIGN.md section 3.
        """
        proposed_term = current_term + 1
        votes = {self.cfg.node_id}   # vote for self

        await self.net.send(Envelope(
            msg_type=MsgType.CANDIDACY.value,
            node_id=self.cfg.node_id,
            term=proposed_term,
            seq=0,
            sent_at_mono=time.monotonic(),
            payload={
                "priority_rank": self.cfg.priority_rank,
                "battery_pct": my_battery_pct,
                "missed_hb_count": my_missed_hb,
                "proposed_term": proposed_term,
            },
        ))

        deadline = time.monotonic() + ELECTION_TIMEOUT_S
        my_key = self._effective_rank_key(self.cfg.priority_rank, my_battery_pct, my_missed_hb)

        try:
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                try:
                    env = await asyncio.wait_for(self._next_relevant(proposed_term), remaining)
                except asyncio.TimeoutError:
                    break

                if env.msg_type == MsgType.VOTE_ACK.value:
                    if env.payload.get("for_term") == proposed_term and \
                       env.payload.get("voted_for_node_id") == self.cfg.node_id:
                        votes.add(env.node_id)
                        if len(votes) >= self.cfg.quorum_size:
                            return ElectionResult(True, proposed_term, self.cfg.node_id,
                                                   "quorum reached")
                elif env.msg_type == MsgType.CANDIDACY.value and env.term == proposed_term:
                    other_key = self._effective_rank_key(
                        env.payload["priority_rank"], env.payload.get("battery_pct"),
                        env.payload.get("missed_hb_count", 0),
                    )
                    if other_key < my_key:
                        # Defer: a higher-priority node is also running at the
                        # same proposed term. Vote for them and stop.
                        await self.send_vote_ack(env.node_id, proposed_term)
                        return ElectionResult(False, proposed_term, None,
                                               f"deferred to higher-priority node {env.node_id}")
                elif env.msg_type == MsgType.LEADER_ASSERT.value and env.term >= proposed_term:
                    return ElectionResult(False, env.term, env.node_id,
                                           "another node already asserted leadership")
        except asyncio.CancelledError:
            raise

        return ElectionResult(False, current_term, None,
                               "election timed out without quorum")

    async def _next_relevant(
            self, 
            proposed_term: int
        ) -> Envelope:
        async for env in self.net.recv():
            if env.msg_type in (MsgType.VOTE_ACK.value, MsgType.CANDIDACY.value,
                                MsgType.LEADER_ASSERT.value):
                return env

    async def assert_leadership_and_await_acks(
            self, 
            term: int
        ) -> bool:
        """
        Broadcasts LEADER_ASSERT and waits for quorum LEADER_ACK before the
        caller is allowed to resume issuing formation commands -- this is the
        bidirectional-confirmation requirement from 01_DESIGN.md section 5.
        """
        await self.net.send(Envelope(
            msg_type=MsgType.LEADER_ASSERT.value,
            node_id=self.cfg.node_id,
            term=term,
            seq=0,
            sent_at_mono=time.monotonic(),
            payload={"new_term": term, "leader_node_id": self.cfg.node_id},
        ))
        acks = {self.cfg.node_id}
        deadline = time.monotonic() + ELECTION_TIMEOUT_S
        while time.monotonic() < deadline and len(acks) < self.cfg.quorum_size:
            remaining = deadline - time.monotonic()
            try:
                env = await asyncio.wait_for(self._next_ack(term), remaining)
                acks.add(env.node_id)
            except asyncio.TimeoutError:
                # Retransmit assert once, in case followers missed the first one.
                await self.net.send(Envelope(
                    msg_type=MsgType.LEADER_ASSERT.value,
                    node_id=self.cfg.node_id, term=term, seq=1,
                    sent_at_mono=time.monotonic(),
                    payload={"new_term": term, "leader_node_id": self.cfg.node_id},
                ))
        return len(acks) >= self.cfg.quorum_size

    async def _next_ack(
            self, 
            term: int
        ) -> Envelope:
        async for env in self.net.recv():
            if env.msg_type == MsgType.LEADER_ACK.value and env.payload.get("ack_term") == term:
                return env

    async def send_leader_ack(
            self, 
            term: int
        ) -> None:
        await self.net.send(Envelope(
            msg_type=MsgType.LEADER_ACK.value,
            node_id=self.cfg.node_id,
            term=term,
            seq=0,
            sent_at_mono=time.monotonic(),
            payload={"ack_term": term, "ack_node_id": self.cfg.node_id},
        ))
