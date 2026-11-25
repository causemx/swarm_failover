# Swarm signaling protocol (as implemented)

All swarm traffic is JSON over UDP broadcast to `255.255.255.255:<udp-port>`
(default **14650**). The same port is used for sending and receiving. This
channel is separate from the MAVLink link between each Pi and its own PX4.

## Envelope

Defined by `models.Envelope`, serialized with `dataclasses.asdict` → `json.dumps`:

```json
{
  "msg_type": "HEARTBEAT",
  "node_id": 3,
  "term": 7,
  "seq": 1284,
  "sent_at_mono": 51234.877,
  "payload": { }
}
```

| Field | Type | Notes |
|---|---|---|
| `msg_type` | str | One of the `MsgType` values below |
| `node_id` | int | Sender's static ID |
| `term` | int | Logical election counter. It is never wall-clock time. |
| `seq` | int | Not one counter per node. `HealthMonitor` and `GcsRelay` each keep their own incrementing counter. Election messages use `0`, and `1` for the `LEADER_ASSERT` retransmit. No receiver reads it. |
| `sent_at_mono` | float | Sender's `time.monotonic()`. No receiver reads it, and it must never be compared across nodes. |
| `payload` | object | Fields specific to the message type |

The receiver drops anything that fails JSON parsing or `Envelope.from_dict`,
silently. There is no authentication, version field or size check.

On Linux, a socket that broadcasts usually also receives its own datagrams. The
handlers tolerate this in practice: own heartbeats are ignored because the
node is not in its own peer table, and an own `CANDIDACY` ranks equal to self,
so it is not treated as higher priority.

## Message types

### HEARTBEAT
- **Sender:** every node, every 200 ms (`HealthMonitor._send_loop`), from start
  until `FOLLOWER_DEAD`.
- **term:** the health monitor's `current_term`, set by `set_role()`.
- **payload:**

  | Field | Value |
  |---|---|
  | `role` | `"leader"` or `"follower"` |
  | `battery_pct` | float 0–100, or `null` if no telemetry yet |
  | `fc_link_ok` | bool. `false` if no telemetry yet or telemetry is stale for more than 1 s |
  | `px4_failsafe_active` | bool. True when the flight mode is RETURN_TO_LAUNCH, LAND or TERMINATION |
  | `missed_hb_count` | always `0` (not implemented) |

- **Consumers:**
  - `HealthMonitor._recv_loop` updates `PeerStatus` and suspects the leader if
    the current leader reports `fc_link_ok=false` or `px4_failsafe_active=true`.
  - `_boot()` adopts the first `role == "leader"` heartbeat it hears.

### STEP_DOWN
- **Sender:** a node in `STEPPING_DOWN`.
- **payload:** `reason` ∈ `fc_link_lost | px4_failsafe | battery_low | manual`.
- **Consumer:** `HealthMonitor._recv_loop` logs it (`peer_step_down`). If the
  sender is the current leader, it suspects the leader at once.

### CANDIDACY
- **Sender:** `ElectionModule.run_election`.
- **term:** the proposed term (`current_term + 1`).
- **payload:** `priority_rank`, `battery_pct`, `missed_hb_count` (always 0), `proposed_term`.
- **Consumers:**
  - Nodes in backoff, which defer if the sender outranks them.
  - Other candidates at the same proposed term, which defer and vote if the
    sender outranks them.

### VOTE_ACK
- **Sender:** a node that defers, either during backoff or mid-election.
- **term / payload:** `for_term`, `voted_for_node_id`.
- **Consumer:** the candidate named by `voted_for_node_id`, when `for_term`
  equals its proposed term.

### LEADER_ASSERT
- **Sender:** the election winner. It sends once on entry, and once more when
  the ack deadline expires (`seq=1`).
- **payload:** `new_term`, `leader_node_id`.
- **Consumers:**
  - followers with a lower term, which adopt it and send `LEADER_ACK`
  - candidates whose proposed term is ≤ the asserted term, which concede
  - booting nodes

### LEADER_ACK
- **Sender:** followers on accepting an assert, and candidates that concede.
- **payload:** `ack_term`, `ack_node_id`.
- **Consumer:** the asserting leader, counted when `ack_term` matches.

### FORMATION_CMD
- **Sender:** none in this codebase. A mission/formation component is expected
  to provide it.
- **payload (expected by consumer):**
  ```json
  { "formation_target": { "north_m": 0.0, "east_m": 5.0, "down_m": -10.0, "yaw_deg": 0.0 } }
  ```
  DESIGN.md also lists `term` and `cmd_seq` in the payload. The code uses only
  the envelope `term`.
- **Consumer:** followers in `FORMATION_CMD` handling. A command is rejected if
  `envelope.term < own term`. Otherwise it is sent to PX4 offboard as a
  `PositionNedYaw` setpoint. The sender's identity is **not** checked against
  the known leader.

### SWARM_LOG
- **Sender:** `GcsRelay.emit()`, for each coordinator event (list in
  [state-machine.md](state-machine.md#gcs-event-types-emitted)).
- **payload:** `event_type`, `reason`, `term`, `local_ts_mono`.
- **Consumer:** the ground station only. Nodes do read these off the socket,
  but no logic acts on them, except that a leader's higher-term check applies
  to *any* message type.

## Ranking (election order)

`ElectionModule._effective_rank_key` returns a tuple. The smaller tuple wins:

```
(priority_rank, -battery_pct_or_0, missed_hb_count)
```

1. Lower `priority_rank` wins. Ranks are expected to be unique, so this decides
   in practice.
2. Higher battery wins.
3. Fewer missed heartbeats win (always 0 today).

During backoff, a node compares itself with battery `None` (treated as 0) and
0 misses. Because rank is unique, the result is the same.

## Quorum

`quorum_size = len(all_node_ids) // 2 + 1`, which is 3 for a fleet of 5. The
candidate counts itself in both the vote quorum and the `LEADER_ACK` quorum.

## Traffic estimate

Steady state is 5 nodes × 5 heartbeats/s = 25 datagrams/s, each about 200
bytes, plus occasional `SWARM_LOG`. One election adds about 5 `CANDIDACY`, a few
`VOTE_ACK`, 1–2 `LEADER_ASSERT` and about 4 `LEADER_ACK`.
