"""Job spec: one TOML file per job. Parse, validate, render templated inputs."""
import json
import re
import shlex
import subprocess
import tomllib
from pathlib import Path

ID_RE = re.compile(r"^[A-Za-z0-9_.:\-]+$")
TEMPLATE = re.compile(r"\{\{\s*(.*?)\s*\}\}")
# jobs.<id>.result[.key...] | jobs.<id>.verdict | findings.<id>.text|status | inputs.<key>
REF = re.compile(r"^(?:(jobs)\.(.+?)\.(result|verdict)|(findings)\.(.+?)\.(text|status)|(inputs))((?:\.[A-Za-z0-9_\-]+)*)$")
VERDICTS = ("pass", "fail", "invalid", "unknown")
FINDING_LISTS = ("produces", "produces_if_pass", "produces_if_fail", "refutes_if_pass", "refutes_if_fail")
DEFAULTS = {"value": 1, "depends_on": [], "inputs": {}, "priority": 0, "run": "",
            "fail_on": ["feedback_report"], **{k: [] for k in FINDING_LISTS}}


class SpecError(ValueError):
    pass


def load(path) -> dict:
    return normalize(tomllib.loads(Path(path).read_text()))


def loads(text: str) -> dict:
    return normalize(tomllib.loads(text))


def normalize(raw: dict) -> dict:
    spec = {**DEFAULTS, **raw}
    for k in ("depends_on", "fail_on", *FINDING_LISTS):
        spec[k] = list(spec[k])
    return spec


def dep_id(dep: str) -> tuple[str, str | None]:
    """`review3@pass` -> ("review3", "pass"): the dependant runs only on that branch."""
    base, _, want = dep.partition("@")
    return base, want or None


def refs(value):
    """Every template expression inside a (nested) value."""
    if isinstance(value, str):
        yield from TEMPLATE.findall(value)
    elif isinstance(value, list):
        for v in value:
            yield from refs(v)
    elif isinstance(value, dict):
        for v in value.values():
            yield from refs(v)


def scenarios(root, cfg: dict) -> list[str] | None:
    """The scenario names a job may run. `[bench] scenario_cmd`, when set, is a shell command run in the state root whose
    last stdout line is a JSON list of names (earlier lines, e.g. warnings, are ignored). Otherwise the entries of
    `[bench] scenario_dir` (relative to the state root, default `scenarios/`), one file or directory per scenario, named
    by its stem. None = no registry: accept anything."""
    b = cfg.get("bench", {})
    if b.get("scenario_cmd"):
        p = subprocess.run(["sh", "-c", b["scenario_cmd"]], cwd=root, capture_output=True, text=True, timeout=60)
        lines = p.stdout.strip().splitlines()
        if p.returncode or not lines:
            raise SystemExit(f"[bench] scenario_cmd exited {p.returncode} with no list: {p.stderr.strip()[-200:]}")
        return sorted(json.loads(lines[-1]))
    d = Path(root) / Path(b.get("scenario_dir", "scenarios")).expanduser()
    if not d.is_dir():
        return None
    return sorted({p.stem for p in d.iterdir() if not p.name.startswith(".")})


def synth(spec: dict, cfg: dict) -> tuple[str, str | None] | None:
    """(run, preflight) the harness supplies for a spec with `scenario` and no `run`, from its lane's `runner` /
    `preflight` / `model` in lanes.toml; None if the spec has its own run (or no scenario, or its lane has no runner)."""
    l = cfg["lanes"].get(spec.get("lane"), {})
    if spec.get("run") or not spec.get("scenario") or "runner" not in l:
        return None
    f = dict(scenario=shlex.quote(spec["scenario"]), model=shlex.quote(l.get("model", "")), funded_s=funded_seconds(spec, cfg["lanes"]))
    return l["runner"].format(**f), (l["preflight"].format(**f) if l.get("preflight") else None)


def funded_seconds(spec: dict, lanes: dict) -> int:
    """The kill line: the seconds budget_usd buys on the spec's lane (budget_usd x 3600 / usd_per_h), floor 30 s,
    cap an hour; `any` (or a lane with no usd_per_h) gets the hour. `lanes` is lanes.load()['lanes']."""
    rate = lanes.get(spec.get("lane"), {}).get("usd_per_h")
    return max(30, min(3600, int(spec.get("budget_usd", 0) * 3600 / rate))) if rate else 3600


def validate(spec: dict, lanes: dict, known: list[str] | None = None, drivers: list[str] | None = None) -> list[str]:
    """Reasons to refuse the spec; empty list = accepted. `lanes` is lanes.load()['lanes']; `known` = scenarios() (None: any);
    `drivers` = [bench] drivers (None: a run is not checked for a verdict line)."""
    errs = []
    for k in ("id", "question", "expect", "if_pass", "if_fail"):
        if not str(spec.get(k, "")).strip():
            errs.append(f"missing {k}")
    if spec.get("id") and not ID_RE.match(str(spec["id"])):
        errs.append(f"bad id {spec['id']!r} (letters, digits, _ . : -)")
    if spec.get("expect") and spec["expect"] not in ("pass", "fail"):
        errs.append(f"expect must be pass or fail, not {spec['expect']!r}")
    if spec.get("if_pass") and str(spec["if_pass"]).strip() == str(spec.get("if_fail", "")).strip():
        errs.append("if_pass == if_fail: the run cannot change a decision")
    lane = spec.get("lane")
    if lane != "any" and lane not in lanes:
        errs.append(f"unknown lane {lane!r} (have: {', '.join([*lanes, 'any'])})")
    if "budget_s" in spec:
        errs.append("no budget_s: fund the run in dollars (budget_usd); the time it buys is budget_usd / the lane's usd_per_h")
    budget_usd, value = spec.get("budget_usd"), spec.get("value", 1)
    rate = lanes.get(lane, {}).get("usd_per_h")
    if isinstance(budget_usd, (int, float)) and rate:
        raw = budget_usd * 3600 / rate
        if raw < 30:
            errs.append(f"funding buys < 30 s on {lane} (${budget_usd} at ${rate}/h)")
        elif raw > 3600:
            errs.append(f"no run longer than an hour: ${budget_usd} buys {raw:.0f} s on {lane} (at most ${rate}; split it)")
    if not isinstance(budget_usd, (int, float)) or budget_usd < 0:
        errs.append("budget_usd must be a number >= 0")
    if not isinstance(value, (int, float)) or value <= 0:
        errs.append("value must be a positive number (questions it settles)")
    elif isinstance(budget_usd, (int, float)):
        # cheap lanes carry a $/value ceiling; `any` could land on any of them, so the strictest applies
        caps = [l["max_usd_per_value"] for n, l in lanes.items()
                if "max_usd_per_value" in l and lane in (n, "any")]
        if caps and budget_usd > value * min(caps):
            errs.append(f"budget_usd {budget_usd} > value {value} x ${min(caps)}/value on a cheap lane")
    if not isinstance(spec.get("run", ""), str):
        errs.append("run must be a shell command string")
    elif spec.get("run") and drivers is not None and "verdict=" not in spec["run"] \
            and not any(spec["run"].lstrip().startswith(d) for d in drivers):
        errs.append("run never prints its verdict: end it with `echo \"pit: verdict=pass|fail result={...}\"` or start it "
                    "with a [bench] drivers command (an exit code alone books INVALID); a question answered by reading is a finding")
    elif spec.get("run") and lane in lanes:
        # a hand-written run may not borrow another lane's model: that work belongs on that lane
        named = set(re.findall(r"--model[ =]['\"]?([^\s'\"]+)", spec["run"]))
        other = {l["model"] for n, l in lanes.items() if n != lane and l.get("model")} - {lanes[lane].get("model")}
        if named & other:
            errs.append(f"run names model {', '.join(sorted(named & other))}, which is another lane's: post it there, "
                        f"or use `scenario` and let this lane's runner build the run")
    if spec.get("scenario") and not spec.get("run"):
        if "runner" not in lanes.get(lane, {}):
            errs.append(f"scenario needs a lane with a runner (lanes.toml), not {lane!r}: give `run` for desk work or a custom driver")
        if spec.get("arms"):
            errs.append("scenario runs one variant: drop `arms` (or post one job per arm)")
        if known is not None and spec["scenario"] not in known:
            errs.append(f"unknown scenario {spec['scenario']!r} (have: {', '.join(known)})")
    deps = {dep_id(d)[0] for d in spec.get("depends_on", [])}
    for d in spec.get("depends_on", []):
        if dep_id(d)[1] not in (None, "pass", "fail"):
            errs.append(f"depends_on {d!r}: only @pass or @fail")
    for k in FINDING_LISTS:
        if not all(isinstance(x, str) and ID_RE.match(x) for x in spec.get(k, [])):
            errs.append(f"{k} must be a list of ids")
    for expr in refs([spec.get("inputs", {}), spec.get("run", "")]):
        m = REF.match(expr)
        if not m:
            errs.append(f"unresolved template {{{{ {expr} }}}}: not jobs.<id>.result|verdict, "
                        f"findings.<id>.text|status or inputs.<key>")
        elif (m[2] or m[5]) and (m[2] or m[5]) not in deps:
            errs.append(f"unresolved template {{{{ {expr} }}}}: {m[2] or m[5]} is not in depends_on")
        elif m[7] and m[8].lstrip(".").split(".")[0] not in spec.get("inputs", {}):
            errs.append(f"unresolved template {{{{ {expr} }}}}: no such input")
    return errs


def _lookup(expr: str, ctx: dict):
    m = REF.match(expr)
    if not m:
        raise SpecError(f"bad template {expr!r}")
    if m[1]:
        job = ctx["jobs"].get(m[2])
        if not job or job.get("verdict") is None:
            raise SpecError(f"unresolved {{{{ {expr} }}}}: {m[2]} has no result yet")
        cur = job["result"] if m[3] == "result" else job["verdict"]
    elif m[4]:
        f = ctx["findings"].get(m[5])
        if not f:
            raise SpecError(f"unresolved {{{{ {expr} }}}}: no finding {m[5]}")
        cur = f[m[6]]
    else:
        cur = ctx["inputs"]
    for key in [k for k in m[8].split(".") if k]:
        if not isinstance(cur, dict) or key not in cur:
            raise SpecError(f"unresolved {{{{ {expr} }}}}: no key {key!r}")
        cur = cur[key]
    return cur


def fill(value, ctx: dict, quote: bool = False):
    """A string that is exactly one template takes the upstream value as-is (lists stay lists);
    templates inside a longer string are interpolated as text (shell-quoted when `quote`)."""
    if isinstance(value, str):
        whole = TEMPLATE.fullmatch(value.strip())
        if whole and not quote:
            return _lookup(whole[1], ctx)
        return TEMPLATE.sub(lambda m: _str(_lookup(m[1], ctx), quote), value)
    if isinstance(value, list):
        return [fill(v, ctx, quote) for v in value]
    if isinstance(value, dict):
        return {k: fill(v, ctx, quote) for k, v in value.items()}
    return value


def _str(v, quote: bool = False) -> str:
    items = v if isinstance(v, list) else [v]
    return " ".join(shlex.quote(str(x)) if quote else str(x) for x in items)


def render(spec: dict, ctx: dict) -> tuple[dict, str]:
    """(inputs, run) with upstream results filled in. ctx = {jobs: {id: {verdict, result}}, findings: {...}}."""
    inputs = fill(spec.get("inputs", {}), ctx)
    # upstream values are data, not shell: quote them into the command
    return inputs, fill(spec.get("run", ""), {**ctx, "inputs": inputs}, quote=True)
