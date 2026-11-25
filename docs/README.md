# swarm_failover documentation

Leader failover for a 5-drone PX4 swarm. Each drone's Raspberry Pi 5B runs one
instance of the service. The instance talks to PX4 through MAVSDK-python and
to the other drones over UDP broadcast on a shared LAN.

The design rationale (why Bully and not Raft, why 200 ms heartbeats, why the
quorum counts the configured fleet) is in [../DESIGN.md](../DESIGN.md). These
pages describe the system **as implemented in `src/`**, and point out where
the code and the design differ.

| Page | Contents |
|---|---|
| [architecture.md](architecture.md) | Modules, dependencies, asyncio tasks, data flow |
| [state-machine.md](state-machine.md) | Per-node states and transitions as coded |
| [protocol.md](protocol.md) | Wire envelope, each message type, who sends and who consumes it |
| [failure-handling.md](failure-handling.md) | Detection paths, timing budget, scenario walkthroughs |
| [operations.md](operations.md) | Install, CLI, hardware/SITL launch, GCS monitoring, tuning constants |
| [implementation-status.md](implementation-status.md) | Design vs. code gaps and known defects. **Read this before flight testing.** |

## One-paragraph summary

Every node broadcasts a `HEARTBEAT` every 200 ms. A follower that has not heard
from the leader for more than 600 ms, or that sees the leader report a dead FC
link or PX4 failsafe, or that receives the leader's `STEP_DOWN`, commands PX4
`HOLD`. It then waits a short randomized backoff and runs a Bully election for
`term + 1`. A candidate needs `VOTE_ACK`s from a strict majority of the
configured fleet (3 of 5). After that it broadcasts `LEADER_ASSERT`, and it
becomes active only after a majority of `LEADER_ACK`s. A leader that detects
its own failure (FC link stale, PX4 failsafe mode, battery ≤ 20 %), or that
sees a higher term, broadcasts `STEP_DOWN`, commands RTL, and leaves the swarm
for the rest of the session.
