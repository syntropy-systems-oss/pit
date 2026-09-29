"""lanes.toml and the one cost-line function everything shares."""
import subprocess
import tomllib
from pathlib import Path


def load(root) -> dict:
    return tomllib.loads((Path(root) / "lanes.toml").read_text())


def rate(cfg: dict, lane: str) -> float:
    """$/h of a lane; `any` or an unknown lane is charged tokens only (desk time is the session's)."""
    return float(cfg["lanes"].get(lane, {}).get("usd_per_h", 0))


def cost_line(cfg: dict, lane: str, uncached_in: int = 0, cache_read: int = 0, out: int = 0,
              wall_s: float = 0.0) -> dict:
    t = cfg["tokens"]
    usd = (uncached_in * t["uncached_per_m"] + cache_read * t["cache_read_per_m"] + out * t["out_per_m"]) / 1e6 \
        + wall_s / 3600 * rate(cfg, lane)
    return {"uncached_in": int(uncached_in), "cache_read": int(cache_read), "out": int(out),
            "wall_s": round(float(wall_s), 1), "usd": round(usd, 4), "lane": lane}


def gate_open(cfg: dict, lane: str) -> tuple[bool, str]:
    """Run the lane's gate command: exit 0 = free. `any` has no gate."""
    cmd = cfg["lanes"].get(lane, {}).get("gate", "true")
    try:
        r = subprocess.run(["sh", "-c", cmd], capture_output=True, timeout=10)
    except subprocess.TimeoutExpired:
        return False, "gate timed out"
    return r.returncode == 0, "free" if r.returncode == 0 else f"gate closed (exit {r.returncode})"
