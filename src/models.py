"""
Shared data model for the swarm failover layer.

Deliberately dependency-free: no networking, no MAVSDK. Every other module
imports from here so message shape/state enums stay a single source of truth.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from enum import Enum, auto
from typing import Optional


# ---------------------------------------------------------------------------
# Node lifecycle state -- mirrors the state machine in 01_DESIGN.md section 8.
# ---------------------------------------------------------------------------
class NodeState(Enum):
    BOOT = auto()
    LISTEN_ONLY = auto()
    FOLLOWER = auto()
    HOLD_SUSPECT = auto()
    ACKING = auto()
    CANDIDATE = auto()
    LEADER_ELECT = auto()
    LEADER_ACTIVE = auto()
    STEPPING_DOWN = auto()
    ZOMBIE_REJECTED = auto()
    FOLLOWER_DEAD = auto()


class StepDownReason(Enum):
    FC_LINK_LOST = "fc_link_lost"
    PX4_FAILSAFE = "px4_failsafe"
    BATTERY_LOW = "battery_low"
    MANUAL = "manual"


class MsgType(Enum):
    HEARTBEAT = "HEARTBEAT"
    STEP_DOWN = "STEP_DOWN"
    CANDIDACY = "CANDIDACY"
    VOTE_ACK = "VOTE_ACK"
    LEADER_ASSERT = "LEADER_ASSERT"
    LEADER_ACK = "LEADER_ACK"
    FORMATION_CMD = "FORMATION_CMD"
    SWARM_LOG = "SWARM_LOG"


# ---------------------------------------------------------------------------
# Wire envelope. IMPORTANT: `term` is a monotonic integer, never wall time.
# `sent_at_mono` is only ever compared against the SENDER's own later
# monotonic reads (e.g. for its own RTT bookkeeping) -- never against another
# node's monotonic clock, since clocks are not assumed synced (see ASSUMPTION
# A3 in 01_DESIGN.md).
# ---------------------------------------------------------------------------
@dataclass
class Envelope:
    msg_type: str
    node_id: int
    term: int
    seq: int
    sent_at_mono: float
    payload: dict

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Envelope":
        return Envelope(
            msg_type=d["msg_type"],
            node_id=int(d["node_id"]),
            term=int(d["term"]),
            seq=int(d["seq"]),
            sent_at_mono=float(d["sent_at_mono"]),
            payload=d.get("payload", {}),
        )


# ---------------------------------------------------------------------------
# Static, operator-configured fleet config -- this is the primary key that
# makes Bully ranking deterministic (ASSUMPTION A2 in 01_DESIGN.md).
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FleetConfig:
    node_id: int
    priority_rank: int          # lower number = higher priority; must be unique per node
    all_node_ids: tuple         # every configured node id, e.g. (1,2,3,4,5)

    @property
    def fleet_size(self) -> int:
        return len(self.all_node_ids)

    @property
    def quorum_size(self) -> int:
        return (self.fleet_size // 2) + 1


@dataclass
class PeerStatus:
    """Health-monitor's per-peer bookkeeping."""
    node_id: int
    last_seen_mono: float = field(default_factory=time.monotonic)
    last_seen_term: int = 0
    missed_heartbeats: int = 0
    last_known_role: str = "follower"
    battery_pct: Optional[float] = None
    fc_link_ok: bool = True
    px4_failsafe_active: bool = False


@dataclass
class ElectionResult:
    won: bool
    new_term: int
    leader_node_id: Optional[int]
    reason: str
