# Implementation status: design vs. code

This page lists where `src/` differs from [DESIGN.md](../DESIGN.md), and the
defects found when reading the code for these docs. Line references are to
the working tree as of 2026-11-25. Items 1–3 block correct failover and
should be fixed before flight testing.

## Blocking defects

### 1. Shared receive queue: messages are consumed by only one reader

`UdpBroadcastNetwork` puts every datagram into one `asyncio.Queue`
([network.py:27](../src/network.py#L27)), and `recv()` is a generator over
`queue.get()` ([network.py:58-61](../src/network.py#L58-L61)). Several
coroutines iterate `net.recv()` at the same time:

- `HealthMonitor._recv_loop`, always
- the coordinator's `_next_relevant_follower_msg` / `_next_higher_term_seen` /
  `_next_heartbeat_or_assert`
- the election's `_listen_for_candidacy_during_backoff` / `_next_relevant` / `_next_ack`

An `asyncio.Queue` gives each item to **one** waiter. Each consumer filters for
its own message types and throws the rest away. As a result:

- Leader heartbeats taken by the coordinator's follower waiter never reach the
  health monitor, which can cause false leader-timeouts.
- `LEADER_ASSERT`, `VOTE_ACK` and `LEADER_ACK` taken by the health monitor are
  lost, which can cause failed elections and ack quorums.

`FakeNetwork` has the same single-queue-per-instance design, so it reproduces
the bug instead of hiding it.

**Fix direction:** make the network a fan-out. Give each subscriber its own
queue (`net.subscribe()` returns a new queue that receives every envelope), or
have one dispatcher that routes by `msg_type`.

### 2. A fleet that boots with no leader never elects one

If `_boot()` hears nothing, the node enters `FOLLOWER` with `leader = None`
([failover_coordinator.py:135](../src/failover_coordinator.py#L135)). The code
comment says the timeout path will start an election. But
`_timeout_watch_loop` skips the check when `current_leader_id is None`
([health_monitor.py:130-131](../src/health_monitor.py#L130-L131)), and nothing
else can raise suspicion without a leader. A fleet that starts together stays
leaderless.

DESIGN.md §8 has `LISTEN_ONLY → CANDIDATE` when nothing is heard. **Fix
direction:** after listen-only with no leader, go to `HOLD_SUSPECT`/`CANDIDATE`
(on the ground the HOLD command needs care), or treat "no leader" as an
immediate suspicion.

### 3. Backoff favors lower-priority nodes

DESIGN.md §3 and the code comment say higher-priority nodes (lower rank number)
wait less. The formula `0.15 + U(0,0.15) − rank × 0.02`
([election.py:69-73](../src/election.py#L69-L73)) does the reverse:

| rank | backoff range |
|---|---|
| 1 | 0.13 – 0.28 s |
| 3 | 0.09 – 0.24 s |
| 5 | 0.05 – 0.20 s |

Lower-priority nodes therefore usually broadcast `CANDIDACY` first. Nodes still
in backoff vote for the first candidate that outranks them, so a rank-5 node
can collect votes from ranks 3 and 4 and win before rank 2 finishes its
backoff. The election still produces exactly one leader, but often not the
preferred one.

**Fix direction:** weight by `(max_rank − rank)`, or add the rank delay
instead of subtracting it: `base + jitter + (rank − 1) × weight`.

## Correctness gaps

| # | Issue | Location | Impact |
|---|---|---|---|
| 4 | Deferring *mid-election* returns `leader_node_id=None`, so the coordinator logs `election_timeout` and goes back to `HOLD_SUSPECT` instead of `FOLLOWER`. In the next backoff, the listener throws away the winner's `LEADER_ASSERT`. | [election.py:167-169](../src/election.py#L167-L169), [failover_coordinator.py:225-229](../src/failover_coordinator.py#L225-L229) | The node may run a pointless second candidacy before it concedes |
| 5 | `LEADER_ASSERT` is resent only after the ack deadline has passed, and the loop then exits, so the resend cannot produce acks | [election.py:207-220](../src/election.py#L207-L220) | No real retransmission. Design §8 has `LEADER_ELECT` retransmit until acks complete. |
| 6 | There is no one-vote-per-term record. A node can vote for one candidate in backoff and later for another at the same term. | election.py | Quorum intersection relies on each node voting once per term. Two leaders at one term are unlikely but possible. |
| 7 | `FORMATION_CMD` is checked only for `term < own`. The sender is not checked against `leader_id`. | [failover_coordinator.py:173-182](../src/failover_coordinator.py#L173-L182) | Any node can command followers at the current term |
| 8 | Before its first telemetry sample, a node sends `fc_link_ok=false`. If it is the leader (or was adopted as leader at boot), followers suspect it at once. | [health_monitor.py:80](../src/health_monitor.py#L80), [health_monitor.py:122-125](../src/health_monitor.py#L122-L125) | False suspicion at startup |
| 9 | A leader's own `STEP_DOWN` loops back to it and can trigger its own health monitor's suspicion | health_monitor.py `_recv_loop` | Harmless: the node is already terminal |
| 10 | The higher-term zombie check fires on *any* message type, including `SWARM_LOG` and a `CANDIDACY` from a node that cannot reach quorum | [failover_coordinator.py:264-267](../src/failover_coordinator.py#L264-L267) | One partitioned node's failed candidacy (term+1) makes a healthy leader step down and RTL when the partition heals, or as soon as it hears it |
| 11 | HOLD is commanded again on every election retry, and also on the ground, before takeoff | [failover_coordinator.py:191](../src/failover_coordinator.py#L191) | Mode churn. HOLD may be refused while disarmed. |
| 12 | `FakeNetwork._all_instances` is a class-level list that is never cleared | [network.py:76](../src/network.py#L76) | Tests leak instances into each other unless each test uses its own `bus` |

## Features in DESIGN.md that are not implemented

| Design item | Status |
|---|---|
| Leader issues `FORMATION_CMD`s (§9) | No sender. `LEADER_ACTIVE` only watches for failure. |
| `DEGRADE_WARN` soft handoff on battery (§2) | Not present. Battery ≤ 20 % → `STEP_DOWN(battery_low)` |
| Per-peer `missed_hb_count` as the third ranking key (§3) | Always sent as 0. `PeerStatus.missed_heartbeats` is counted but never used. |
| `fc_link_ok=false` must last more than 1 s before followers act (§2) | Followers act on the first such heartbeat. The 1 s comes from the sender's staleness window. |
| `ZOMBIE_REJECTED` state (§8) | Enum value exists but is never entered. The zombie path goes straight to `STEPPING_DOWN`. |
| Listen-only boot: no heartbeats until a valid one is observed (§6) | Heartbeats start before `_boot()` |
| Booting node becomes a candidate if quorum peers are reachable (§8) | Not implemented (see #2) |
| Local structured JSON-lines log on disk (§7) | Only Python `logging` to stderr, plus `SWARM_LOG` |
| `seq` as a per-node counter that detects reordering (§9) | `seq` is not used consistently and no receiver checks it |
| `cmd_seq` in `FORMATION_CMD` (§9) | Not used |
| Step-down: "PX4 native failsafe takes over" (§5, §8) | The code also *commands* RTL itself (`trigger_rtl()`) |
| Term re-sync for a recovered leader (§6) | Only by restarting the process, which then gets the term through listen-only boot |
| Unit tests with `FakeNetwork` (deliverable 6) | No `tests/` directory yet. `network.py` mentions one. |

## Housekeeping

- Module docstrings refer to `01_DESIGN.md`, but the file is `DESIGN.md`.
- The uncommitted change removes the module docstring from `gcs_relay.py`.
- `mavsdk_interface.py` imports `time` inside methods instead of at module level.
- `AsyncIterator` is imported but unused in `mavsdk_interface.py`.
- CLI inputs are not validated: whether `--node-id` is in `--fleet`, whether
  ranks are unique, whether the fleet list is the same on every node.
