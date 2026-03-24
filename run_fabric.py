#!/usr/bin/env python3
"""run_fabric.py — Full end-to-end FABRIC deployment + benchmarks + fault injection.

Run with: /usr/local/bin/python3.12 run_fabric.py 2>&1 | tee run_fabric.log

Stages:
  1.  Init FABRIC (credentials from ./creds/)
  2.  Create or reuse slice (feed-node, engine-node, strat-0..7)
  3.  Install Python 3.11 + deps on all nodes  [parallel]
  4.  Clone repo + compile proto               [parallel]
  5.  Write topology.yaml + .env on all nodes
  6.  Connectivity check
  7.  Smoke test (1 strategy, 30s)
  8.  Scaling benchmarks (1/2/4/8 × 2 scenarios × 5 trials)
  9.  Fault injection (kill strat-7 at t=30s)
  10. Download logs
  11. Delete slice
"""
import json
import os
import sys
import threading
import time
import traceback
import warnings
import logging
from datetime import datetime

warnings.filterwarnings('ignore')
logging.disable(logging.WARNING)

def ts():
    return datetime.now().strftime('%H:%M:%S')

def log(msg):
    print(f'[{ts()}] {msg}', flush=True)

# ── Credentials ────────────────────────────────────────────────────────────────
CREDS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'creds')

from fabrictestbed_extensions.fablib.fablib import FablibManager as fablib_manager

log('[1] Initializing FABRIC...')
fablib = fablib_manager(
    credmgr_host='cm.fabric-testbed.net',
    orchestrator_host='orchestrator.fabric-testbed.net',
    project_id='c59f6f2b-c2e4-4950-bc18-ecec5b9f38c3',
    token_location=f'{CREDS}/id_token.json',
    bastion_username='lkothuru_0000375421',
    bastion_key_location=f'{CREDS}/fabric_cc',
    bastion_host='bastion.fabric-testbed.net',
    slice_private_key_file=f'{CREDS}/slice_cc',
    slice_public_key_file=f'{CREDS}/slice_cc.pub',
)

SLICE_NAME = 'trading_engine'
SITE       = 'EDUKY'   # project tagged OnlyEDUKY
IMAGE      = 'default_ubuntu_22'
MAX_STRAT  = 8
REPO_URL   = 'https://github.com/LALITH0110/distributed-trading-engine.git'

log('    Connected!')

# ── Helpers ────────────────────────────────────────────────────────────────────
def write_script(node, remote_path, script_body):
    """Write a Python script to a remote node via heredoc."""
    node.execute(
        f"cat > {remote_path} << 'PYEOF'\n{script_body}\nPYEOF",
        quiet=True
    )

def kill_all(slice_):
    for n in slice_.get_nodes():
        try:
            if n.get_reservation_state() in ('Active', 'Ticketed'):
                n.execute('pkill -9 -f "venv311\\|run_engine\\|run_feed\\|run_strat" 2>/dev/null; true', quiet=True)
        except Exception:
            pass
    time.sleep(1)

# Remote script templates — write to files on nodes, avoid quoting hell
ENGINE_SCRIPT = """\
import os, sys, time, json
sys.path.insert(0, os.path.expanduser('~/trading_engine'))
for line in open(os.path.expanduser('~/trading_engine/.env')):
    line = line.strip()
    if line.startswith('export ') and '=' in line:
        k, v = line[7:].split('=', 1)
        os.environ[k] = v
from engine.matching_engine import start_matching_engine
from engine.risk_gateway import start_risk_gateway
me = start_matching_engine()
rg = start_risk_gateway()
print(f'ME alive={me.is_alive()} RG alive={rg.is_alive()}', flush=True)
end = time.time() + DURATION
while time.time() < end:
    time.sleep(5)
    try:
        with open('/tmp/me_metrics.json') as _f:
            m = json.load(_f)
        print(f'METRICS: orders={m["orders"]} throughput={m["throughput"]} p50={m["p50_us"]}us p99={m["p99_us"]}us', flush=True)
    except Exception:
        print(f'METRICS: (none yet) t={int(time.time()-end+DURATION)}s', flush=True)
me.terminate(); rg.terminate()
me.join(timeout=10); rg.join(timeout=10)
print('engine done', flush=True)
"""

FEED_SCRIPT = """\
import os, sys, time
sys.path.insert(0, os.path.expanduser('~/trading_engine'))
for line in open(os.path.expanduser('~/trading_engine/.env')):
    line = line.strip()
    if line.startswith('export ') and '=' in line:
        k, v = line[7:].split('=', 1)
        os.environ[k] = v
from engine.feed_handler import start_feed_handler
fh = start_feed_handler(mode='MODE')
print(f'FH alive={fh.is_alive()}', flush=True)
time.sleep(DURATION)
print('feed done', flush=True)
"""

STRAT_SCRIPT = """\
import os, sys, time
sys.path.insert(0, os.path.expanduser('~/trading_engine'))
for line in open(os.path.expanduser('~/trading_engine/.env')):
    line = line.strip()
    if line.startswith('export ') and '=' in line:
        k, v = line[7:].split('=', 1)
        os.environ[k] = v
from engine.strategy import MeanReversionStrategy, start_strategy
p = start_strategy(MeanReversionStrategy('STRAT_ID', lookback=20, z_buy=ZBUY, z_sell=ZSELL, heartbeat_timeout_s=30.0))
print(f'strat STRAT_ID alive={p.is_alive()}', flush=True)
time.sleep(DURATION)
print('strat STRAT_ID done', flush=True)
"""

def launch_engine(node, duration):
    s = ENGINE_SCRIPT.replace('DURATION', str(duration))
    write_script(node, '/tmp/run_engine.py', s)
    node.execute('nohup ~/venv311/bin/python /tmp/run_engine.py > /tmp/engine.log 2>&1 &', quiet=True)

def launch_feed(node, mode, duration):
    s = FEED_SCRIPT.replace('MODE', mode).replace('DURATION', str(duration))
    write_script(node, '/tmp/run_feed.py', s)
    node.execute('nohup ~/venv311/bin/python /tmp/run_feed.py > /tmp/feed.log 2>&1 &', quiet=True)

def launch_strat(node, strat_id, z_buy, z_sell, duration):
    s = (STRAT_SCRIPT
         .replace('STRAT_ID', strat_id)
         .replace('ZBUY', str(z_buy))
         .replace('ZSELL', str(z_sell))
         .replace('DURATION', str(duration)))
    write_script(node, '/tmp/run_strat.py', s)
    node.execute('nohup ~/venv311/bin/python /tmp/run_strat.py > /tmp/strat.log 2>&1 &', quiet=True)

def check_log(node, logfile, label):
    """Fetch last 20 lines of a remote log; warn if empty."""
    out, _ = node.execute(f'tail -20 {logfile} 2>/dev/null || echo "(empty)"', quiet=True)
    out = out.strip()
    log(f'    [{label}] {out[:300] if out else "(no output — check log)"}')
    if not out or out == '(empty)':
        log(f'    WARNING: {label} log empty — process may have crashed!')
    return out

# ── Main (with cleanup on exit) ───────────────────────────────────────────────
slice_ = None

try:
    # ── Step 2: Create or reuse slice ─────────────────────────────────────────
    existing = [s for s in fablib.get_slices() if s.get_name() == SLICE_NAME]
    slice_exists = bool(existing)

    if slice_exists:
        slice_ = fablib.get_slice(SLICE_NAME)
        # Check if setup is actually complete (repo + env present on engine-node)
        check_out, _ = slice_.get_node('engine-node').execute(
            'test -f ~/trading_engine/engine/__init__.py && test -f ~/trading_engine/.env && echo "ready" || echo "missing"', quiet=True
        )
        SKIP_SETUP = 'ready' in check_out
        if SKIP_SETUP:
            log(f'    Reusing slice "{SLICE_NAME}" — setup verified complete.')
        else:
            log(f'    Slice exists but setup incomplete — will run setup steps.')
    else:
        SKIP_SETUP = False

    if not slice_exists:
        log(f'[2] Creating slice "{SLICE_NAME}" at {SITE}...')
        slice_ = fablib.new_slice(name=SLICE_NAME)
        feed_n   = slice_.add_node(name='feed-node',   image=IMAGE, cores=2, ram=8, disk=10, site=SITE)
        engine_n = slice_.add_node(name='engine-node', image=IMAGE, cores=2, ram=8, disk=10, site=SITE)
        strat_ns = [slice_.add_node(name=f'strat-{i}', image=IMAGE, cores=2, ram=4, disk=10, site=SITE)
                    for i in range(MAX_STRAT)]
        # L2 data-plane network — FABRIC management IPs block inter-node TCP;
        # NIC_Basic gives each node a direct L2 interface with no security-group restrictions.
        data_net = slice_.add_l2network(name='data-net')
        for node in [feed_n, engine_n] + strat_ns:
            nic = node.add_component(model='NIC_Basic', name='dp-nic')
            data_net.add_interface(nic.get_interfaces()[0])
        slice_.submit()
        log('    Submitted — waiting for SSH...')
        slice_.wait_ssh(progress=True)
        log('    All nodes ready!')

    # ── Step 3: IPs ───────────────────────────────────────────────────────────
    log('[3] Node IPs:')
    slice_ = fablib.get_slice(SLICE_NAME)
    node_ips = {n.get_name(): str(n.get_management_ip()) for n in slice_.get_nodes()}
    for name, ip in sorted(node_ips.items()):
        log(f'    {name}: {ip}')

    # Data-plane IPv4 addresses (10.1.0.x) — used for all ZMQ connections
    DATA_IPS = {
        'feed-node':   '10.1.0.1',
        'engine-node': '10.1.0.2',
        **{f'strat-{i}': f'10.1.0.{10 + i}' for i in range(MAX_STRAT)},
    }
    FEED_IP   = DATA_IPS['feed-node']
    ENGINE_IP = DATA_IPS['engine-node']

    # Configure data-plane IPs on NIC_Basic interfaces (idempotent)
    def config_dp(node):
        name = node.get_name()
        ip   = DATA_IPS.get(name)
        if not ip:
            return
        # Find the non-management, non-loopback interface (NIC_Basic adds one)
        out, _ = node.execute(
            "ip -o link show | awk -F': ' '$2 !~ /^lo$|^enp3s0$/ {print $2}' | head -1",
            quiet=True
        )
        dp_iface = out.strip()
        if dp_iface:
            node.execute(
                f'sudo ip addr replace {ip}/24 dev {dp_iface} && '
                f'sudo ip link set {dp_iface} up',
                quiet=True
            )
            log(f'    {name}: dp={dp_iface} ip={ip}')
        else:
            log(f'    {name}: WARNING — no data-plane interface found!')

    log('[3b] Configuring data-plane IPs + flushing iptables...')
    def config_dp_and_fw(node):
        config_dp(node)
        # Flush iptables to ensure ZMQ ports are not blocked
        node.execute('sudo iptables -F && sudo iptables -P INPUT ACCEPT && sudo iptables -P FORWARD ACCEPT && sudo iptables -P OUTPUT ACCEPT', quiet=True)
        # Patch zmq_factory: remove IPV6=1 (data-plane is IPv4, AF_INET6 causes issues)
        node.execute(
            "sed -i '/sock.setsockopt(zmq.IPV6/d' ~/trading_engine/engine/zmq_factory.py",
            quiet=True
        )
    ts_dp = [threading.Thread(target=config_dp_and_fw, args=(n,)) for n in slice_.get_nodes()]
    for t in ts_dp: t.start()
    for t in ts_dp: t.join()
    log('    Data-plane IPs configured + iptables flushed + zmq_factory patched.')

    # ── Step 4: Install Python 3.11 ───────────────────────────────────────────
    if not SKIP_SETUP:
        log('[4] Installing Python 3.11 (parallel)...')
        # Use venv — avoids all pip discovery issues across nodes
        # libzmq3-dev needed so pyzmq links against system libzmq (avoids import errors)
        INSTALL = (
            'sudo apt-get update -qq && '
            'sudo add-apt-repository -y ppa:deadsnakes/ppa 2>/dev/null && '
            'sudo apt-get update -qq && '
            'sudo apt-get install -y -qq git python3.11 python3.11-venv python3.11-dev '
            'libzmq3-dev libffi-dev build-essential && '
            'python3.11 -m venv ~/venv311 && '
            '~/venv311/bin/pip install --upgrade pip setuptools wheel -q'
        )

        def do_install(node):
            t0 = time.time()
            stdout, stderr = node.execute(INSTALL, quiet=True, timeout=600)
            ver, _   = node.execute('~/venv311/bin/python --version', quiet=True)
            pip_ver, _ = node.execute('~/venv311/bin/pip --version', quiet=True)
            if 'Python 3.11' not in ver:
                log(f'    {node.get_name()}: WARN venv broken — {ver.strip()} | stderr: {stderr[:100]}')
            else:
                log(f'    {node.get_name()}: {ver.strip()} | venv ok ({int(time.time()-t0)}s)')

        ts_ = [threading.Thread(target=do_install, args=(n,)) for n in slice_.get_nodes()]
        for t in ts_: t.start()
        for t in ts_: t.join()
        log('    Python 3.11 installed on all nodes.')

    # ── Step 5: Clone repo + install deps ────────────────────────────────────
    if not SKIP_SETUP:
        log('[5] Cloning repo + installing deps (parallel)...')
        CLONE = (
            f'rm -rf ~/trading_engine && '
            f'git clone -q {REPO_URL} ~/trading_engine && '
            f'~/venv311/bin/pip install -r ~/trading_engine/requirements.txt -q && '
            f"~/venv311/bin/python -c 'import sortedcontainers, zmq, yaml, numpy; print(\"deps ok\")' && "
            f'echo "clone done"'
        )
        VERIFY = "~/venv311/bin/python -c 'import sortedcontainers, zmq, yaml, numpy; print(\"deps ok\")'"

        failed_nodes = []
        ok_node = [None]  # first node that succeeded (for scp fallback)

        def do_clone(node):
            t0 = time.time()
            for attempt in range(2):
                stdout, stderr = node.execute(CLONE, quiet=True, timeout=600)
                combined = (stdout + stderr).strip()
                if 'deps ok' in combined and 'clone done' in combined:
                    log(f'    {node.get_name()}: deps ok ({int(time.time()-t0)}s)')
                    if ok_node[0] is None:
                        ok_node[0] = node
                    return
                if attempt < 1:
                    log(f'    {node.get_name()}: retry (pip failed)')
                    time.sleep(3)
            log(f'    {node.get_name()}: pip unreachable — will scp from peer')
            failed_nodes.append(node)

        ts_ = [threading.Thread(target=do_clone, args=(n,)) for n in slice_.get_nodes()]
        for t in ts_: t.start()
        for t in ts_: t.join()

        # Fallback: tar venv+repo from a working node, scp to broken ones
        if failed_nodes and ok_node[0]:
            donor = ok_node[0]
            log(f'    Packing venv+repo from {donor.get_name()} for {len(failed_nodes)} failed node(s)...')
            donor.execute(
                'cd ~ && tar czf /tmp/venv_repo.tar.gz venv311/ trading_engine/',
                quiet=True, timeout=120
            )
            donor_ip = DATA_IPS[donor.get_name()]
            for fn in failed_nodes:
                fn_name = fn.get_name()
                log(f'    Copying to {fn_name}...')
                # scp via data-plane IP (L2 network, no firewall)
                fn.execute(
                    f'scp -o StrictHostKeyChecking=no {donor_ip}:/tmp/venv_repo.tar.gz /tmp/ && '
                    f'cd ~ && rm -rf venv311 trading_engine && '
                    f'tar xzf /tmp/venv_repo.tar.gz && rm /tmp/venv_repo.tar.gz',
                    quiet=True, timeout=120
                )
                v_out, _ = fn.execute(VERIFY, quiet=True)
                if 'deps ok' in v_out:
                    log(f'    {fn_name}: deps ok (via scp)')
                else:
                    log(f'    {fn_name}: STILL FAILED after scp!')
        elif failed_nodes:
            log('    ERROR: all nodes failed pip install — no donor available!')

        log('    Repo + deps ready on all nodes.')

    # ── Step 6: Topology + env ─────────────────────────────────────────────────
    if not SKIP_SETUP:
        log('[6] Writing topology + env...')
        import yaml

        topo = {
            'deployment_mode': 'fabric',
            'hosts': {
                'local':  {k: '127.0.0.1' for k in ('feed','matching_engine','risk_gateway','dashboard')},
                'fabric': {'feed': FEED_IP, 'matching_engine': ENGINE_IP,
                           'risk_gateway': ENGINE_IP, 'dashboard': ENGINE_IP},
            },
            'endpoints': {
                'feed_pub':         {'role': 'PUB',  'bind_host_key': 'feed',            'port': 5555},
                'exec_report_pub':  {'role': 'PUB',  'bind_host_key': 'matching_engine', 'port': 5556},
                'heartbeat_pub':    {'role': 'PUB',  'bind_host_key': 'feed',            'port': 5557},
                'order_ingress':    {'role': 'PULL', 'bind_host_key': 'risk_gateway',    'port': 5558},
                'order_egress':     {'role': 'PULL', 'bind_host_key': 'matching_engine', 'port': 5559},
                'kill_switch':      {'role': 'PULL', 'bind_host_key': 'risk_gateway',    'port': 5560},
                'risk_reject_pub':  {'role': 'PUB',  'bind_host_key': 'risk_gateway',    'port': 5561},
                'order_cancel':     {'role': 'PULL', 'bind_host_key': 'matching_engine', 'port': 5562},
                'me_heartbeat_pub': {'role': 'PUB',  'bind_host_key': 'matching_engine', 'port': 5563},
                'rg_heartbeat_pub': {'role': 'PUB',  'bind_host_key': 'risk_gateway',    'port': 5564},
            },
            'zmq': {'linger_ms': 100, 'io_threads': 2, 'sndhwm': 10000,
                    'rcvhwm': 10000, 'connect_sleep_ms': 500},
            'matching_engine': {'ring_buffer_size': 65536, 'orphan_timeout_s': 10.0, 'gc_interval_s': 5.0},
            'risk_gateway': {'fat_finger_max_notional': 100000, 'position_limit': 100,
                             'rate_limit_per_s': 100, 'token_bucket_capacity': 100, 'inbound_rcvhwm': 10000},
        }
        topo_yaml = yaml.safe_dump(topo, default_flow_style=False)
        env_content = (
            f'export DEPLOYMENT_MODE=fabric\n'
            f'export FABRIC_FEED_HOST={FEED_IP}\n'
            f'export FABRIC_ME_HOST={ENGINE_IP}\n'
            f'export FABRIC_RISK_HOST={ENGINE_IP}\n'
            f'export FABRIC_DASH_HOST={ENGINE_IP}\n'
            f'export PYTHONPATH=$HOME/trading_engine\n'
            f'export STRATEGY_MODEL_PATH=$HOME/trading_engine/models/ml_signal_model.joblib\n'
        )

        for node in slice_.get_nodes():
            node.execute('mkdir -p ~/trading_engine/config', quiet=True)
            node.execute(f"cat > ~/trading_engine/config/topology.yaml << 'TEOF'\n{topo_yaml}TEOF", quiet=True)
            node.execute(f"cat > ~/trading_engine/.env << 'EEOF'\n{env_content}EEOF", quiet=True)
            node.execute(
                "grep -q 'trading_engine/.env' ~/.bashrc || "
                "echo 'source ~/trading_engine/.env' >> ~/.bashrc",
                quiet=True
            )
            log(f'    {node.get_name()}: topology + env written')

    engine = slice_.get_node('engine-node')
    feed   = slice_.get_node('feed-node')

    # ── Step 7: Connectivity ───────────────────────────────────────────────────
    log('[7] Connectivity check...')
    out, _ = feed.execute(f'ping -4 -c 2 -W 3 {ENGINE_IP}', quiet=True, timeout=15)
    log(f'    feed → engine: {"OK" if "2 received" in out or "2 packets" in out else "WARN — check network"}')
    strat0 = slice_.get_node('strat-0')
    out2, _ = strat0.execute(f'ping -4 -c 2 -W 3 {ENGINE_IP}', quiet=True, timeout=15)
    log(f'    strat-0 → engine: {"OK" if "2 received" in out2 or "2 packets" in out2 else "WARN — " + out2.strip()[-100:]}')
    out3, _ = strat0.execute(f'ping -4 -c 2 -W 3 {FEED_IP}', quiet=True, timeout=15)
    log(f'    strat-0 → feed: {"OK" if "2 received" in out3 or "2 packets" in out3 else "WARN — " + out3.strip()[-100:]}')

    # ── Step 7b: TCP + ZMQ connectivity test ──────────────────────────────────
    log('[7b] TCP + ZMQ connectivity test...')
    # TCP test
    engine.execute(f'nohup nc -l -p 9999 > /dev/null 2>&1 &', quiet=True)
    time.sleep(1)
    tcp_out, _ = strat0.execute(f'nc -z -w 3 {ENGINE_IP} 9999 && echo TCP_OK || echo TCP_FAIL', quiet=True, timeout=10)
    log(f'    strat-0 → engine TCP: {tcp_out.strip()}')
    ipt_out, _ = engine.execute('sudo iptables -L INPUT -n --line-numbers 2>/dev/null | head -5', quiet=True)
    log(f'    engine iptables: {ipt_out.strip()[:150]}')
    # ZMQ PUSH/PULL test: engine binds PULL on 9998, strat-0 PUSH sends "zmq_hello"
    zmq_recv_py = (
        "import zmq, sys\n"
        "ctx = zmq.Context()\n"
        "s = ctx.socket(zmq.PULL)\n"
        "s.bind('tcp://0.0.0.0:9998')\n"
        "s.setsockopt(zmq.RCVTIMEO, 8000)\n"
        "print('ZMQ_RECV_WAITING', flush=True)\n"
        "try:\n"
        "    r = s.recv()\n"
        "    print('ZMQ_RECV_GOT:' + r.decode(), flush=True)\n"
        "except zmq.error.Again:\n"
        "    print('ZMQ_RECV_TIMEOUT', flush=True)\n"
        "s.close(); ctx.term()\n"
    )
    zmq_send_py = (
        f"import zmq, time\n"
        f"ctx = zmq.Context()\n"
        f"s = ctx.socket(zmq.PUSH)\n"
        f"s.connect('tcp://{ENGINE_IP}:9998')\n"
        f"time.sleep(1)\n"
        f"s.send(b'zmq_hello')\n"
        f"print('ZMQ_SENT', flush=True)\n"
        f"time.sleep(0.5); s.close(); ctx.term()\n"
    )
    write_script(engine, '/tmp/zmq_recv.py', zmq_recv_py)
    write_script(strat0, '/tmp/zmq_send.py', zmq_send_py)
    engine.execute('nohup ~/venv311/bin/python /tmp/zmq_recv.py > /tmp/zmq_recv.log 2>&1 &', quiet=True)
    time.sleep(2)
    strat0.execute('~/venv311/bin/python /tmp/zmq_send.py > /tmp/zmq_send.log 2>&1', quiet=True, timeout=10)
    time.sleep(4)
    zmq_r, _ = engine.execute('cat /tmp/zmq_recv.log 2>/dev/null || echo "NO_LOG"', quiet=True)
    zmq_s, _ = strat0.execute('cat /tmp/zmq_send.log 2>/dev/null || echo "NO_LOG"', quiet=True)
    zmqf_check, _ = engine.execute('grep -c "IPV6" ~/trading_engine/engine/zmq_factory.py 2>/dev/null || echo 0', quiet=True)
    log(f'    ZMQ recv: {zmq_r.strip()[:80]} | send: {zmq_s.strip()[:40]} | zmq_factory IPV6 lines: {zmqf_check.strip()}')

    # ── Step 8: Smoke test ────────────────────────────────────────────────────
    log('[8] Smoke test (1 strat, 30s)...')
    kill_all(slice_)
    RUN = 40

    launch_engine(engine, RUN)
    time.sleep(3)
    launch_feed(feed, 'gbm', RUN)
    time.sleep(3)
    launch_strat(slice_.get_node('strat-0'), 'strat-001', -2.0, 2.0, RUN - 3)

    log('    Waiting 30s...')
    time.sleep(30)

    ep, _ = engine.execute('pgrep -f run_engine.py | wc -l', quiet=True)
    fp, _ = feed.execute('pgrep -f run_feed.py | wc -l', quiet=True)
    sp, _ = slice_.get_node('strat-0').execute('pgrep -f run_strat.py | wc -l', quiet=True)
    log(f'    engine:{ep.strip()} feed:{fp.strip()} strat-0:{sp.strip()} (all should be >0)')

    # Log output from each component
    check_log(engine, '/tmp/engine.log', 'engine')
    check_log(feed,   '/tmp/feed.log',   'feed')
    check_log(slice_.get_node('strat-0'), '/tmp/strat.log', 'strat-0')

    kill_all(slice_)
    log('    Smoke test complete.')

    # ── Step 9: Scaling benchmarks ────────────────────────────────────────────
    log('[9] Scaling benchmarks...')
    NODE_COUNTS = [1, 2, 4, 8]
    SCENARIOS   = ['gbm', 'stress']
    N_TRIALS    = 5
    BENCH_DUR   = 30
    WARMUP      = 5
    # Max tolerated wall time per trial (seconds) — warn if exceeded
    TRIAL_TIMEOUT = (BENCH_DUR + WARMUP + 15)

    results = {}
    for scenario in SCENARIOS:
        results[scenario] = {}
        for n_nodes in NODE_COUNTS:
            results[scenario][n_nodes] = []
            for trial in range(N_TRIALS):
                log(f'    {scenario} | {n_nodes} nodes | trial {trial+1}/{N_TRIALS}')
                kill_all(slice_)

                total = BENCH_DUR + WARMUP + 8
                t_start = time.time()

                launch_engine(engine, total)
                time.sleep(2)
                launch_feed(feed, scenario, total)
                time.sleep(2)

                for i in range(n_nodes):
                    z_buy  = -2.0 if i % 2 == 0 else -99.0
                    z_sell = 99.0 if i % 2 == 0 else 2.0
                    launch_strat(slice_.get_node(f'strat-{i}'), f'strat-{i:03d}',
                                 z_buy, z_sell, total - 2)

                wait_secs = BENCH_DUR + WARMUP + 10
                log(f'    Waiting {wait_secs}s for trial...')
                time.sleep(wait_secs)

                elapsed = time.time() - t_start
                if elapsed > TRIAL_TIMEOUT:
                    log(f'    WARNING: trial took {elapsed:.0f}s (expected <{TRIAL_TIMEOUT}s) — possible code issue!')

                # Collect metrics from file written by ME before ZMQ shutdown
                metrics_raw, _ = engine.execute(
                    'cat /tmp/me_metrics.json 2>/dev/null || echo ""',
                    quiet=True
                )
                metrics_raw = metrics_raw.strip()
                try:
                    import json as _j
                    m = _j.loads(metrics_raw)
                    metrics = (
                        f"orders={m['orders']} fills={m['fills']} "
                        f"throughput={m['throughput']} orders/sec "
                        f"latency p50={m['p50_us']}us p95={m['p95_us']}us "
                        f"p99={m['p99_us']}us p999={m['p999_us']}us"
                    )
                except Exception:
                    metrics = ""
                    eng_log, _ = engine.execute('cat /tmp/engine.log 2>/dev/null', quiet=True)
                    strat_log, _ = slice_.get_node('strat-0').execute('cat /tmp/strat.log 2>/dev/null', quiet=True)
                    log(f'    WARNING: no metrics')
                    log(f'    [engine.log]: {eng_log.strip()[-400:]}')
                    log(f'    [strat.log]: {strat_log.strip()[-400:]}')

                results[scenario][n_nodes].append({
                    'scenario': scenario, 'n_nodes': n_nodes, 'trial': trial + 1,
                    'elapsed_s': round(elapsed, 1),
                    'metrics': metrics,
                    'metrics_raw': _j.loads(metrics_raw) if metrics else {},
                })
                log(f'    Trial done ({elapsed:.0f}s). {metrics or "Metrics: (none)"}')
                kill_all(slice_)

    with open('benchmark_results.json', 'w') as f:
        json.dump(results, f, indent=2)
    log('    Saved benchmark_results.json')

    # ── Step 10: Fault injection ──────────────────────────────────────────────
    log('[10] Fault injection (8 nodes, kill strat-7 at t=30s)...')
    FAULT_DUR = 60
    KILL_AT   = 30
    kill_all(slice_)

    launch_engine(engine, FAULT_DUR + 10)
    time.sleep(2)
    launch_feed(feed, 'gbm', FAULT_DUR + 10)
    time.sleep(2)

    strat_pids = {}
    for i in range(8):
        z_buy  = -2.0 if i % 2 == 0 else -99.0
        z_sell = 99.0 if i % 2 == 0 else 2.0
        sn = slice_.get_node(f'strat-{i}')
        launch_strat(sn, f'strat-{i:03d}', z_buy, z_sell, FAULT_DUR + 5)
        # Capture the PID right after launch for accurate alive check
        pid_out, _ = sn.execute('pgrep -n -f run_strat.py 2>/dev/null || echo ""', quiet=True)
        strat_pids[i] = pid_out.strip()
        log(f'    strat-{i} started (pid={strat_pids[i]})')

    log(f'    Waiting {KILL_AT}s before kill...')
    time.sleep(KILL_AT)
    slice_.get_node('strat-7').execute('pkill -9 -f run_strat.py 2>/dev/null; true', quiet=True)
    log('    [!] strat-7 KILLED')

    time.sleep(FAULT_DUR - KILL_AT + 3)

    log('\n    === FAULT INJECTION RESULTS ===')
    fault_results = {}
    all_pass = True
    for i in range(8):
        sn = slice_.get_node(f'strat-{i}')
        pid = strat_pids.get(i, '')
        if pid:
            alive_out, _ = sn.execute(f'kill -0 {pid} 2>/dev/null && echo alive || echo dead', quiet=True)
            alive = 'alive' in alive_out
        else:
            cnt, _ = sn.execute('pgrep -f run_strat.py | wc -l', quiet=True)
            alive = int(cnt.strip()) > 0
        expected = (i != 7)
        ok       = alive == expected
        all_pass = all_pass and ok
        log(f'    strat-{i}: {"ALIVE" if alive else "DEAD"} (expected {"ALIVE" if expected else "DEAD"}) [{"PASS" if ok else "FAIL"}]')
        fault_results[f'strat-{i}'] = {'alive': alive, 'expected': expected, 'pass': ok}

    log(f'    Fault injection: {"ALL PASS" if all_pass else "SOME FAILURES — see above"}')
    with open('fault_injection_results.json', 'w') as f:
        json.dump(fault_results, f, indent=2)

    # ── Step 11: Download logs ─────────────────────────────────────────────────
    log('[11] Downloading logs...')
    os.makedirs('results', exist_ok=True)
    for remote, local, src in [
        ('/tmp/engine.log', 'results/engine_bench.log', engine),
        ('/tmp/feed.log',   'results/feed_bench.log',   feed),
    ]:
        try:
            src.download_file(remote, local)
            log(f'    {local}')
        except Exception as e:
            log(f'    {local}: {e}')

    kill_all(slice_)

    log('[11] Deleting slice...')
    slice_.delete()
    log('    Slice deleted.')

    log('\n=== ALL DONE ===')
    log('  benchmark_results.json')
    log('  fault_injection_results.json')
    log('  results/  (logs)')

except KeyboardInterrupt:
    log('\n!!! Interrupted by user !!!')
    log('Cleaning up...')
    if slice_:
        try:
            kill_all(slice_)
            slice_.delete()
            log('Slice deleted.')
        except Exception as e:
            log(f'Cleanup error: {e}')
    log('Done. Check run_fabric.log for full history.')
    sys.exit(1)

except Exception as e:
    log(f'\n!!! ERROR: {e} !!!')
    traceback.print_exc()
    log('Cleaning up...')
    if slice_:
        try:
            kill_all(slice_)
            slice_.delete()
            log('Slice deleted.')
        except Exception as cleanup_err:
            log(f'Cleanup error: {cleanup_err}')
    sys.exit(1)
