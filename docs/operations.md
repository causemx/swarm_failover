# Operations guide

## Requirements

- Python 3.8+ (the target platform is Raspberry Pi OS on a Pi 5B)
- `mavsdk` (the design targets MAVSDK-python 3.5.13) and `pyserial`, from [requirements.txt](../requirements.txt)
- PX4 reachable through MAVSDK: UART on hardware, UDP for SITL
- All nodes and the GCS on one Layer-2 segment where `255.255.255.255`
  broadcast is delivered. Some APs have client isolation or broadcast
  filtering; turn those off.

```bash
pip install -r requirements.txt
```

## Running a node

The modules use flat imports, so run from inside `src/`:

```bash
cd src
python main.py --node-id <ID> --priority-rank <RANK> --fleet <ID,ID,...> \
    [--fc-address <MAVSDK_URL>] [--udp-port <PORT>]
```

| Option | Required | Default | Notes |
|---|---|---|---|
| `--node-id` | yes | — | Must appear in `--fleet` (not validated) |
| `--priority-rank` | yes | — | Lower = higher priority. Must be unique (not validated). |
| `--fleet` | yes | — | Every configured node ID. It sets the quorum, so use the **same list on every node**. |
| `--fc-address` | no | `serial:///dev/ttyAMA0:921600` | MAVSDK connection URL |
| `--udp-port` | no | `14650` | Must match on every node and on the GCS listener |

Startup order inside the process:
1. Connect MAVSDK and block until PX4 reports connected.
2. Bind the UDP socket.
3. Start heartbeats.
4. Listen for 1 s.
5. Enter the state machine.

Press `Ctrl+C` to stop.

### Five drones on hardware

```bash
python main.py --node-id 1 --priority-rank 1 --fleet 1,2,3,4,5
python main.py --node-id 2 --priority-rank 2 --fleet 1,2,3,4,5
python main.py --node-id 3 --priority-rank 3 --fleet 1,2,3,4,5
python main.py --node-id 4 --priority-rank 4 --fleet 1,2,3,4,5
python main.py --node-id 5 --priority-rank 5 --fleet 1,2,3,4,5
```

Quorum is 3, so the fleet can still elect a leader after losing up to 2 drones.

On the Pi, enable the UART (`/dev/ttyAMA0`) and make sure the PX4 `TELEM`
port's baud rate matches (921600 by default here).

### PX4 SITL

```bash
python main.py --node-id 1 --priority-rank 1 --fleet 1,2,3 --fc-address udpin://0.0.0.0:14540
python main.py --node-id 2 --priority-rank 2 --fleet 1,2,3 --fc-address udpin://0.0.0.0:14541
python main.py --node-id 3 --priority-rank 3 --fleet 1,2,3 --fc-address udpin://0.0.0.0:14542
```

Several nodes can run on one host because the socket sets `SO_REUSEADDR`.
Whether every process on the port receives each broadcast depends on the OS.
On Linux, check this before trusting SITL results, and consider
`SO_REUSEPORT` or separate network namespaces.

## Watching from the GCS

The GCS only listens. A minimal listener:

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

To see liveness, also print `HEARTBEAT`s: `role`, `battery_pct`, `fc_link_ok`.
The event types are listed in
[state-machine.md](state-machine.md#gcs-event-types-emitted).

Each node also logs to stderr through Python `logging` at INFO, in the format
`%(asctime)s [%(name)s] %(message)s`. The logger names are `coordinator`,
`election`, `health_monitor` and `mavsdk_interface`. DESIGN.md §7 describes
local JSON-lines files on disk; these are not implemented yet. To keep logs,
redirect stderr, for example through a systemd unit with the journal.

## Tuning constants

| Constant | File | Default | Effect |
|---|---|---|---|
| `HEARTBEAT_INTERVAL_S` | health_monitor.py | 0.2 s | Heartbeat period and the timeout-check period |
| `MISSED_HB_THRESHOLD` | health_monitor.py | 3 | Leader timeout = interval × threshold |
| `BATTERY_DEGRADE_PCT` | health_monitor.py | 20.0 % | Leader voluntarily steps down at or below this |
| `FC_LINK_STALE_S` | mavsdk_interface.py | 1.0 s | Telemetry silence treated as FC link loss |
| `ELECTION_TIMEOUT_S` | election.py | 1.5 s | Vote-collection window, and separately the ack window |
| `BACKOFF_BASE_S` | election.py | 0.15 s | Backoff base |
| `BACKOFF_JITTER_S` | election.py | 0.15 s | Uniform random jitter added to the backoff |
| `BACKOFF_PRIORITY_WEIGHT_S` | election.py | 0.02 s | Subtracted per rank number |
| `VOTE_COLLECT_GRACE_S` | election.py | 0.05 s | Defined but unused |
| `LISTEN_ONLY_TIMEOUT_S` | failover_coordinator.py | 1.0 s | Boot listen window |

Guidance:
- In noisier RF, raise `HEARTBEAT_INTERVAL_S` to 0.3–0.5 s and keep the 3-miss
  ratio (DESIGN.md §1).
- `FC_LINK_STALE_S` must be comfortably longer than the slowest telemetry
  stream interval. If PX4 or MAVSDK publishes battery and flight mode at about
  1 Hz, 1.0 s is borderline and can cause false FC-link-lost step-downs. Check
  the stream rates on your rig, or raise this value or the MAVSDK rates.
- Keep `priority_rank × BACKOFF_PRIORITY_WEIGHT_S` below `BACKOFF_BASE_S` for
  your largest rank. Otherwise backoffs clamp to 0 and stop staggering.

## PX4 parameters to review

This layer depends on PX4 failsafes when its own process or link dies. Check:
- Offboard-loss behavior (`COM_OBL_RC_ACT`, `COM_OF_LOSS_T`) for a follower
  whose Pi dies while it is in offboard.
- Battery failsafe thresholds (`BAT_CRIT_THR`, `BAT_EMERGEN_THR`, `COM_LOW_BAT_ACT`).
  The design assumes the PX4 hard failsafe is around 15 %, below this layer's
  20 % voluntary step-down.
- Data-link and RC-loss actions.
