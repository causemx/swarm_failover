# Failure detection and failover

## Detection paths

| # | Trigger | Detected by | Where | Latency |
|---|---|---|---|---|
| 1 | Leader heartbeats stop (Pi dead, process dead, network loss) | Followers | `HealthMonitor._timeout_watch_loop` | > 600 ms since last heartbeat, checked every 200 ms → **600–800 ms** |
| 2 | Leader sends `STEP_DOWN` | Followers | `HealthMonitor._recv_loop` | ≈ one network hop |
| 3 | Leader heartbeat says `fc_link_ok=false` or `px4_failsafe_active=true` | Followers | `HealthMonitor._handle_heartbeat` | Next heartbeat (≤ 200 ms) |
| 4 | Own telemetry stale for more than 1 s (FC link lost) | Leader itself | `_self_health_loop` + `MavsdkInterface.latest()` | 1.0–1.2 s after the last telemetry |
| 5 | Own flight mode is RETURN_TO_LAUNCH / LAND / TERMINATION | Leader itself | `_self_health_loop` | ≤ 200 ms after the mode change is reported |
| 6 | Own battery ≤ 20 % (`BATTERY_DEGRADE_PCT`) | Leader itself | `_self_health_loop` | ≤ 200 ms after the battery update |
| 7 | Any message with a term higher than the leader's own | Leader itself | `_run_leader_active` | On receipt |

Paths 4–7 lead the leader to `STEPPING_DOWN`: it broadcasts `STEP_DOWN` and
commands RTL. Path 3 makes followers react before the leader's own
step-down arrives. It covers a leader that sees the problem but crashes before
sending `STEP_DOWN`.

"FC link lost" means none of the battery, flight-mode or armed streams has
produced an update for `FC_LINK_STALE_S` (1.0 s). There is no direct MAVSDK
link-state check.

## Failover timeline (happy path)

```
t=0        leader heartbeats stop
t≈0.6–0.8  followers suspect → PX4 HOLD → backoff starts
t≈+0.05–0.28  backoff ends (depends on rank and jitter)
           first candidate broadcasts CANDIDACY(term+1)
           followers still in backoff hear it → VOTE_ACK
t≈+RTT     candidate has 3 votes → LEADER_ASSERT
t≈+RTT     followers → LEADER_ACK → leader has 3 acks → LEADER_ACTIVE
total ≈ 1 s typical, well inside the 15 s budget in DESIGN.md
```

Worst cases per attempt: election 1.5 s, plus ack collection 1.5 s, plus
backoff 0.3 s. On a failure the node goes back to `HOLD_SUSPECT` and retries
with fresh jitter, with no limit on retries.

## Vehicle behavior during failover

| Node | Vehicle action |
|---|---|
| Follower that suspects the leader | Offboard stopped, PX4 `HOLD`. The command is repeated on each election retry. |
| Follower after a new leader is active | Stays in `HOLD` until a valid `FORMATION_CMD` arrives, then goes back to offboard |
| Failed leader (self-detected) | `STEP_DOWN`, offboard stopped, `RTL` |
| Failed leader (process/Pi dead) | Nothing from this layer. PX4's own offboard-loss / data-link-loss failsafe applies. |
| Old leader that sees a higher term | `STEP_DOWN(manual)`, `RTL`, session ends |

## Scenario walkthroughs

### Leader's Pi loses power
Heartbeats stop, and followers suspect within about 0.7 s (path 1) and hold.
The highest-priority survivor normally wins (but see the backoff inversion in
[implementation-status.md](implementation-status.md#3-backoff-favors-lower-priority-nodes)).
The dead leader's PX4 is handled by its own failsafe parameters.

### Leader's UART to PX4 breaks
The leader's telemetry goes stale, and after 1 s `latest()` reports
`fc_link_ok=false`. In the same moment:
- the leader's next heartbeat carries `fc_link_ok=false`, so followers suspect
  (path 3)
- the leader's `_self_health_loop` raises `FC_LINK_LOST`, so the leader sends
  `STEP_DOWN`

Its RTL command cannot reach PX4. PX4's offboard-loss failsafe takes over.

### Leader battery reaches 20 %
The leader steps down voluntarily (`battery_low`) and RTLs, and followers elect
a replacement. DESIGN.md describes a softer `DEGRADE_WARN` handoff. This is not
implemented: battery goes straight to `STEP_DOWN`.

### Network partition 3 / 2
The group of 3 can reach quorum and elects a leader at term T+1. The group of 2
retries forever from `HOLD_SUSPECT` and never reaches 3 votes, so its drones
stay in PX4 `HOLD`. When the partition heals, if the old leader was in the
group of 2, the first message it gets at term T+1 makes it step down
(`zombie_rejected`) and RTL.

### Access point dies (no group of 3)
No node can reach quorum. Every node cycles HOLD_SUSPECT → CANDIDATE → timeout
indefinitely, and every drone stays in `HOLD`. This deadlock is intended: no
leader is safer than two.

### Two followers detect loss at the same time
Backoff staggers them, and the one that broadcasts first gets votes from the
others still in backoff. If both broadcast `CANDIDACY` at the same proposed
term, the lower-ranked one sees the higher-ranked candidacy mid-election,
votes for it and drops out.

### Stale commands from an old leader
Followers drop any `FORMATION_CMD` whose envelope term is below their own
(`stale_cmd_rejected`). An old leader with an equal term would not be caught by
this check. Terms only increase through elections, so equal-term commands from
a non-leader are not expected, but the sender's identity is not checked.

### A drone boots late
It listens for 1 s. If it hears a leader heartbeat or a `LEADER_ASSERT`, it
joins as a follower at that term. It does not reset to term 0 when a leader
exists.

## Safety boundary

PX4 always has final authority. This layer never fights a PX4 failsafe: a
leader whose PX4 enters RTL/LAND/TERMINATION only steps down. The GCS only
listens and has no part in any decision.
