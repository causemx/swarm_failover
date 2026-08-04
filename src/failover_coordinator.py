"""
Top-level per-drone orchestrator. Owns the NodeState state machine from
01_DESIGN.md section 8 and is the only module that references all the
others. Contains no networking or MAVSDK details itself -- it delegates.

Term-gating for FORMATION_CMD (the concrete zombie-leader defense from
01_DESIGN.md section 4) lives here: this is where a message's term is
compared against locally-known term before anything is passed to MAVSDK.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from models import (
    Envelope, MsgType, NodeState, StepDownReason, FleetConfig,
)
from network import UdpBroadcastNetwork
from mavsdk_interface import MavsdkInterface
from health_monitor import HealthMonitor
from election import ElectionModule
from gcs_relay import GcsRelay

log = logging.getLogger("coordinator")

LISTEN_ONLY_TIMEOUT_S = 1.0   # boot: must observe >=1 heartbeat before participating


class FailoverCoordinator:
    def __init__(self, cfg: FleetConfig, net: UdpBroadcastNetwork,
                 mavsdk: MavsdkInterface, gcs: GcsRelay):
        self.cfg = cfg
        self.net = net
        self.mavsdk = mavsdk
        self.gcs = gcs
        self.election = ElectionModule(cfg, net)

        self.state = NodeState.BOOT
        self.term = 0
        self.leader_id: Optional[int] = None

        self.health = HealthMonitor(
            cfg, net, mavsdk,
            on_leader_suspected=self._on_leader_suspected_sync,
            on_self_failure_detected=self._on_self_failure_sync,
            on_peer_step_down=self._on_peer_step_down_sync,
        )

        self._leader_suspected_event = asyncio.Event()
        self._self_failure_reason: Optional[StepDownReason] = None
        self._self_failure_event = asyncio.Event()

    # ------------------------------------------------------------------
    # Sync callbacks from health_monitor (called from its own coroutines) --
    # they just set asyncio.Events / state that the main loop below awaits,
    # keeping health_monitor decoupled from coordinator's control flow.
    # ------------------------------------------------------------------
    def _on_leader_suspected_sync(self, leader_id: Optional[int]) -> None:
        self._leader_suspected_event.set()

    def _on_self_failure_sync(self, reason: StepDownReason) -> None:
        self._self_failure_reason = reason
        self._self_failure_event.set()

    def _on_peer_step_down_sync(self, node_id: int, reason: StepDownReason) -> None:
        self._log_event("peer_step_down", f"node {node_id}: {reason.value}")

    def _log_event(self, event_type: str, reason: str) -> None:
        log.info("[term=%s] %s: %s", self.term, event_type, reason)
        self.gcs.emit(event_type, reason, self.term)

    # ------------------------------------------------------------------
    async def run(self) -> None:
        await self.mavsdk.connect()
        await self.net.start()
        health_task = asyncio.create_task(self.health.run())

        await self._boot()

        while self.state != NodeState.FOLLOWER_DEAD:
            if self.state == NodeState.FOLLOWER:
                await self._run_follower()
            elif self.state == NodeState.HOLD_SUSPECT:
                await self._run_hold_suspect()
            elif self.state == NodeState.CANDIDATE:
                await self._run_candidate()
            elif self.state == NodeState.LEADER_ELECT:
                await self._run_leader_elect()
            elif self.state == NodeState.LEADER_ACTIVE:
                await self._run_leader_active()
            elif self.state == NodeState.STEPPING_DOWN:
                await self._run_stepping_down()
            else:
                await asyncio.sleep(0.1)

        health_task.cancel()

    # ------------------------------------------------------------------
    async def _boot(self) -> None:
        """LISTEN_ONLY: adopt whatever term/leader we observe; never assume
        term=0/no-leader just because we personally missed the election
        (handles the late-join / rebooted-drone edge case)."""
        self.state = NodeState.LISTEN_ONLY
        self._log_event("state_transition", "BOOT -> LISTEN_ONLY")

        deadline = time.monotonic() + LISTEN_ONLY_TIMEOUT_S
        heard = False
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                env = await asyncio.wait_for(self._next_heartbeat_or_assert(), remaining)
                if env.msg_type == MsgType.HEARTBEAT.value and env.payload.get("role") == "leader":
                    self.term = env.term
                    self.leader_id = env.node_id
                    heard = True
                    break
                if env.msg_type == MsgType.LEADER_ASSERT.value:
                    self.term = env.term
                    self.leader_id = env.node_id
                    heard = True
                    break
            except asyncio.TimeoutError:
                break

        if heard:
            self._enter_follower(self.term, self.leader_id)
        else:
            # No one heard from -- either we're first to boot, or fleet-wide
            # partition (see 01_DESIGN.md section 4 no-leader deadlock case).
            # Enter FOLLOWER at term 0 with no known leader; the normal
            # heartbeat-timeout path will trigger an election shortly.
            self._enter_follower(0, None)

    async def _next_heartbeat_or_assert(self) -> Envelope:
        async for env in self.net.recv():
            if env.msg_type in (MsgType.HEARTBEAT.value, MsgType.LEADER_ASSERT.value):
                return env

    def _enter_follower(self, term: int, leader_id: Optional[int]) -> None:
        self.state = NodeState.FOLLOWER
        self.term = term
        self.leader_id = leader_id
        self.health.set_role("follower", term, leader_id)
        self._leader_suspected_event = asyncio.Event()
        self._log_event("state_transition", f"-> FOLLOWER (term={term}, leader={leader_id})")

    # ------------------------------------------------------------------
    async def _run_follower(self) -> None:
        """Stay here processing heartbeats/asserts/formation-cmds (with term
        gating) until leader loss is suspected."""
        wait_task = asyncio.create_task(self._leader_suspected_event.wait())
        recv_task = asyncio.create_task(self._next_relevant_follower_msg())
        done, pending = await asyncio.wait(
            {wait_task, recv_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()

        if wait_task in done:
            self.state = NodeState.HOLD_SUSPECT
            return

        env = recv_task.result()
        if env.msg_type == MsgType.LEADER_ASSERT.value and env.term > self.term:
            self.term = env.term
            self.leader_id = env.node_id
            self.health.set_role("follower", self.term, self.leader_id)
            await self.election.send_leader_ack(self.term)
            self._log_event("leader_ack_sent", f"acked term {self.term} leader {self.leader_id}")
        elif env.msg_type == MsgType.FORMATION_CMD.value:
            if env.term < self.term:
                self._log_event("stale_cmd_rejected",
                                 f"from node {env.node_id} term {env.term} < {self.term}")
                return  # zombie-leader defense: discard stale-term commands
            p = env.payload
            await self.mavsdk.resume_formation_command(
                p["formation_target"]["north_m"], p["formation_target"]["east_m"],
                p["formation_target"]["down_m"], p["formation_target"]["yaw_deg"],
            )

    async def _next_relevant_follower_msg(self) -> Envelope:
        async for env in self.net.recv():
            if env.msg_type in (MsgType.LEADER_ASSERT.value, MsgType.FORMATION_CMD.value):
                return env

    # ------------------------------------------------------------------
    async def _run_hold_suspect(self) -> None:
        await self.mavsdk.hold_position()
        self._log_event("state_transition", "-> HOLD_SUSPECT, PX4 HOLD commanded")

        telem = self.mavsdk.latest()
        battery_pct = telem.battery_pct if telem else None
        missed_hb = 0

        higher = await self.election.backoff_before_candidacy(battery_pct, missed_hb)
        if higher is not None:
            self.state = NodeState.ACKING
            await self.election.send_vote_ack(higher, self.term + 1)
            self._log_event("deferred", f"to higher-priority candidate {higher}")
            self._enter_follower(self.term, self.leader_id)  # awaiting their LEADER_ASSERT
            return

        self.state = NodeState.CANDIDATE

    async def _run_candidate(self) -> None:
        telem = self.mavsdk.latest()
        battery_pct = telem.battery_pct if telem else None
        self._log_event("candidacy_started", f"proposed_term={self.term + 1}")

        result = await self.election.run_election(self.term, battery_pct, 0)
        if result.won:
            self.term = result.new_term
            self.leader_id = self.cfg.node_id
            self.state = NodeState.LEADER_ELECT
            self._log_event("election_won", f"term={self.term}")
        elif result.leader_node_id is not None:
            # Someone else already asserted / we deferred mid-election.
            self.term = result.new_term
            self.leader_id = result.leader_node_id
            self._enter_follower(self.term, self.leader_id)
            await self.election.send_leader_ack(self.term)
        else:
            # Timed out without quorum -- retry with fresh jitter. Covers the
            # partial-partition case where we can see fewer than quorum peers.
            self._log_event("election_timeout", result.reason)
            self.state = NodeState.HOLD_SUSPECT

    async def _run_leader_elect(self) -> None:
        got_quorum_ack = await self.election.assert_leadership_and_await_acks(self.term)
        if got_quorum_ack:
            self.state = NodeState.LEADER_ACTIVE
            self.health.set_role("leader", self.term, self.cfg.node_id)
            self._log_event("leader_active", f"term={self.term}, quorum acks received")
        else:
            # Couldn't confirm quorum ACKs -- do not start issuing commands.
            # This is a residual "elected but unconfirmed" edge; erring
            # conservative means falling back rather than commanding blind.
            self._log_event("leader_ack_quorum_failed", "reverting to HOLD_SUSPECT")
            self.state = NodeState.HOLD_SUSPECT

    async def _run_leader_active(self) -> None:
        self._self_failure_event = asyncio.Event()
        fail_task = asyncio.create_task(self._self_failure_event.wait())
        zombie_task = asyncio.create_task(self._next_higher_term_seen())

        done, pending = await asyncio.wait(
            {fail_task, zombie_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()

        if fail_task in done:
            self.state = NodeState.STEPPING_DOWN
            return
        if zombie_task in done:
            higher_term = zombie_task.result()
            self._log_event("zombie_rejected", f"higher term {higher_term} observed, stopping commands")
            self.term = higher_term
            self.state = NodeState.STEPPING_DOWN

    async def _next_higher_term_seen(self) -> int:
        async for env in self.net.recv():
            if env.term > self.term:
                return env.term

    async def _run_stepping_down(self) -> None:
        reason = self._self_failure_reason or StepDownReason.MANUAL
        await self.net.send(Envelope(
            msg_type=MsgType.STEP_DOWN.value,
            node_id=self.cfg.node_id,
            term=self.term,
            seq=0,
            sent_at_mono=time.monotonic(),
            payload={"reason": reason.value},
        ))
        self._log_event("step_down", reason.value)
        # PX4 native failsafe (RTL/land) governs the vehicle from here --
        # our layer does not attempt further control. Per ASSUMPTION A5, this
        # session terminates; a recovered drone rejoins only via a fresh
        # ground-side session, never a silent mid-mission rejoin.
        await self.mavsdk.trigger_rtl()
        self.state = NodeState.FOLLOWER_DEAD
