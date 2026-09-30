"""lanes.toml (the resource table) and the one cost-line function everything shares."""
import subprocess
import tomllib
from pathlib import Path


def load(root) -> dict:
    return tomllib.loads((Path(root) / "lanes.toml").read_text())


def rate(cfg: dict, lane: str) -> float:
    """$/h of a lane; `any` or an unknown lane is charged its meters only (desk time is the session's)."""
    return float(cfg["lanes"].get(lane, {}).get("usd_per_h", 0))


def price(prices: dict, meter: str) -> float | None:
    """$ per unit of a meter: `usd_per_<meter>`, or for a `tok_<x>` meter `usd_per_mtok_<x>` / 1e6; None = unpriced."""
    if f"usd_per_{meter}" in prices:
        return float(prices[f"usd_per_{meter}"])
    if meter.startswith("tok_") and f"usd_per_mtok_{meter[4:]}" in prices:
        return float(prices[f"usd_per_mtok_{meter[4:]}"]) / 1e6
    return None


def cost_line(cfg: dict, lane: str, wall_s: float = 0.0, meters: dict | None = None) -> dict:
    """A run's cost: wall x the lane's usd_per_h + the sum of meter x price. Prices are the lane's `[lanes.<name>.prices]`
    over the top-level `[prices]` defaults (which also price lane `any`). An unpriced meter is recorded, not charged."""
    prices = {**cfg.get("prices", {}), **cfg["lanes"].get(lane, {}).get("prices", {})}
    meters = {k: v for k, v in (meters or {}).items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
    usd_time = float(wall_s) / 3600 * rate(cfg, lane)
    charged = {m: n * p for m, n in meters.items() if (p := price(prices, m)) is not None}
    unpriced = sorted(set(meters) - set(charged))
    return {"wall_s": round(float(wall_s), 1), "meters": meters, "usd_time": round(usd_time, 4),
            "usd": round(usd_time + sum(charged.values()), 4), "lane": lane, **({"unpriced": unpriced} if unpriced else {})}


def busy(cfg: dict, lane: str, on: dict[str, str]) -> str | None:
    """Why `lane` can take no job now, from `on` (job -> lane of every run in flight): its slots are full, or another lane
    of its device has a run. None when it is free. `any` is never busy here."""
    if lane == "any":
        return None
    l = cfg["lanes"].get(lane, {})
    mine = sorted(j for j, x in on.items() if x == lane)
    if len(mine) >= l.get("slots", 1):
        return f"busy ({', '.join(mine)})"
    dev = l.get("device")
    held = dev and next(((j, x) for j, x in sorted(on.items()) if x != lane and cfg["lanes"].get(x, {}).get("device") == dev), None)
    return f"device {dev} busy ({held[0]} running on {held[1]})" if held else None


def gate_open(cfg: dict, lane: str) -> tuple[bool, str]:
    """Run the lane's gate command: exit 0 = free. `any` has no gate."""
    cmd = cfg["lanes"].get(lane, {}).get("gate", "true")
    try:
        r = subprocess.run(["sh", "-c", cmd], capture_output=True, timeout=10)
    except subprocess.TimeoutExpired:
        return False, "gate timed out"
    return r.returncode == 0, "free" if r.returncode == 0 else f"gate closed (exit {r.returncode})"
