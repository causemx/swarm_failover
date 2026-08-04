# Test Plan (Deliverable 6)

## 1. Unit tests — election logic in isolation (no hardware)

File: `tests/test_election.py`, using `network.FakeNetwork` (an in-memory broadcast bus — see
`network.py`). These require zero sockets and zero MAVSDK, and were run standalone as a smoke test
during development:

| Test | Verifies |
|---|---|
| `test_single_candidate_reaches_quorum` | One candidate, quorum ACKs arrive → wins, term increments by 1 |
| `test_election_fails_without_quorum` | Only 1/4 peers reachable (simulated partial partition) → election correctly times out rather than declaring a false win |
| `test_higher_priority_candidacy_wins_simultaneous_election` | The explicit edge case: two followers detect loss simultaneously and both broadcast candidacy at the same term — the lower-priority-rank node must win, the other must defer, deterministically |
| `test_leader_assert_requires_quorum_acks_before_active` | New leader does not enter LEADER_ACTIVE (i.e. does not resume commanding) until quorum LEADER_ACK is received |
| `test_leader_assert_fails_without_quorum_acks` | If nobody acks the assert (partition), leader correctly does *not* proceed to active — this is the "elected but unconfirmed" conservative fallback from `failover_coordinator.py` |

Run with `pip install pytest pytest-asyncio --break-system-packages` then
`pytest tests/test_election.py -v` in an environment with network access (this sandbox has none,
so the equivalent logic was verified via a standalone asyncio smoke script — both `run_election`
quorum and simultaneous-candidacy deferral behaved as designed).

**Gaps to fill before hardware:** add tests for `health_monitor.py`'s missed-heartbeat counting
and self-health loop using a fake `mavsdk` stub (mock `latest()` to return a
`VehicleTelemetryState` with `fc_link_ok=False` and assert `on_self_failure_detected` fires with
`FC_LINK_LOST` — same pattern, just needs a `FakeMavsdk` stub class), and a full
`failover_coordinator.py` integration test wiring `FakeNetwork` + `FakeMavsdk` together to walk
the entire BOOT→FOLLOWER→HOLD_SUSPECT→CANDIDATE→LEADER_ELECT→LEADER_ACTIVE path end to end.

## 2. PX4 SITL multi-instance simulation plan (before touching real hardware)

Goal: validate the full state machine and timing budget with 5 simulated vehicles on one
development machine, before any real RF/AP variability is involved.

1. **Bring up 5 PX4 SITL instances** using `gazebo`/`jmavsim` multi-instance scripts
   (`Tools/simulation/gazebo-classic/sitl_multiple_run.sh -n 5` or the equivalent for your PX4
   1.16.1 checkout), each on a distinct MAVLink UDP port, simulating 5 separate "companion
   computers" as 5 separate `main.py` processes on localhost (use `127.0.0.1` broadcast
   equivalent, e.g. a loopback multicast group, since real UDP broadcast doesn't traverse
   localhost cleanly for 5 distinct processes — substitute a simple UDP multicast group in
   `network.py` for this test phase only).
2. **Baseline run:** all 5 healthy, confirm exactly one leader emerges, confirm formation-hold
   commands propagate, confirm heartbeat traffic rate matches the ~25 msg/sec total estimate from
   `01_DESIGN.md` section 1.
3. **Kill the leader process (`kill -9`)** mid-formation-flight → measure wall-clock time from
   kill to a new `LEADER_ACTIVE` state in another node's logs. This is the direct measurement
   against the **15s failover budget** — target well under that given the 600ms detection + ~2s
   worst-case election/ack overhead this design implies.
4. **Simultaneous dual-candidacy:** kill the leader, and additionally inject artificial network
   delay (`tc netem` on the loopback/veth pair) so two followers' timeout windows overlap almost
   exactly → confirm only one becomes leader (this exercises the same code path as
   `test_higher_priority_candidacy_wins_simultaneous_election` but over real (loopback) sockets
   instead of `FakeNetwork`).
5. **Partition simulation:** use `iptables`/`tc` to drop packets between two subsets of the 5
   SITL processes (e.g. {1,2} can't reach {3,4,5}) → confirm the group of 3 elects a leader and
   the group of 2 does not (quorum rule from section 4), and confirm the isolated pair holds
   position rather than electing a rogue leader.
6. **Full AP-down simulation:** drop all UDP traffic between all 5 processes simultaneously →
   confirm every node eventually falls back to its own PX4 SITL native failsafe (position hold),
   confirming the no-leader deadlock is handled safely rather than silently.
7. **Cascading failover:** kill the newly-elected leader again within a few seconds of it taking
   over → confirm a second election runs cleanly, term increments correctly, no residual state
   from the first election causes a stuck node.
8. **Battery-triggered voluntary handoff:** in SITL, artificially inject a low battery telemetry
   value on the leader → confirm `DEGRADE_WARN`/voluntary step-down path is faster/cleaner than
   the timeout path (should show up as a materially shorter failover time in the logs than the
   `kill -9` case, since the leader announces itself proactively per section 2).
9. **Zombie leader:** in SITL, sever only the *MAVLink FC↔companion* link (not the network link)
   on the leader's companion process while leaving its network process alive → confirm it
   self-detects `FC_LINK_LOST` and broadcasts `STEP_DOWN` rather than requiring a timeout from
   peers, and confirm followers reject any stale `FORMATION_CMD` it might still emit before fully
   stepping down.

## 3. Hardware-in-the-loop (HIL) checklist — RPi 5B / PX4 rig

Run this checklist on the bench (props off / tethered) before any flight test, then repeat a
reduced subset in a real flight test with appropriate safety pilot oversight.

- [ ] Confirm UART link (PX4 ↔ RPi 5B) parameters match `--fc-address` connection string; verify
      MAVSDK-python 3.5.13 connects and streams telemetry (`battery`, `flight_mode`, `armed`) at
      expected rate on real hardware, not just SITL.
- [ ] Confirm UDP broadcast actually reaches all 5 RPis on the real infrastructure AP — some
      consumer/enterprise APs block client-to-client broadcast by default ("AP isolation"); this
      must be explicitly disabled on the AP, or the entire failover layer is silently non-functional
      even though each drone individually looks healthy.
- [ ] Measure real heartbeat round-trip / jitter on the actual AP under load (5 companion
      computers + any onboard vision/telemetry) to confirm the 200ms/3-miss thresholds from
      section 1 still hold in your real RF environment — increase if you observe frequent
      false-positive suspicions.
- [ ] Verify RPi 5B CPU/network overhead of the failover process alongside MAVSDK-python and any
      onboard vision workload stays within budget (measure with `htop`/`iftop` during a live
      election storm test, not just steady-state heartbeating).
- [ ] Bench-test physical leader-kill: literally power off one RPi 5B (not just kill the process)
      to confirm the OS-level network departure is detected the same way as a process kill.
- [ ] Bench-test the AP-isolation / full-partition case physically (turn off Wi-Fi on the AP or
      power-cycle it) with props off, confirm every RPi/PX4 pair independently falls back to PX4
      native failsafe rather than doing something undefined.
- [ ] Confirm logs (local + relayed `SWARM_LOG` to the CLI GCS) are legible and timestamped
      consistently enough to reconstruct an incident timeline after a bench test, since real wall
      clocks will differ slightly per A3 — logs should show enough term/event ordering to
      reconstruct sequence without relying on wall-clock alignment.
- [ ] Only after all of the above pass: a real flight test with a safety pilot able to take manual
      control of any vehicle at any time, starting with a *simulated* leader failure via companion
      software kill switch (not an actual power failure) at low altitude over a safe area, before
      testing a real power-loss failover in flight.
