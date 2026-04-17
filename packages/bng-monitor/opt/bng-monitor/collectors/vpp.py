"""VPP data collector — talks to vppctl to gather session, interface, and policer data."""
import asyncio
import re
import time
import logging

from config import VPPCTL

log = logging.getLogger("bng.collector.vpp")

# Cached state
_cache = {
    "vpp_running": False,
    "vpp_pid": None,
    "vpp_version": "",
    "vpp_uptime": "",
    "sessions": [],          # list of dicts
    "interfaces": {},        # name -> {state, sw_if, rx_bytes, tx_bytes, ...}
    "policers": [],          # list of dicts
    "pppoe_summary": {},     # {total: N}
    "last_update": 0,
}


async def _run(cmd: str, timeout: float = 10.0) -> tuple[int, str]:
    """Run a shell command, return (returncode, stdout)."""
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, stdout.decode(errors="replace")
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return -1, ""
    except Exception as e:
        return -1, str(e)


async def check_vpp_running() -> bool:
    """Check if VPP is running."""
    rc, out = await _run("pgrep -x vpp_main")
    if rc == 0 and out.strip():
        _cache["vpp_pid"] = int(out.strip().split()[0])
        _cache["vpp_running"] = True
        return True
    _cache["vpp_running"] = False
    _cache["vpp_pid"] = None
    return False


async def collect_vpp_version():
    rc, out = await _run(f"{VPPCTL} show version")
    if rc == 0:
        _cache["vpp_version"] = out.strip().split("\n")[0] if out.strip() else ""


async def collect_vpp_uptime():
    rc, out = await _run(f"{VPPCTL} show clock")
    if rc == 0:
        _cache["vpp_uptime"] = out.strip()


async def collect_pppoe_sessions():
    """Parse 'vppctl show pppoe session' output.

    Actual format (multi-line per session):
      [0] sw-if-index 15 client-ip4 192.168.102.12 client-ip6 0.0.0.0/0 session-id 64 encap-if-index 9 decap-fib-index 0
          local-mac a2:08:78:df:22:02  client-mac 48:a9:8a:11:23:a1
    """
    rc, out = await _run(f"{VPPCTL} show pppoe session")
    if rc != 0:
        _cache["sessions"] = []
        _cache["pppoe_summary"] = {"total": 0}
        return

    sessions = []

    # Merge continuation lines (indented) with the previous [N] line
    merged = []
    for line in out.strip().split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("Number "):
            continue
        if re.match(r"\[\d+\]", stripped):
            merged.append(stripped)
        elif merged:
            # Continuation line — append to last entry
            merged[-1] += " " + stripped

    for block in merged:
        s = {}
        # Index
        idx_m = re.match(r"\[(\d+)\]", block)
        if idx_m:
            s["index"] = int(idx_m.group(1))

        # Fields — match actual VPP output field names
        for key, pattern in [
            ("client_ip", r"client-ip4?\s+([\d.]+)"),
            ("session_id", r"session-id\s+(\d+)"),
            ("encap_if_index", r"encap-if-index\s+(\d+)"),
            ("decap_fib_index", r"decap-fib-index\s+(\d+)"),
            ("sw_if_index", r"sw-if-index\s+(\d+)"),
            ("session_name", r"session-name\s+(\S+)"),
            ("client_mac", r"client-mac\s+([\da-fA-F:]+)"),
            ("local_mac", r"local-mac\s+([\da-fA-F:]+)"),
        ]:
            m = re.search(pattern, block)
            if m:
                s[key] = m.group(1)

        if s.get("client_ip") or s.get("session_id"):
            sessions.append(s)

    _cache["sessions"] = sessions
    _cache["pppoe_summary"] = {"total": len(sessions)}


async def collect_interface_stats():
    """Parse 'vppctl show interface' output.

    Actual format — each counter is on its own line:
      hendro                            16     up           0/0/0/0       rx packets                    46
                                                                          rx bytes                    7590
                                                                          tx packets                     0
                                                                          tx bytes                       0
    """
    rc, out = await _run(f"{VPPCTL} show interface", timeout=15)
    if rc != 0:
        return

    interfaces = {}
    current_name = None
    current = {}

    for line in out.split("\n"):
        # Interface name line: "hendro                16     up      0/0/0/0"
        # Skip header line
        if "Name" in line and "Idx" in line and "State" in line:
            continue

        name_m = re.match(r"^(\S+)\s+(\d+)\s+(up|down)", line, re.IGNORECASE)
        if name_m:
            if current_name:
                interfaces[current_name] = current
            current_name = name_m.group(1)
            current = {
                "name": current_name,
                "sw_if_index": int(name_m.group(2)),
                "state": name_m.group(3).lower(),
                "rx_bytes": 0, "tx_bytes": 0,
                "rx_packets": 0, "tx_packets": 0,
                "rx_errors": 0, "tx_errors": 0,
                "drops": 0,
            }
            # The name line itself may contain the first counter
            counter_m = re.search(r"(rx packets|rx bytes|tx packets|tx bytes|drops)\s+(\d+)", line)
            if counter_m:
                _apply_counter(current, counter_m.group(1), int(counter_m.group(2)))
            continue

        if current_name:
            # Counter lines (indented)
            counter_m = re.search(r"(rx packets|rx bytes|tx packets|tx bytes|drops|rx-error|tx-error)\s+(\d+)", line)
            if counter_m:
                _apply_counter(current, counter_m.group(1), int(counter_m.group(2)))

    if current_name:
        interfaces[current_name] = current

    _cache["interfaces"] = interfaces


def _apply_counter(iface: dict, counter_name: str, value: int):
    """Apply a parsed counter to an interface dict."""
    mapping = {
        "rx packets": "rx_packets",
        "rx bytes": "rx_bytes",
        "tx packets": "tx_packets",
        "tx bytes": "tx_bytes",
        "drops": "drops",
        "rx-error": "rx_errors",
        "tx-error": "tx_errors",
    }
    key = mapping.get(counter_name)
    if key:
        iface[key] = value


async def collect_policers():
    """Parse 'vppctl show policer' output.

    Actual VPP output format:
      Name "vyos_br2_15_30000_3000000_down" type 2r3c-2698 cir 24000 eir 30000 cb 3000000 eb 600000
      rate type kbps, round type closest
      conform action transmit, exceed action transmit, violate action drop

      Policer at index 0: dual rate, not color-aware
      cir 126334 tok/period, pir 157918 tok/period, scale 10
      cur lim 3072000000, cur bkt 3071875072, ext lim 614400000, ext bkt 614275072
      last update 5790548573
      conform 12 packets, 1464 bytes
      exceed 0 packets, 0 bytes
      violate 0 packets, 0 bytes
      -----------

    Policer name format: vyos_<br>_<sw_if_index>_<rate>_<burst>_<direction>
    """
    rc, out = await _run(f"{VPPCTL} show policer")
    if rc != 0:
        _cache["policers"] = []
        return

    policers = []
    current = None
    for line in out.split("\n"):
        line = line.strip()
        if not line or line.startswith("---"):
            continue

        # Name line: Name "vyos_br2_15_30000_3000000_down" type 2r3c-2698 cir 24000 eir 30000 cb 3000000 eb 600000
        name_m = re.match(r'^Name\s+"([^"]+)"\s+type\s+(\S+)\s+cir\s+(\d+)\s+eir\s+(\d+)\s+cb\s+(\d+)\s+eb\s+(\d+)', line)
        if name_m:
            if current:
                policers.append(current)
            pname = name_m.group(1)
            current = {
                "name": pname,
                "type": name_m.group(2),
                "cir": int(name_m.group(3)),
                "eir": int(name_m.group(4)),
                "cb": int(name_m.group(5)),
                "eb": int(name_m.group(6)),
                "conform_packets": 0, "conform_bytes": 0,
                "exceed_packets": 0, "exceed_bytes": 0,
                "violate_packets": 0, "violate_bytes": 0,
                "conform_action": "", "exceed_action": "", "violate_action": "",
                "rate_type": "",
            }
            # Parse policer name components: vyos_br2_15_30000_3000000_down
            parts_m = re.match(r'vyos_(\w+)_(\d+)_(\d+)_(\d+)_(up|down)', pname)
            if parts_m:
                current["br"] = parts_m.group(1)
                current["sw_if_index"] = int(parts_m.group(2))
                current["rate_kbps"] = int(parts_m.group(3))
                current["burst"] = int(parts_m.group(4))
                current["direction"] = parts_m.group(5)
            continue

        if not current:
            continue

        # rate type kbps, round type closest
        rt_m = re.match(r'rate type\s+(\w+)', line)
        if rt_m:
            current["rate_type"] = rt_m.group(1)

        # conform action transmit, exceed action transmit, violate action drop
        act_m = re.match(r'conform action\s+(\S+),\s*exceed action\s+(\S+),\s*violate action\s+(\S+)', line)
        if act_m:
            current["conform_action"] = act_m.group(1)
            current["exceed_action"] = act_m.group(2)
            current["violate_action"] = act_m.group(3)

        # conform 12 packets, 1464 bytes
        conform_m = re.match(r'conform\s+(\d+)\s+packets,\s+(\d+)\s+bytes', line)
        if conform_m:
            current["conform_packets"] = int(conform_m.group(1))
            current["conform_bytes"] = int(conform_m.group(2))

        # exceed 0 packets, 0 bytes
        exceed_m = re.match(r'exceed\s+(\d+)\s+packets,\s+(\d+)\s+bytes', line)
        if exceed_m:
            current["exceed_packets"] = int(exceed_m.group(1))
            current["exceed_bytes"] = int(exceed_m.group(2))

        # violate 0 packets, 0 bytes
        violate_m = re.match(r'violate\s+(\d+)\s+packets,\s+(\d+)\s+bytes', line)
        if violate_m:
            current["violate_packets"] = int(violate_m.group(1))
            current["violate_bytes"] = int(violate_m.group(2))

    if current:
        policers.append(current)

    _cache["policers"] = policers


def find_policers_for_interface(ifname: str) -> list[dict]:
    """Find all policers matching an interface name.

    Strategy:
    1. Look up sw_if_index from cached interfaces
    2. Find policers where name contains _<sw_if_index>_
    3. Fallback: try matching ifname directly in policer name
    """
    interfaces = _cache.get("interfaces", {})
    policers = _cache.get("policers", [])

    if not policers:
        return []

    # Get sw_if_index for the interface
    iface = interfaces.get(ifname)
    sw_if_index = iface.get("sw_if_index") if iface else None

    matching = []

    if sw_if_index is not None:
        # Primary: match by sw_if_index field parsed from policer name
        for p in policers:
            if p.get("sw_if_index") == sw_if_index:
                matching.append(p)

        # Fallback: regex match on raw name
        if not matching:
            pattern = f"_{sw_if_index}_"
            for p in policers:
                if pattern in p.get("name", ""):
                    matching.append(p)

    # Last resort: match ifname in policer name directly
    if not matching:
        for p in policers:
            if ifname in p.get("name", ""):
                matching.append(p)

    return matching


async def vpp_ping(source: str, dest_ip: str, count: int = 3) -> dict:
    """Execute a VPP ping and return results.

    VPP ping syntax: ping <addr> [source <intf>] [repeat <count>] [verbose]
    source must be an interface name (e.g. loop100), NOT an IP address.
    """
    cmd = f"{VPPCTL} ping {dest_ip} repeat {count} verbose"
    if source:
        cmd = f"{VPPCTL} ping {dest_ip} source {source} repeat {count} verbose"
    rc, out = await _run(cmd, timeout=30)
    return {
        "success": rc == 0 and "packet loss" in out,
        "output": out.strip(),
        "source": source,
        "destination": dest_ip,
    }


async def get_session_traffic(sw_if_index: int) -> dict:
    """Get traffic stats for a specific session interface."""
    rc, out = await _run(f"{VPPCTL} show interface {sw_if_index}")
    result = {"rx_bytes": 0, "tx_bytes": 0, "rx_packets": 0, "tx_packets": 0}
    if rc == 0:
        for line in out.split("\n"):
            rx_m = re.search(r"rx packets\s+(\d+).*?bytes\s+(\d+)", line)
            if rx_m:
                result["rx_packets"] = int(rx_m.group(1))
                result["rx_bytes"] = int(rx_m.group(2))
            tx_m = re.search(r"tx packets\s+(\d+).*?bytes\s+(\d+)", line)
            if tx_m:
                result["tx_packets"] = int(tx_m.group(1))
                result["tx_bytes"] = int(tx_m.group(2))
    return result


async def collect_all():
    """Run all VPP collections."""
    running = await check_vpp_running()
    if not running:
        _cache["sessions"] = []
        _cache["interfaces"] = {}
        _cache["policers"] = []
        _cache["pppoe_summary"] = {"total": 0}
        _cache["vpp_version"] = ""
        _cache["vpp_uptime"] = ""
        _cache["last_update"] = time.time()
        return _cache

    await asyncio.gather(
        collect_vpp_version(),
        collect_vpp_uptime(),
        collect_pppoe_sessions(),
        collect_interface_stats(),
        collect_policers(),
    )
    _cache["last_update"] = time.time()
    return _cache


def get_cache() -> dict:
    return _cache.copy()
