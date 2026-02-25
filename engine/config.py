"""
engine/config.py — Topology configuration loader.

Usage:
    from engine.config import load_topology
    topo = load_topology()
    feed_bind = topo.get_bind_addr("feed_pub")    # tcp://127.0.0.1:5555
    feed_conn = topo.get_connect_addr("feed_pub") # tcp://127.0.0.1:5555

Environment variable overrides:
    DEPLOYMENT_MODE         — "local" or "fabric" (overrides topology.yaml)
    FABRIC_FEED_HOST        — IP/hostname of feed handler node
    FABRIC_ME_HOST          — IP/hostname of matching engine node
    FABRIC_RISK_HOST        — IP/hostname of risk gateway node
    FABRIC_DASH_HOST        — IP/hostname of dashboard node
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

# Default path — relative to project root
_DEFAULT_TOPOLOGY = Path(__file__).parent.parent / "config" / "topology.yaml"

# Env var → host key mapping for fabric mode
_FABRIC_ENV_MAP = {
    "feed":            "FABRIC_FEED_HOST",
    "matching_engine": "FABRIC_ME_HOST",
    "risk_gateway":    "FABRIC_RISK_HOST",
    "dashboard":       "FABRIC_DASH_HOST",
}


@dataclass(frozen=True)
class ZmqSettings:
    linger_ms: int
    io_threads: int
    sndhwm: int
    rcvhwm: int
    connect_sleep_ms: int


@dataclass(frozen=True)
class MESettings:
    ring_buffer_size: int    # deque maxlen per symbol (ME-03 default 65536)
    orphan_timeout_s: float  # seconds of silence before GC cancels orders (ME-09)
    gc_interval_s: float     # how often GC runs in match loop


@dataclass(frozen=True)
class RGSettings:
    fat_finger_max_notional: float  # RISK-01: max notional per order (default $100K)
    position_limit: int             # RISK-02: max abs net position per symbol per strategy
    rate_limit_per_s: float         # RISK-03: token bucket refill rate
    token_bucket_capacity: int      # RISK-03: max burst capacity
    inbound_rcvhwm: int             # RISK-06: inbound PULL HWM


@dataclass(frozen=True)
class Topology:
    deployment_mode: str          # "local" or "fabric"
    _raw: dict                    # full parsed YAML (private)
    zmq: ZmqSettings
    me: MESettings
    rg: RGSettings

    def _resolve_host(self, host_key: str) -> str:
        """Resolve host for current deployment_mode, with env-var override."""
        if self.deployment_mode == "fabric":
            env_var = _FABRIC_ENV_MAP.get(host_key)
            if env_var:
                val = os.environ.get(env_var)
                if not val:
                    raise RuntimeError(
                        f"deployment_mode=fabric but {env_var} not set. "
                        f"Export {env_var}=<FABRIC_IP> before starting."
                    )
                return val
        # local mode: read from hosts.local section
        return self._raw["hosts"]["local"][host_key]

    def _endpoint(self, endpoint_key: str) -> dict:
        ep = self._raw["endpoints"].get(endpoint_key)
        if ep is None:
            raise KeyError(f"No endpoint '{endpoint_key}' in topology.yaml")
        return ep

    def get_bind_addr(self, endpoint_key: str) -> str:
        """Returns tcp://HOST:PORT for the socket that binds (server side)."""
        ep = self._endpoint(endpoint_key)
        host = self._resolve_host(ep["bind_host_key"])
        return f"tcp://{host}:{ep['port']}"

    def get_connect_addr(self, endpoint_key: str) -> str:
        """Returns tcp://HOST:PORT for sockets that connect (client side).
        For loopback, bind and connect addresses are identical.
        """
        return self.get_bind_addr(endpoint_key)

    def get_port(self, endpoint_key: str) -> int:
        return int(self._endpoint(endpoint_key)["port"])


def load_topology(path: Optional[Path] = None) -> Topology:
    """Load topology.yaml and return a Topology instance.

    deployment_mode is read from YAML first, then overridden by
    DEPLOYMENT_MODE env var if set.
    """
    if path is None:
        env_path = os.environ.get("ME_TOPOLOGY_PATH")
        path = Path(env_path) if env_path else _DEFAULT_TOPOLOGY
    with open(path, "r") as f:
        raw = yaml.safe_load(f)

    # Env-var override for deployment mode
    mode = os.environ.get("DEPLOYMENT_MODE", raw.get("deployment_mode", "local")).lower()
    if mode not in ("local", "fabric"):
        raise ValueError(f"DEPLOYMENT_MODE must be 'local' or 'fabric', got '{mode}'")

    zmq_cfg = raw.get("zmq", {})
    zmq = ZmqSettings(
        linger_ms=zmq_cfg.get("linger_ms", 100),
        io_threads=zmq_cfg.get("io_threads", 2),
        sndhwm=zmq_cfg.get("sndhwm", 10000),
        rcvhwm=zmq_cfg.get("rcvhwm", 10000),
        connect_sleep_ms=zmq_cfg.get("connect_sleep_ms", 100),
    )

    me_cfg = raw.get("matching_engine", {})
    me = MESettings(
        ring_buffer_size=me_cfg.get("ring_buffer_size", 65536),
        orphan_timeout_s=float(me_cfg.get("orphan_timeout_s", 10.0)),
        gc_interval_s=float(me_cfg.get("gc_interval_s", 5.0)),
    )

    rg_cfg = raw.get("risk_gateway", {})
    rg = RGSettings(
        fat_finger_max_notional=float(rg_cfg.get("fat_finger_max_notional", 100_000)),
        position_limit=int(rg_cfg.get("position_limit", 10)),
        rate_limit_per_s=float(rg_cfg.get("rate_limit_per_s", 10.0)),
        token_bucket_capacity=int(rg_cfg.get("token_bucket_capacity", 10)),
        inbound_rcvhwm=int(rg_cfg.get("inbound_rcvhwm", 1000)),
    )

    return Topology(deployment_mode=mode, _raw=raw, zmq=zmq, me=me, rg=rg)
