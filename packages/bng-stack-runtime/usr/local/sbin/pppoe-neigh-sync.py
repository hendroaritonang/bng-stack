#!/usr/bin/env python3
import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

STATE_FILE = Path('/run/pppoe-neigh-sync/state.json')
LOCK_FILE = Path('/run/pppoe-neigh-sync/lock')
INSTANCE_STATE_DIR = Path('/run/accel-ppp-vpp')
INSTANCE_CONFIG_GLOB = '/etc/accel-ppp-*.conf'


def log(msg):
    print(f'pppoe-neigh-sync: {msg}', flush=True)


def run(cmd, timeout=5):
    return subprocess.run(cmd, text=True, capture_output=True, check=False, timeout=timeout)


def ensure_runtime_dir():
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except FileNotFoundError:
        return {}
    except Exception as err:
        log(f'failed to load state file: {err}')
        return {}


def save_state(state):
    ensure_runtime_dir()
    tmp = STATE_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(state, sort_keys=True))
    tmp.replace(STATE_FILE)


def get_proto201_routes():
    try:
        res = run(['ip', '-4', '-j', 'route', 'show', 'table', 'main', 'proto', '201'], timeout=4)
    except subprocess.TimeoutExpired:
        raise RuntimeError('timeout reading proto 201 routes')

    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or res.stdout.strip() or f'ip route rc={res.returncode}')

    try:
        payload = json.loads(res.stdout or '[]')
    except json.JSONDecodeError as err:
        raise RuntimeError(f'invalid route json: {err}') from err

    routes = {}
    for item in payload:
        dst = str(item.get('dst', '')).split('/')[0]
        dev = item.get('dev', '')
        if dst and dev and dev.startswith('vlan'):
            routes[dst] = dev
    return routes


def list_instance_configs():
    return sorted(Path('/etc').glob('accel-ppp-*.conf'))


def get_instance_name(cfg_path: Path):
    name = cfg_path.name
    if name.startswith('accel-ppp-') and name.endswith('.conf'):
        return name[len('accel-ppp-'):-len('.conf')]
    return None


def get_instance_cli_port(cfg_path: Path):
    for raw_line in cfg_path.read_text().splitlines():
        line = raw_line.strip()
        if line.startswith('tcp=127.0.0.1:'):
            return line.split(':', 1)[1].strip()
    return None


def get_instance_gateway(instance):
    cfg = Path(f'/etc/accel-ppp-{instance}.conf')
    if not cfg.exists():
        return None

    for raw_line in cfg.read_text().splitlines():
        line = raw_line.strip()
        if line.startswith('gw-ip-address='):
            return line.split('=', 1)[1].strip()
    return None


def get_gateway_loop_id(gw_ip):
    parts = (gw_ip or '').split('.')
    if len(parts) != 4:
        return None
    return parts[2]


def get_active_sessions():
    # Clear zombie cache each cycle so reconnected subscribers are re-probed.
    _zombie_interfaces.clear()

    sessions = {}
    mac_re = re.compile(r'^(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$')

    for cfg in list_instance_configs():
        instance = get_instance_name(cfg)
        port = get_instance_cli_port(cfg)
        gw_ip = get_instance_gateway(instance)
        if not instance or not port:
            continue

        try:
            res = run(['accel-cmd', '-p', port, 'show', 'sessions'], timeout=4)
        except subprocess.TimeoutExpired:
            log(f'timeout reading accel sessions for {instance}')
            continue

        if res.returncode != 0 or 'calling-sid' not in res.stdout:
            continue

        for line in res.stdout.splitlines():
            if '|' not in line:
                continue
            parts = [p.strip() for p in line.split('|')]
            if len(parts) < 8:
                continue

            ifname = parts[0]
            username = parts[1]
            mac = parts[2].lower()
            ip = parts[3].split('/')[0].strip()
            state = parts[7].lower()

            if state != 'active' or not ip or not mac_re.match(mac):
                continue

            session_name = ifname or username
            sessions[ip] = {
                'mac': mac,
                'instance': instance,
                'session_name': session_name,
                'gw_ip': gw_ip,
            }

    return sessions


_loopbacks_ready = set()

# Track interfaces that returned 'unknown input' from VPP — i.e. they no
# longer exist in VPP's interface table.  We stop calling vppctl for them
# so we don't spam VPP logs every 2 s.  The set is cleared whenever a fresh
# active session list is built, so a legitimately reconnected subscriber
# (same username, new session) will be probed again.
_zombie_interfaces = set()


def vpp_loopback_exists(loop_if):
    """Check if a VPP loopback interface already exists and is up."""
    try:
        res = run(['/usr/bin/vppctl', 'show interface', loop_if], timeout=4)
        if res.returncode == 0 and loop_if in (res.stdout or ''):
            return 'up' in (res.stdout or '').lower()
    except Exception:
        pass
    return False


def vpp_interface_is_up(ifname):
    """Check if a VPP interface exists and is admin-up.

    Returns:
        True  — interface exists and is admin-up
        False — interface exists but is admin-down, or vppctl error
        None  — interface is unknown to VPP ('unknown input' response)
               caller should treat this as a zombie and stop polling
    """
    if not ifname:
        return False
    try:
        res = run(['/usr/bin/vppctl', 'show interface', ifname], timeout=4)
        stdout = res.stdout or ''
        if 'unknown input' in stdout or 'unknown input' in (res.stderr or ''):
            # VPP does not know this interface at all — it was deleted
            return None
        if res.returncode == 0 and ifname in stdout:
            for line in stdout.splitlines():
                parts = line.split()
                if len(parts) >= 3 and parts[0] == ifname:
                    return parts[2].lower() == 'up'
        return False
    except Exception:
        return False


def ensure_vpp_source(instance, session_name, gw_ip):
    if not session_name or not gw_ip:
        return

    # Skip interfaces already confirmed as zombie (not in VPP table).
    # This prevents spamming VPP CLI with 'show interface <username>' every
    # 2 s when the PPPoE session has already been torn down in VPP but
    # accel-ppp still reports it as active briefly during teardown.
    if session_name in _zombie_interfaces:
        return

    loop_id = get_gateway_loop_id(gw_ip)
    if not loop_id:
        return
    loop_if = f'loop{loop_id}'

    # Only create loopback once per daemon lifetime (or if not yet tracked)
    if loop_if not in _loopbacks_ready:
        if not vpp_loopback_exists(loop_if):
            run(['/usr/bin/vppctl', f'create loopback interface instance {loop_id}'])
            run(['/usr/bin/vppctl', f'set interface state {loop_if} up'])
            run(['/usr/bin/vppctl', f'set interface ip address {loop_if} {gw_ip}/32'])
            log(f'created loopback {loop_if} with {gw_ip}/32')
        _loopbacks_ready.add(loop_if)

    # Only set unnumbered if the session interface actually exists and is UP in VPP.
    # Orphan/DOWN interfaces from desync would spam VPP logs with parse errors.
    status = vpp_interface_is_up(session_name)
    if status is None:
        # Interface unknown to VPP — mark as zombie, stop polling
        _zombie_interfaces.add(session_name)
        log(f'interface {session_name!r} not found in VPP — marking as zombie, skipping')
        return
    if not status:
        return
    run(['/usr/bin/vppctl', f'set interface unnumbered {session_name} use {loop_if}'])


def neigh_replace(ip, dev, mac):
    try:
        res = run(['ip', 'neigh', 'replace', ip, 'lladdr', mac, 'nud', 'permanent', 'dev', dev], timeout=4)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f'timeout replacing neighbor {ip} dev {dev}')

    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or res.stdout.strip() or f'ip neigh replace rc={res.returncode}')


def neigh_del(ip, dev):
    try:
        res = run(['ip', 'neigh', 'del', ip, 'dev', dev], timeout=4)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f'timeout deleting neighbor {ip} dev {dev}')

    if res.returncode != 0 and 'No such file or directory' not in (res.stderr or ''):
        raise RuntimeError(res.stderr.strip() or res.stdout.strip() or f'ip neigh del rc={res.returncode}')


def build_desired_state():
    sessions = get_active_sessions()
    routes = get_proto201_routes()
    desired = {}
    for ip, info in sessions.items():
        dev = routes.get(ip)
        if dev:
            desired[f'{dev}|{ip}'] = info['mac']

        ensure_vpp_source(info.get('instance'), info.get('session_name'), info.get('gw_ip'))
    return desired


def sync_once():
    previous = load_state()
    desired = build_desired_state()

    for key, mac in desired.items():
        dev, ip = key.split('|', 1)
        if previous.get(key) != mac:
            neigh_replace(ip, dev, mac)
            log(f'set neighbor {ip} dev {dev} mac {mac}')

    for key in previous.keys() - desired.keys():
        dev, ip = key.split('|', 1)
        neigh_del(ip, dev)
        log(f'deleted neighbor {ip} dev {dev}')

    if desired != previous:
        save_state(desired)


def take_lock():
    ensure_runtime_dir()
    LOCK_FILE.touch(exist_ok=True)
    lock_handle = LOCK_FILE.open('r+')
    try:
        import fcntl
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError('another pppoe-neigh-sync instance is already running')
    return lock_handle


def main():
    parser = argparse.ArgumentParser(description='Sync accel/VPP PPPoE sessions to Linux permanent neighbor entries')
    parser.add_argument('--daemon', action='store_true', help='Run continuously')
    parser.add_argument('--interval', type=float, default=2.0, help='Sync interval in seconds (daemon mode)')
    args = parser.parse_args()

    try:
        lock_handle = take_lock()
    except Exception as err:
        log(str(err))
        return 1

    try:
        if not args.daemon:
            sync_once()
            return 0

        while True:
            try:
                sync_once()
            except Exception as err:
                log(f'sync failed: {err}')
            time.sleep(max(args.interval, 0.5))
    finally:
        lock_handle.close()


if __name__ == '__main__':
    sys.exit(main())
