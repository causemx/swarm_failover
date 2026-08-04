"""
Entry point. Run one instance of this per drone, on that drone's RPi 5B.

Usage:
    python main.py --node-id 1 --priority-rank 1 --fleet 1,2,3,4,5 \
        --fc-address serial:///dev/ttyAMA0:921600 --udp-port 14650
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from models import FleetConfig
from network import UdpBroadcastNetwork
from mavsdk_interface import MavsdkInterface
from gcs_relay import GcsRelay
from failover_coordinator import FailoverCoordinator


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--node-id", type=int, required=True)
    p.add_argument("--priority-rank", type=int, required=True,
                    help="Lower = higher priority. Must be unique across the fleet.")
    p.add_argument("--fleet", type=str, required=True,
                    help="Comma-separated list of all node ids, e.g. 1,2,3,4,5")
    p.add_argument("--fc-address", type=str, default="serial:///dev/ttyAMA0:921600")
    p.add_argument("--udp-port", type=int, default=14650)
    return p.parse_args()


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
    )
    args = parse_args()
    all_ids = tuple(int(x) for x in args.fleet.split(","))

    cfg = FleetConfig(
        node_id=args.node_id,
        priority_rank=args.priority_rank,
        all_node_ids=all_ids,
    )

    net = UdpBroadcastNetwork(bind_port=args.udp_port)
    mavsdk = MavsdkInterface(system_address=args.fc_address)
    gcs = GcsRelay(node_id=cfg.node_id, net=net)

    coordinator = FailoverCoordinator(cfg, net, mavsdk, gcs)
    await coordinator.run()


if __name__ == "__main__":
    asyncio.run(main())
