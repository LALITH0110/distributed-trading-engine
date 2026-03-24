"""
engine/zmq_factory.py — ZeroMQ socket factory with correct defaults.

CRITICAL: zmq.Context() must be created INSIDE the multiprocessing.Process
target function — never before fork. Pass the already-created context here.

Usage:
    def worker(topo):
        ctx = zmq.Context(io_threads=topo.zmq.io_threads)  # AFTER fork
        pub = zmq_factory.make_pub(ctx, topo)
        pub.bind(topo.get_bind_addr("feed_pub"))
        ...
        pub.close()
        ctx.term()
"""

from __future__ import annotations

import time

import zmq

from engine.config import Topology


def _apply_defaults(sock: zmq.Socket, topo: Topology) -> zmq.Socket:
    """Apply LINGER and HWM to every socket. IPV6 not set (data-plane is IPv4)."""
    sock.setsockopt(zmq.LINGER, topo.zmq.linger_ms)
    sock.setsockopt(zmq.SNDHWM, topo.zmq.sndhwm)
    sock.setsockopt(zmq.RCVHWM, topo.zmq.rcvhwm)
    return sock


def make_pub(ctx: zmq.Context, topo: Topology) -> zmq.Socket:
    """PUB socket with LINGER + HWM. Caller must bind."""
    sock = ctx.socket(zmq.PUB)
    return _apply_defaults(sock, topo)


def make_sub(ctx: zmq.Context, topo: Topology, topic: bytes = b"") -> zmq.Socket:
    """SUB socket with LINGER + HWM.
    Subscribes to `topic` immediately — setsockopt(SUBSCRIBE) is mandatory
    or the socket receives nothing (silent failure).
    Caller must connect after receiving this socket.
    """
    sock = ctx.socket(zmq.SUB)
    _apply_defaults(sock, topo)
    sock.setsockopt(zmq.SUBSCRIBE, topic)  # MUST be called before connect
    return sock


def make_push(ctx: zmq.Context, topo: Topology) -> zmq.Socket:
    """PUSH socket with LINGER + HWM. Caller must connect to PULL endpoint."""
    sock = ctx.socket(zmq.PUSH)
    return _apply_defaults(sock, topo)


def make_pull(ctx: zmq.Context, topo: Topology) -> zmq.Socket:
    """PULL socket with LINGER + HWM. Caller must bind."""
    sock = ctx.socket(zmq.PULL)
    return _apply_defaults(sock, topo)


def sleep_for_connect(topo: Topology) -> None:
    """Sleep connect_sleep_ms after connect() to avoid slow-joiner message loss.
    Call this after every connect() before the first recv().
    """
    time.sleep(topo.zmq.connect_sleep_ms / 1000.0)
