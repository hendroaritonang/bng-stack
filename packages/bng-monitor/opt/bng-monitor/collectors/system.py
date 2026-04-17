"""System metrics collector — CPU, memory, disk, VPP process stats."""
import asyncio
import time
import logging
import psutil

log = logging.getLogger("bng.collector.system")

_cache = {
    "cpu_percent": 0.0,
    "cpu_count": 0,
    "mem_total_mb": 0.0,
    "mem_used_mb": 0.0,
    "mem_percent": 0.0,
    "disk_total_gb": 0.0,
    "disk_used_gb": 0.0,
    "disk_percent": 0.0,
    "load_avg": [0.0, 0.0, 0.0],
    "uptime_seconds": 0,
    "vpp_rss_mb": 0.0,
    "vpp_cpu_percent": 0.0,
    "last_update": 0,
}


async def collect_all():
    """Collect system metrics."""
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, _collect_sync)
    _cache.update(data)
    _cache["last_update"] = time.time()
    return _cache


def _collect_sync() -> dict:
    data = {}

    # CPU
    data["cpu_percent"] = psutil.cpu_percent(interval=0.5)
    data["cpu_count"] = psutil.cpu_count()

    # Memory
    mem = psutil.virtual_memory()
    data["mem_total_mb"] = round(mem.total / (1024 * 1024), 1)
    data["mem_used_mb"] = round(mem.used / (1024 * 1024), 1)
    data["mem_percent"] = mem.percent

    # Disk
    disk = psutil.disk_usage("/")
    data["disk_total_gb"] = round(disk.total / (1024**3), 1)
    data["disk_used_gb"] = round(disk.used / (1024**3), 1)
    data["disk_percent"] = disk.percent

    # Load average
    data["load_avg"] = list(psutil.getloadavg())

    # Uptime
    data["uptime_seconds"] = int(time.time() - psutil.boot_time())

    # VPP process stats
    data["vpp_rss_mb"] = 0.0
    data["vpp_cpu_percent"] = 0.0
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if proc.info["name"] == "vpp_main":
                mem_info = proc.memory_info()
                data["vpp_rss_mb"] = round(mem_info.rss / (1024 * 1024), 1)
                data["vpp_cpu_percent"] = proc.cpu_percent(interval=0.1)
                break
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    return data


def get_cache() -> dict:
    return _cache.copy()
