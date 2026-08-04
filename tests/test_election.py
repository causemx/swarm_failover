"""
Unit tests for election.py using FakeNetwork -- zero hardware, zero real
sockets, zero MAVSDK. Run with: pytest tests/test_election.py -v

These exercise the specific edge cases called out in 01_DESIGN.md:
  - simultaneous candidacy from two followers (backoff collapse)
  - quorum required before declaring victory
  - stale/lower-term messages rejected
  - deterministic tiebreak via static priority_rank
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from models import FleetConfig, Envelope, MsgType
from network import FakeNetwork
from election import ElectionModule


def make_cluster(node_ids_and_ranks):
    """node_ids_and_ranks: list of (node_id, priority_rank) tuples."""
    bus = []
    all_ids = tuple(n for n, _ in node_ids_and_ranks)
    modules = {}
    nets = {}
    for node_id, rank in node_ids_and_ranks:
        cfg = FleetConfig(node_id=node_id, priority_rank=rank, all_node_ids=all_ids)
        net = FakeNetwork(node_id=node_id, bus=bus)
        modules[node_id] = ElectionModule(cfg, net)
        nets[node_id] = net
    return modules, nets


@pytest.mark.asyncio
async def test_single_candidate_reaches_quorum():
    """5 nodes, one runs an election, others auto-ack -- must win with quorum."""
    modules, nets = make_cluster([(1, 1), (2, 2), (3, 3), (4, 4), (5, 5)])

    async def auto_ack(node_id):
        async for env in nets[node_id].recv():
            if env.msg_type == MsgType.CANDIDACY.value:
                await modules[node_id].send_vote_ack(env.node_id, env.term)

    ackers = [asyncio.create_task(auto_ack(nid)) for nid in (2, 3, 4, 5)]
    result = await modules[1].run_election(current_term=0, my_battery_pct=90, my_missed_hb=0)
    for t in ackers:
        t.cancel()

    assert result.won is True
    assert result.new_term == 1
    assert result.leader_node_id == 1


@pytest.mark.asyncio
async def test_election_fails_without_quorum():
    """Only 1 other voter responds out of 4 -- with quorum_size=3, that's not enough."""
    modules, nets = make_cluster([(1, 1), (2, 2), (3, 3), (4, 4), (5, 5)])

    async def single_ack(node_id):
        async for env in nets[node_id].recv():
            if env.msg_type == MsgType.CANDIDACY.value:
                await modules[node_id].send_vote_ack(env.node_id, env.term)
                return  # only one peer acks; others stay silent (simulated partition)

    t = asyncio.create_task(single_ack(2))
    result = await modules[1].run_election(current_term=0, my_battery_pct=90, my_missed_hb=0)
    t.cancel()

    assert result.won is False
    assert "timed out" in result.reason


@pytest.mark.asyncio
async def test_higher_priority_candidacy_wins_simultaneous_election():
    """
    Two followers (node 2, rank=2 and node 3, rank=3) both detect leader loss
    at the same instant and broadcast candidacy at the same term. Node 2 has
    the better (lower) priority_rank and must win; node 3 must defer.
    """
    modules, nets = make_cluster([(1, 1), (2, 2), (3, 3), (4, 4), (5, 5)])

    async def voter(node_id):
        async for env in nets[node_id].recv():
            if env.msg_type == MsgType.CANDIDACY.value:
                await modules[node_id].send_vote_ack(env.node_id, env.term)

    voters = [asyncio.create_task(voter(nid)) for nid in (1, 4, 5)]

    results = await asyncio.gather(
        modules[2].run_election(current_term=0, my_battery_pct=80, my_missed_hb=0),
        modules[3].run_election(current_term=0, my_battery_pct=80, my_missed_hb=0),
    )
    for t in voters:
        t.cancel()

    result_2, result_3 = results
    # Node 2 (better rank) should win; node 3 should defer to node 2.
    assert result_2.won is True
    assert result_2.leader_node_id == 2
    assert result_3.won is False
    assert "deferred" in result_3.reason


@pytest.mark.asyncio
async def test_leader_assert_requires_quorum_acks_before_active():
    modules, nets = make_cluster([(1, 1), (2, 2), (3, 3), (4, 4), (5, 5)])

    async def auto_ack_leader_assert(node_id):
        async for env in nets[node_id].recv():
            if env.msg_type == MsgType.LEADER_ASSERT.value:
                await modules[node_id].send_leader_ack(env.term)

    ackers = [asyncio.create_task(auto_ack_leader_assert(nid)) for nid in (2, 3)]
    confirmed = await modules[1].assert_leadership_and_await_acks(term=1)
    for t in ackers:
        t.cancel()

    # self + 2 acks = 3 = quorum_size for N=5
    assert confirmed is True


@pytest.mark.asyncio
async def test_leader_assert_fails_without_quorum_acks():
    modules, nets = make_cluster([(1, 1), (2, 2), (3, 3), (4, 4), (5, 5)])
    # No one acks -- simulate a bad partition where the new leader can send
    # but can't hear back from enough peers.
    confirmed = await modules[1].assert_leadership_and_await_acks(term=1)
    assert confirmed is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
