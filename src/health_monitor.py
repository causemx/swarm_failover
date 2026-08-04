"""
Owns the heartbeat send loop and per-peer missed-heartbeat tracking.

Two independent detection paths, per 01_DESIGN.md section 2:
  1. Timeout-based: 3 consecutive missed heartbeats from whoever we believe
     is the current leader -> fires on_leader_suspected.
  2. Self-reported: this node's OWN mavsdk_interface telemetry shows FC link
     lost / PX4 failsafe active / battery <= threshold -> fires
     on_self_failure_detected IMMEDIATELY, without waiting for any timeout,
     because a node always knows about its own failure before the network's
     timeout mechanism would.

This module has no election logic and does not decide who becomes leader --
it only raises events. failover_coordinator.py wires those events to
election.py and mavsdk_interface.py.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Dict, Optional

from models import Envelope, MsgType, PeerStatus, StepDownReason, FleetConfig
from network import UdpBroadcastNetwork
from mavsdk_interface import MavsdkInterface

log = logging.getLogger("health_monitor")

HEARTBEAT_INTERVAL_S = 0.2      # 200ms -- see 01_DESIGN.md section 1 justification
MISSED_HB_THRESHOLD = 3         # 3 misses = 600ms detection latency
BATTERY_DEGRADE_PCT = 20.0      # ASSUMPTION A1: voluntary step-down trigger, not PX4 hard failsafe


class HealthMonitor:
    def __init__(
        self,
        cfg: FleetConfig,
        net: UdpBroadcastNetwork,
        mavsdk: MavsdkInterface,
        on_leader_suspected: Callable[[int], None],
        on_self_failure_detected: Callable[[StepDownReason], None],
        on_peer_step_down: Callable[[int, StepDownReason], None],
    ):
        self.cfg = cfg
        self.net = net
        self.mavsdk = mavsdk
        self.on_leader_suspected = on_leader_suspected
        self.on_self_failure_detected = on_self_failure_detected
        self.on_peer_step_down = on_peer_step_down

        self.peers: Dict[int, PeerStatus] = {
            nid: PeerStatus(node_id=nid) for nid in cfg.all_node_ids if nid != cfg.node_id
        }
        self.current_leader_id: Optional[int] = None
        self.current_term: int = 0
        self._seq = 0
        self._role = "follower"
        self._leader_suspected_fired = False

    def set_role(self, role: str, term: int, leader_id: Optional[int]) -> None:
        self._role = role
        self.current_term = term
        self.current_leader_id = leader_id
        self._leader_suspected_fired = False

    async def run(self) -> None:
        await asyncio.gather(self._send_loop(), self._recv_loop(), self._timeout_watch_loop(),
                              self._self_health_loop())

    # ------------------------------------------------------------------
    async def _send_loop(self) -> None:
        while True:
            telem = self.mavsdk.latest()
            self._seq += 1
            payload = {
                "role": self._role,
                "battery_pct": telem.battery_pct if telem else None,
                "fc_link_ok": telem.fc_link_ok if telem else False,
                "px4_failsafe_active": telem.px4_failsafe_active if telem else False,
                "missed_hb_count": 0,
            }
            env = Envelope(
                msg_type=MsgType.HEARTBEAT.value,
                node_id=self.cfg.node_id,
                term=self.current_term,
                seq=self._seq,
                sent_at_mono=time.monotonic(),
                payload=payload,
            )
            await self.net.send(env)
            await asyncio.sleep(HEARTBEAT_INTERVAL_S)

    async def _recv_loop(self) -> None:
        async for env in self.net.recv():
            if env.msg_type == MsgType.HEARTBEAT.value:
                self._handle_heartbeat(env)
            elif env.msg_type == MsgType.STEP_DOWN.value:
                reason = StepDownReason(env.payload.get("reason", "manual"))
                log.warning("peer %s stepped down: %s", env.node_id, reason.value)
                self.on_peer_step_down(env.node_id, reason)
                if env.node_id == self.current_leader_id:
                    self._fire_leader_suspected()

    def _handle_heartbeat(self, env: Envelope) -> None:
        peer = self.peers.get(env.node_id)
        if peer is None:
            return  # not in configured fleet -- ignore (or log as anomaly)
        peer.last_seen_mono = time.monotonic()
        peer.last_seen_term = env.term
        peer.missed_heartbeats = 0
        peer.last_known_role = env.payload.get("role", "follower")
        peer.battery_pct = env.payload.get("battery_pct")
        peer.fc_link_ok = env.payload.get("fc_link_ok", True)
        peer.px4_failsafe_active = env.payload.get("px4_failsafe_active", False)

        # Leader self-reporting its own FC link/failsafe trouble -- treat as
        # equivalent to STEP_DOWN even if it hasn't broadcast one explicitly
        # (defensive: covers a leader that detects the problem but crashes
        # before it finishes sending STEP_DOWN).
        if env.node_id == self.current_leader_id and (
            not peer.fc_link_ok or peer.px4_failsafe_active
        ):
            self._fire_leader_suspected()

    async def _timeout_watch_loop(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_S)
            if self.current_leader_id is None:
                continue
            leader_peer = self.peers.get(self.current_leader_id)
            if leader_peer is None:
                continue
            elapsed = time.monotonic() - leader_peer.last_seen_mono
            if elapsed > HEARTBEAT_INTERVAL_S * MISSED_HB_THRESHOLD:
                leader_peer.missed_heartbeats += 1
                self._fire_leader_suspected()

    def _fire_leader_suspected(self) -> None:
        if self._leader_suspected_fired:
            return
        self._leader_suspected_fired = True
        log.warning("leader %s suspected lost at term %s", self.current_leader_id, self.current_term)
        self.on_leader_suspected(self.current_leader_id)

    async def _self_health_loop(self) -> None:
        """Only meaningful while this node believes it IS the leader."""
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_S)
            if self._role != "leader":
                continue
            telem = self.mavsdk.latest()
            if telem is None:
                continue
            if not telem.fc_link_ok:
                self.on_self_failure_detected(StepDownReason.FC_LINK_LOST)
            elif telem.px4_failsafe_active:
                self.on_self_failure_detected(StepDownReason.PX4_FAILSAFE)
            elif telem.battery_pct is not None and telem.battery_pct <= BATTERY_DEGRADE_PCT:
                self.on_self_failure_detected(StepDownReason.BATTERY_LOW)
