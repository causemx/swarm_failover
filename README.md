# swarm_failover

Leader failover for a small drone swarm. Each drone runs one instance of this
service on its companion computer (Raspberry Pi 5B). The instance talks to the
PX4 flight controller through MAVSDK and to the other drones over UDP broadcast.

When the current leader drone fails, the rest of the fleet detects it, holds
position, elects a new leader with a quorum-based Bully election, and resumes
formation flight under the new leader. A failed leader steps down and returns
to launch (RTL).

## How it works

- **Heartbeats.** Every node broadcasts a `HEARTBEAT` every 200 ms with its
  role, term, battery and flight-controller health.
- **Failure detection.** There are two detection paths:
  - *Timeout:* followers suspect the leader after about 600 ms of missed
    heartbeats (3 intervals).
  - *Self-report:* a leader whose own FC link goes stale (no telemetry for
    1 s), whose PX4 enters a failsafe mode (RTL / LAND / TERMINATION), or
    whose battery drops to 20 % or less steps down immediately and broadcasts
    `STEP_DOWN`.
- **Hold.** As soon as a follower suspects the leader, it commands PX4 `HOLD`.
- **Election.** Nodes wait a short randomized backoff. Nodes with higher
  priority wait less, so they usually go first. A node then broadcasts
  `CANDIDACY` for `term + 1` and needs `VOTE_ACK`s from a strict majority of
  the *configured* fleet (`fleet_size // 2 + 1`). Ranking order is:
  `priority_rank` (lower wins), then battery (higher wins), then missed
  heartbeats (fewer wins).
- **Confirmation.** The winner broadcasts `LEADER_ASSERT` and starts issuing
  formation commands only after a quorum of `LEADER_ACK`s.
- **Zombie-leader defense.** Every message carries a monotonic `term`.
  Followers discard `FORMATION_CMD`s from older terms. A leader that sees a
  higher term steps down.
- **Late join.** A node that boots listens for 1 s and adopts any leader and
  term it hears before it joins in.
- **Observability.** State transitions and events are broadcast as
  `SWARM_LOG` messages for a ground station to read. The ground station only
  listens; it never takes part in decisions.

### Node states

```
BOOT -> LISTEN_ONLY -> FOLLOWER -> HOLD_SUSPECT -> CANDIDATE -> LEADER_ELECT -> LEADER_ACTIVE
                          ^              |              |                              |
                          +--- (defer) --+-- (lose) ----+                    STEPPING_DOWN -> FOLLOWER_DEAD (RTL)
```

## Project layout

| File | Responsibility |
|---|---|
| `src/main.py` | CLI entry point; wires the modules together |
| `src/failover_coordinator.py` | Per-drone state machine; term gating of formation commands |
| `src/election.py` | Bully election, votes, leader assert/ack (no MAVSDK dependency) |
| `src/health_monitor.py` | Heartbeat send/receive, leader timeout, leader self-health checks |
| `src/mavsdk_interface.py` | Only module that talks to the flight controller (telemetry, HOLD, offboard, RTL) |
| `src/network.py` | `UdpBroadcastNetwork` transport and an in-memory `FakeNetwork` for tests |
| `src/gcs_relay.py` | Fire-and-forget `SWARM_LOG` events to the ground station |
| `src/models.py` | Shared enums, message envelope, fleet config (no dependencies) |

## Requirements

- Python 3.8+
- A PX4 flight controller reachable through MAVSDK (serial UART on hardware,
  or UDP for SITL)
- All drones on the same Layer-2 network segment (the service uses UDP
  broadcast to `255.255.255.255`)

Install the dependencies:

```bash
pip install -r requirements.txt
```

## Usage

The modules use flat imports, so run the service from inside `src/`:

```bash
cd src
python main.py --node-id <ID> --priority-rank <RANK> --fleet <ID,ID,...> \
    [--fc-address <MAVSDK_URL>] [--udp-port <PORT>]
```

| Option | Required | Default | Description |
|---|---|---|---|
| `--node-id` | yes | - | This drone's ID. Must appear in `--fleet`. |
| `--priority-rank` | yes | - | Election priority. **Lower = higher priority.** Must be unique across the fleet. |
| `--fleet` | yes | - | Comma-separated list of every node ID in the fleet, e.g. `1,2,3,4,5`. Quorum is computed from this list. |
| `--fc-address` | no | `serial:///dev/ttyAMA0:921600` | MAVSDK connection string for the flight controller. |
| `--udp-port` | no | `14650` | UDP broadcast port for swarm traffic. Must be the same on every node. |

Press `Ctrl+C` to stop the service.

### Example: five-drone fleet on hardware

Run one command on each drone's Pi. Use the same `--fleet` everywhere, and
give each drone a unique ID and rank:

```bash
# Drone 1 (preferred leader)
python main.py --node-id 1 --priority-rank 1 --fleet 1,2,3,4,5

# Drone 2
python main.py --node-id 2 --priority-rank 2 --fleet 1,2,3,4,5

# ... and so on through drone 5
python main.py --node-id 5 --priority-rank 5 --fleet 1,2,3,4,5
```

With 5 nodes the quorum is 3, so the fleet can still elect a leader with up to
2 drones lost.

### Example: PX4 SITL

Point `--fc-address` at each SITL instance's MAVLink UDP port:

```bash
python main.py --node-id 1 --priority-rank 1 --fleet 1,2,3 --fc-address udpin://0.0.0.0:14540
python main.py --node-id 2 --priority-rank 2 --fleet 1,2,3 --fc-address udpin://0.0.0.0:14541
python main.py --node-id 3 --priority-rank 3 --fleet 1,2,3 --fc-address udpin://0.0.0.0:14542
```

Each process blocks at startup until its flight controller connects.

### Watching events from a ground station

Every node broadcasts JSON envelopes on the swarm UDP port. To watch the
`SWARM_LOG` events from a laptop on the same network:

```python
import json, socket

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("", 14650))
while True:
    msg = json.loads(s.recv(65536))
    if msg["msg_type"] == "SWARM_LOG":
        p = msg["payload"]
        print(f"node {msg['node_id']} term {msg['term']}: {p['event_type']} - {p['reason']}")
```

## Tuning

The timing constants live at the top of their modules:

| Constant | File | Default |
|---|---|---|
| `HEARTBEAT_INTERVAL_S` | `health_monitor.py` | `0.2` s |
| `MISSED_HB_THRESHOLD` | `health_monitor.py` | `3` |
| `BATTERY_DEGRADE_PCT` | `health_monitor.py` | `20.0` % |
| `FC_LINK_STALE_S` | `mavsdk_interface.py` | `1.0` s |
| `ELECTION_TIMEOUT_S` | `election.py` | `1.5` s |
| `BACKOFF_BASE_S` / `BACKOFF_JITTER_S` / `BACKOFF_PRIORITY_WEIGHT_S` | `election.py` | `0.15` / `0.15` / `0.02` s |
| `LISTEN_ONLY_TIMEOUT_S` | `failover_coordinator.py` | `1.0` s |

## Design notes and limitations

- **Quorum counts the configured fleet, not the drones currently reachable.**
  If the network is split and no part holds a majority, no new leader is
  elected, and every node keeps retrying from `HOLD_SUSPECT`. This is
  deliberate: two leaders at once would be worse.
- **A drone that steps down stays out.** It goes to `FOLLOWER_DEAD` and RTL.
  It can only rejoin through a new ground-side session, never in the middle
  of a mission.
- **Clocks are not assumed to be in sync.** `term` (a counter) orders
  leadership, and `sent_at_mono` is only compared against the sender's own
  clock.
- **Testing without hardware.** `election.py` depends only on an object with
  `send()`/`recv()`, so you can test it against `network.FakeNetwork` with no
  hardware or sockets.
