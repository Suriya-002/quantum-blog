"""The minus sign you can't see: the experiment behind the second "Quantum, Explained" post.

One-qubit experiments on a real IBM quantum computer, all on the same physical
qubit, 4,000 runs each. Heads = 0, tails = 1.

  1. Spin, then look            H, Z, measure            -> about 50/50: a look can't see the minus sign
  2. Spin between two tosses    H, Z, H, measure         -> tails almost every time
  3. The dial, 13 settings      H, P(angle), H, measure  -> heads slides from 100% to 0% and back,
                                                            following cos^2(angle / 2)

Z turns the arrow half a turn around the axis through the poles, which puts a
minus sign on the tails weight. P(angle) is the same kind of turn by any angle.
P(180 deg) is exactly Z, and P(0) does nothing, so the dial's first setting is
last week's "toss twice".

Barriers stop Qiskit from merging the gates before the job is sent. Without
them, the compiler would turn H, Z, H into a single X pulse. The script checks
that the hardware will really run both H gates, and that the compiled circuits
still give the right answer on paper, before it submits anything.

Run from this folder:
  python the_minus_sign.py                    dry run, submit to IBM, wait, save results
  python the_minus_sign.py --dry-run          every check, nothing sent to the quantum computer
  python the_minus_sign.py --backend NAME     use a named machine instead of the least busy one
  python the_minus_sign.py --qubit 22         prefer this qubit, if it beats the chip's median today
  python the_minus_sign.py --login            enter a new IBM Quantum API key
  python the_minus_sign.py --new              fresh hardware run; older results are archived

Running it again after a submit collects the same job instead of submitting a
second one, so the window can be closed while the job waits in IBM's queue.
"""

from __future__ import annotations

import argparse
import getpass
import json
import math
import os
import platform
import shutil
import statistics
import sys
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path

try:
    import numpy as np
    import qiskit
    import qiskit_ibm_runtime
    from qiskit import ClassicalRegister, QuantumCircuit, QuantumRegister
    from qiskit.primitives import BackendSamplerV2
    from qiskit.providers.basic_provider import BasicSimulator
    from qiskit.quantum_info import Operator
    from qiskit.transpiler import generate_preset_pass_manager
except ImportError as exc:  # pragma: no cover
    sys.exit(f"Missing package ({exc.name}). Run the PowerShell setup block first.")

SHOTS = 4000
SEED = 2026
JOB_TAGS = ["quantum-blog", "02-the-minus-sign"]
DIAL_DEGREES = list(range(0, 361, 30))

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
JOB_FILE = RESULTS / "job.json"
HW_JSON = RESULTS / "hardware_results.json"
HW_PNG = RESULTS / "the_minus_sign_hardware.png"
CIRCUITS_TXT = RESULTS / "circuits.txt"
DRY_DIR = RESULTS / "dryrun"


def _experiments() -> list[tuple[str, str, float, int | None]]:
    """(key, label in the console, chance of heads in theory, dial angle in degrees or None)."""
    rows: list[tuple[str, str, float, int | None]] = [
        ("spin_look", "Spin, then look", 0.5, None),
        ("spin_between", "Spin between two tosses", 0.0, None),
    ]
    for deg in DIAL_DEGREES:
        heads = math.cos(math.radians(deg) / 2) ** 2
        rows.append((f"dial_{deg:03d}", f"Dial at {deg} deg", round(heads, 12), deg))
    return rows


EXPERIMENTS = _experiments()
# Physical pulses (sx or x) each experiment must still have after transpiling.
# Fewer than 2 in a two-toss circuit would mean the H gates were merged.
MIN_PULSES = {key: (1 if key == "spin_look" else 2) for key, _, _, _ in EXPERIMENTS}

STATUS_TEXT = {
    "INITIALIZING": "IBM is preparing the job",
    "QUEUED": "waiting in IBM's queue",
    "RUNNING": "running on the quantum computer",
    "DONE": "done",
    "ERROR": "failed",
    "CANCELLED": "cancelled",
}


class CheckFailed(RuntimeError):
    """A check failed before or after submission; the message says what to do."""


def log(msg: str = "") -> None:
    print(msg, flush=True)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clock() -> str:
    return datetime.now().strftime("%H:%M")


def first_line(exc: BaseException) -> str:
    text = str(exc).strip()
    return text.splitlines()[0][:300] if text else type(exc).__name__


def rel(path: Path) -> str:
    try:
        return os.path.relpath(path, HERE)
    except ValueError:
        return str(path)


# ---------------------------------------------------------------- circuits

def _one_qubit(name: str) -> QuantumCircuit:
    return QuantumCircuit(QuantumRegister(1, "q"), ClassicalRegister(1, "final"), name=name)


def build_circuits() -> list[QuantumCircuit]:
    """The experiments, in the order of EXPERIMENTS."""
    spin_look = _one_qubit("spin_look")
    spin_look.h(0)
    spin_look.barrier()
    spin_look.z(0)                 # the minus sign
    spin_look.measure(0, 0)

    spin_between = _one_qubit("spin_between")
    spin_between.h(0)
    spin_between.barrier()         # without the barriers, H, Z, H becomes one X pulse
    spin_between.z(0)
    spin_between.barrier()
    spin_between.h(0)
    spin_between.measure(0, 0)

    circuits = [spin_look, spin_between]
    for deg in DIAL_DEGREES:
        dial = _one_qubit(f"dial_{deg:03d}")
        dial.h(0)
        dial.barrier()
        dial.p(math.radians(deg), 0)   # turn around the poles by `deg` degrees
        dial.barrier()
        dial.h(0)
        dial.measure(0, 0)
        circuits.append(dial)
    return circuits


# ---------------------------------------------------------------- statistics

def wilson_95(k: int, n: int) -> list[float]:
    """95% Wilson score interval for k successes out of n."""
    if n == 0:
        return [0.0, 1.0]
    z = 1.959963984540054
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return [max(0.0, centre - half), min(1.0, centre + half)]


def fit_dial(points: list[tuple[int, float]]) -> dict:
    """Least-squares fit of heads = middle + half_swing * cos(angle) to the dial points."""
    angles = np.radians([deg for deg, _ in points])
    heads = np.array([p for _, p in points])
    design = np.column_stack([np.ones_like(angles), np.cos(angles)])
    (middle, half_swing), *_ = np.linalg.lstsq(design, heads, rcond=None)
    return {"middle": float(middle), "swing": float(2 * half_swing)}


def summarize(pub_results) -> dict:
    """Counts, heads fraction, 95% interval and gap from theory for each experiment."""
    out: dict = {}
    for (key, _, theory, deg), pub in zip(EXPERIMENTS, pub_results):
        counts = pub.data.final.get_counts()
        heads, tails = counts.get("0", 0), counts.get("1", 0)
        n = heads + tails
        entry = {
            "counts": {"0": heads, "1": tails},
            "runs": n,
            "p_heads": heads / n,
            "ci95_heads": wilson_95(heads, n),
            "theory_p_heads": theory,
            "gap_from_theory": heads / n - theory,
        }
        if deg is not None:
            entry["dial_degrees"] = deg
        out[key] = entry
    dial = [(e["dial_degrees"], e["p_heads"]) for e in out.values() if "dial_degrees" in e]
    out["_dial"] = {
        "largest_gap_from_theory": max(abs(e["gap_from_theory"]) for e in out.values()
                                       if isinstance(e, dict) and "dial_degrees" in e),
        "fit": fit_dial(dial),
    }
    return out


# ---------------------------------------------------------------- dry run

def ideal_dry_run(circuits: list[QuantumCircuit]) -> dict:
    """Run the circuits on a noiseless local simulator and check them against theory."""
    sampler = BackendSamplerV2(
        backend=BasicSimulator(), options={"default_shots": SHOTS, "seed_simulator": SEED}
    )
    summary = summarize(sampler.run(circuits, shots=SHOTS).result())
    problems = []
    for key, title, theory, _ in EXPERIMENTS:
        got = summary[key]["p_heads"]
        exact = theory in (0.0, 1.0)
        if (exact and got != theory) or (not exact and abs(got - theory) > 0.035):
            problems.append(f"{title} gave {100 * got:.1f}% heads, theory says {100 * theory:.1f}%")
    if problems:
        raise CheckFailed("The ideal simulation disagrees with theory: " + "; ".join(problems))
    return summary


# ---------------------------------------------------------------- IBM account

def _service_class():
    try:
        from qiskit_ibm_runtime import IBMQuantumComputeService as service_class
    except ImportError:
        from qiskit_ibm_runtime import QiskitRuntimeService as service_class
    return service_class


def _looks_like_login_problem(exc: BaseException) -> bool:
    from qiskit_ibm_runtime.exceptions import IBMAccountError, IBMNotAuthorizedError

    if isinstance(exc, (IBMAccountError, IBMNotAuthorizedError)):
        return True
    text = f"{type(exc).__name__} {exc}".lower()
    network_words = ("connection", "timed out", "timeout", "max retries", "name resolution",
                     "getaddrinfo", "proxy", "ssl", "certificate")
    if any(word in text for word in network_words):
        return False
    login_words = ("api key", "apikey", "unauthorized", "not authorized", "401", "403", "iam", "token")
    return any(word in text for word in login_words)


def connect_service(force_login: bool = False):
    service_class = _service_class()
    if not force_login:
        try:
            return service_class()
        except Exception as exc:  # noqa: BLE001 - show the reason, then decide
            if not _looks_like_login_problem(exc):
                raise CheckFailed(
                    f"Could not reach IBM Quantum ({type(exc).__name__}: {first_line(exc)}). "
                    "Check the internet connection, VPN or firewall, then run the same block again."
                ) from None
            log(f"  No working IBM Quantum login on this PC yet ({first_line(exc)}).")

    log("  Paste your IBM Quantum API key and press Enter. It stays hidden while you paste.")
    log("  You can create one on your dashboard at https://quantum.cloud.ibm.com")
    log("  It is saved on this PC, so this is a one-time step.")
    try:
        key = getpass.getpass("  API key (just Enter to stop): ").strip()
    except (EOFError, KeyboardInterrupt):
        key = ""
    if not key:
        raise CheckFailed("No API key entered. Nothing was sent to the quantum computer.")
    service_class.save_account(
        token=key,
        channel="ibm_quantum_platform",
        instance="auto",
        set_as_default=True,
        overwrite=True,
    )
    try:
        return service_class()
    except Exception as exc:  # noqa: BLE001
        raise CheckFailed(
            f"IBM Quantum did not accept that key ({first_line(exc)}). "
            "Run the block again and paste a fresh key."
        ) from None


def choose_backend(service, name: str | None):
    from qiskit.providers.exceptions import QiskitBackendNotFoundError

    try:
        if name:
            backend = service.backend(name)
        else:
            backend = service.least_busy(operational=True, simulator=False)
    except QiskitBackendNotFoundError:
        if name:
            log(f"  No machine called {name} on this account right now. Using the least busy one.")
            return choose_backend(service, None)
        raise CheckFailed("No IBM machine is available right now.") from None
    try:
        waiting = backend.status().pending_jobs
        log(f"  Machine: {backend.name} ({waiting} jobs in its queue right now)")
    except Exception:  # noqa: BLE001
        log(f"  Machine: {backend.name}")
    return backend


# ---------------------------------------------------------------- calibration

def readout_details(properties, qubit: int) -> dict:
    """Readout probabilities for one qubit from IBM's calibration data, if available."""
    out: dict = {}
    if properties is None:
        return out
    try:
        qubit_props = properties.qubit_property(qubit)
    except Exception:  # noqa: BLE001
        return out
    for name in ("prob_meas1_prep0", "prob_meas0_prep1", "readout_error"):
        if name in qubit_props:
            out[name] = float(qubit_props[name][0])
    try:
        out["calibrated_at"] = properties.last_update_date.isoformat()
    except Exception:  # noqa: BLE001
        pass
    return out


def pick_qubit(backend, requested: int | None = None) -> tuple[int, dict]:
    """The qubit to use: the requested one if it beats the chip's median today, else the best.

    "Best" = smallest error budget for a two-toss circuit: readout + 2 pulses.
    """
    target = backend.target
    if "measure" not in target.operation_names or "sx" not in target.operation_names:
        raise CheckFailed(f"{backend.name} does not report measure and sx calibration data.")
    measure, sx = target["measure"], target["sx"]
    rows = []
    for qargs, props in measure.items():
        if not qargs or props is None or props.error is None:
            continue
        qubit = qargs[0]
        sx_props = sx.get((qubit,))
        if sx_props is None or sx_props.error is None:
            continue
        if props.error >= 0.5 or sx_props.error >= 0.5:
            continue
        rows.append((props.error + 2 * sx_props.error, qubit, props.error, sx_props.error))
    if not rows:
        raise CheckFailed(f"{backend.name} reports no usable qubit right now. Try again later.")
    rows.sort()
    median_readout = statistics.median(r[2] for r in rows)
    median_sx = statistics.median(r[3] for r in rows)

    chosen, rule = rows[0], "lowest (readout error + 2 x sx error) on the device"
    if requested is not None:
        match = [r for r in rows if r[1] == requested]
        if match and match[0][2] <= median_readout and match[0][3] <= median_sx:
            chosen, rule = match[0], f"qubit {requested} as requested (better than the device median today)"
        elif match:
            log(f"  Qubit {requested} is worse than the typical qubit on {backend.name} today "
                f"(readout {100 * match[0][2]:.2f}%, sqrt(X) {100 * match[0][3]:.3f}%). "
                f"Using qubit {rows[0][1]} instead.")
        else:
            log(f"  Qubit {requested} has no usable calibration on {backend.name} today. "
                f"Using qubit {rows[0][1]} instead.")
    _, qubit, readout_error, sx_error = chosen
    info = {
        "physical_qubit": qubit,
        "readout_error": readout_error,
        "sx_error": sx_error,
        "device_median_readout_error": median_readout,
        "device_median_sx_error": median_sx,
        "qubits_considered": len(rows),
        "selection_rule": rule,
        "best_qubit_today": rows[0][1],
    }
    try:
        qubit_props = target.qubit_properties[qubit]
        if qubit_props.t1 is not None:
            info["t1_us"] = qubit_props.t1 * 1e6
        if qubit_props.t2 is not None:
            info["t2_us"] = qubit_props.t2 * 1e6
    except Exception:  # noqa: BLE001
        pass
    try:
        info.update(readout_details(backend.properties(), qubit))
    except Exception:  # noqa: BLE001
        pass
    return qubit, info


# ---------------------------------------------------------------- transpile

def heads_on_paper(circuit: QuantumCircuit) -> float:
    """Chance of heads for a compiled one-qubit circuit, worked out exactly from its gates."""
    unitary = np.eye(2, dtype=complex)
    for instruction in circuit.data:
        name = instruction.operation.name
        if name in ("barrier", "measure", "delay"):
            continue
        unitary = Operator(instruction.operation).data @ unitary
    return float(abs(unitary[0, 0]) ** 2)


def transpile_and_check(backend, circuits: list[QuantumCircuit], qubit: int) -> list[QuantumCircuit]:
    """Map the circuits onto the chosen qubit and confirm nothing was merged or changed."""
    pass_manager = generate_preset_pass_manager(
        optimization_level=1, backend=backend, initial_layout=[qubit]
    )
    isa = pass_manager.run(circuits)
    problems = []
    for (key, title, theory, _), circuit in zip(EXPERIMENTS, isa):
        ops = circuit.count_ops()
        pulses = ops.get("sx", 0) + ops.get("x", 0)
        if pulses < MIN_PULSES[key]:
            problems.append(
                f"'{title}' has {pulses} pulse(s) after transpiling, needs {MIN_PULSES[key]} "
                "(the gates were merged)"
            )
        if ops.get("measure", 0) != 1:
            problems.append(f"'{title}' has {ops.get('measure', 0)} measurements, needs 1")
        used = {
            circuit.find_bit(q).index
            for instruction in circuit.data
            if instruction.operation.name != "barrier"
            for q in instruction.qubits
        }
        if used != {qubit}:
            problems.append(f"'{title}' landed on qubits {sorted(used)} instead of qubit {qubit}")
        if abs(heads_on_paper(circuit) - theory) > 1e-6:
            problems.append(f"'{title}' no longer gives {100 * theory:.1f}% heads after compiling")
    if problems:
        raise CheckFailed("Transpile check failed: " + "; ".join(problems))
    return isa


def write_circuits_txt(circuits, isa, backend_name: str, qubit: int) -> None:
    parts = [f"The minus sign circuits on {backend_name}, physical qubit {qubit}. Written {utc_now()}.", ""]
    for (_, title, _, _), circuit, isa_circuit in zip(EXPERIMENTS, circuits, isa):
        parts += [
            f"=== {title} ===",
            "As written:",
            str(circuit.draw(output="text")),
            "",
            "As the hardware runs it (Rz is a free frame change, sqrt(X) is a real microwave pulse):",
            str(isa_circuit.draw(output="text", idle_wires=False)),
            "",
        ]
    CIRCUITS_TXT.write_text("\n".join(parts), encoding="utf-8")


# ---------------------------------------------------------------- submit and wait

def usage_and_queue_check(service, assume_yes: bool) -> bool:
    """Warn if the free QPU time is used up or older jobs are still waiting."""
    concerns = []
    try:
        usage = service.usage() or {}
        used = usage.get("usage_consumed_seconds")
        limit = usage.get("usage_limit_seconds") or usage.get("usage_allocation_seconds")
        left = usage.get("usage_remaining_seconds")
        if used is not None and limit:
            if left is None:
                left = max(limit - used, 0)
            log(f"  QPU time on this account: {used:.0f} s used of {limit:.0f} s ({left:.0f} s left).")
        if usage.get("usage_limit_reached") or (left is not None and left < 60):
            concerns.append(
                "Your QPU time is nearly used up. IBM will hold the job until time comes back "
                "(Open Plan: 10 minutes per rolling 28 days). This job needs about 30 seconds."
            )
    except Exception as exc:  # noqa: BLE001
        log(f"  (Could not read the QPU time left: {first_line(exc)})")
    try:
        waiting = service.jobs(pending=True, limit=100)
        if waiting:
            concerns.append(
                f"{len(waiting)} earlier job(s) on this account are still waiting or running. "
                "This one will probably wait behind them."
            )
    except Exception as exc:  # noqa: BLE001
        log(f"  (Could not list your waiting jobs: {first_line(exc)})")
    if not concerns:
        return True
    for concern in concerns:
        log("  NOTE: " + concern)
    if assume_yes:
        return True
    try:
        answer = input("  Submit anyway? [y/N]: ")
    except (EOFError, KeyboardInterrupt):
        answer = ""
    return answer.strip().lower() in ("y", "yes")


def submit(backend, isa: list[QuantumCircuit]):
    from qiskit_ibm_runtime import SamplerV2

    with warnings.catch_warnings():
        # SamplerV2 is deprecated in qiskit-ibm-runtime 0.50 but stays available until
        # at least late December 2026. It returns results that can be collected again
        # later by job ID, which the resume step relies on.
        warnings.simplefilter("ignore", DeprecationWarning)
        sampler = SamplerV2(mode=backend)
    options = sampler.options
    options.default_shots = SHOTS
    options.dynamical_decoupling.enable = False
    options.twirling.enable_gates = False
    options.twirling.enable_measure = False
    options.environment.job_tags = JOB_TAGS
    try:
        return sampler.run(isa, shots=SHOTS)
    except Exception as exc:  # noqa: BLE001
        raise CheckFailed(
            f"IBM did not accept the job ({type(exc).__name__}: {first_line(exc)}). Nothing ran."
        ) from None


def find_job(service, meta: dict):
    """The job submitted earlier, looked up by the ID saved in job.json."""
    from qiskit_ibm_runtime.exceptions import RuntimeJobNotFound

    try:
        return service.job(meta["job_id"])
    except RuntimeJobNotFound:
        missing = RESULTS / f"job_not_found_{meta['job_id']}.json"
        JOB_FILE.replace(missing)
        raise CheckFailed(
            f"IBM has no job {meta['job_id']} on this account any more. Its record was moved to "
            f"{rel(missing)}. Paste the block again to submit a fresh job."
        ) from None
    except Exception as exc:  # noqa: BLE001
        raise CheckFailed(
            f"Could not look up job {meta['job_id']} ({first_line(exc)}). Paste the same block again to retry."
        ) from None


def status_name(job) -> str:
    status = job.status()
    return str(getattr(status, "name", status)).upper()


def wait_for(job, poll_seconds: float = 20) -> str:
    last, last_note = None, time.monotonic()
    while True:
        try:
            status = status_name(job)
        except Exception as exc:  # noqa: BLE001
            log(f"  [{clock()}] could not reach IBM ({first_line(exc)}); trying again in 60 s")
            time.sleep(60)
            continue
        if status != last:
            log(f"  [{clock()}] {STATUS_TEXT.get(status, status.lower())}")
            last, last_note = status, time.monotonic()
        elif time.monotonic() - last_note >= 300:
            log(f"  [{clock()}] still {STATUS_TEXT.get(status, status.lower())}")
            last_note = time.monotonic()
        if status in ("DONE", "ERROR", "CANCELLED"):
            return status
        time.sleep(poll_seconds)


def versions() -> dict:
    return {
        "python": platform.python_version(),
        "qiskit": qiskit.__version__,
        "qiskit_ibm_runtime": qiskit_ibm_runtime.__version__,
        "os": f"{platform.system()} {platform.release()}",
    }


def qpu_seconds(record: dict) -> float | None:
    usage = record.get("ibm_usage") or {}
    for key in ("qpu_charge_time_seconds", "quantum_seconds", "seconds"):
        value = usage.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def build_record(job, meta: dict, summary: dict) -> dict:
    record = {
        "experiment": "The minus sign you can't see (Quantum, Explained #2)",
        "data": "Raw counts from IBM quantum hardware. No error mitigation, no post-selection.",
        "heads_tails": "heads = 0, tails = 1",
        "backend": meta["backend"],
        "physical_qubit": meta["physical_qubit"],
        "job_id": meta["job_id"],
        "submitted_utc": meta["submitted_utc"],
        "collected_utc": utc_now(),
        "shots_per_experiment": meta["shots_per_experiment"],
        "results": summary,
        "calibration_at_submit": meta["calibration"],
        "transpiled_gate_counts": meta["isa_ops"],
        "software": versions(),
    }
    try:
        metrics = job.metrics()
        record["ibm_timestamps"] = metrics.get("timestamps")
        record["ibm_usage"] = metrics.get("usage")
    except Exception:  # noqa: BLE001
        pass
    try:
        at_run = readout_details(job.properties(), meta["physical_qubit"])
        if at_run:
            record["calibration_at_run"] = at_run
    except Exception:  # noqa: BLE001
        pass
    return record


def finish(job, meta: dict) -> int:
    log("Step 4/4  Waiting for the result. You can close this window and paste the same")
    log("          block again later; it collects this job instead of submitting a new one.")
    try:
        status = wait_for(job)
    except KeyboardInterrupt:
        log("\nStopped waiting. The job is still with IBM. Paste the same block again later to collect it.")
        return 3
    if status != "DONE":
        reason = ""
        try:
            reason = job.error_message() or ""
        except Exception:  # noqa: BLE001
            pass
        failed = RESULTS / f"job_{status.lower()}_{meta['job_id']}.json"
        if JOB_FILE.exists():
            JOB_FILE.replace(failed)
        log(f"The job ended as {status}. {reason}".rstrip())
        log(f"Its record was moved to {rel(failed)}. Paste the block again to submit a fresh job.")
        return 1
    try:
        result = job.result()
    except Exception as exc:  # noqa: BLE001
        raise CheckFailed(
            f"The job finished but the result did not download ({first_line(exc)}). "
            "Paste the same block again to retry; the job is not run again."
        ) from None
    summary = summarize(result)
    record = build_record(job, meta, summary)
    HW_JSON.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    save_chart(summary, record)
    print_summary(summary, record)
    open_file(HW_PNG)
    return 0


def save_chart(summary: dict, record: dict) -> None:
    try:
        make_plot(summary, record, HW_PNG, simulated=False)
    except Exception as exc:  # noqa: BLE001 - the numbers are already saved
        log(f"  (The results are saved, but drawing the chart failed: {first_line(exc)})")


# ---------------------------------------------------------------- output

def print_summary(summary: dict, record: dict) -> None:
    log("")
    log("=" * 70)
    log(f" RESULT  {record['backend']}, qubit {record['physical_qubit']}, "
        f"{record['shots_per_experiment']:,} runs each, no error mitigation")
    log("=" * 70)
    for key in ("spin_look", "spin_between"):
        title = next(t for k, t, _, _ in EXPERIMENTS if k == key)
        heads = 100 * summary[key]["p_heads"]
        log(f" {title:<30} heads {heads:5.1f}%   tails {100 - heads:5.1f}%")
    log("-" * 70)
    log(" The dial: H, turn by the angle, H, look")
    for key, _, theory, deg in EXPERIMENTS:
        if deg is None:
            continue
        heads = 100 * summary[key]["p_heads"]
        log(f"   {deg:3d} deg   heads {heads:5.1f}%   (theory {100 * theory:5.1f}%)")
    dial = summary["_dial"]
    log(f" Largest gap from theory: {100 * dial['largest_gap_from_theory']:.1f} points. "
        f"Fitted swing from top to bottom: {100 * dial['fit']['swing']:.1f}% (theory 100%).")
    log("-" * 70)
    at_submit = record.get("calibration_at_submit") or {}
    at_run = record.get("calibration_at_run") or {}
    misread = at_run.get("prob_meas1_prep0", at_submit.get("prob_meas1_prep0"))
    if misread is not None:
        log(f" IBM's calibration for this qubit: a 0 is misread as 1 in {100 * misread:.2f}% of readouts.")
    if "readout_error" in at_submit:
        log(f" Readout error {100 * at_submit['readout_error']:.2f}% "
            f"(device median {100 * at_submit['device_median_readout_error']:.2f}%), "
            f"sqrt(X) pulse error {100 * at_submit['sx_error']:.3f}%.")
    charged = qpu_seconds(record)
    if charged is not None:
        log(f" QPU time charged: {charged:.1f} s")
    log(f" Saved: {rel(HW_JSON)}, {rel(HW_PNG)}, {rel(CIRCUITS_TXT)}")
    log("=" * 70)


def make_plot(summary: dict, record: dict, path: Path, simulated: bool = False) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.sans-serif": ["Segoe UI", "Helvetica Neue", "Arial", "DejaVu Sans"],
            "font.size": 11,
        }
    )
    accent, grey, ink, quiet = "#2563EB", "#94A3B8", "#0F172A", "#64748B"
    fig, (bars, curve) = plt.subplots(
        1, 2, figsize=(12, 5), dpi=200, gridspec_kw={"width_ratios": [1, 1.6], "wspace": 0.28}
    )

    # Left: the two named experiments, next to last week's toss twice (the dial at 0 degrees)
    keys = ["dial_000", "spin_look", "spin_between"]
    labels = ["H, H\n(last week's\ntoss twice)", "H, Z, look", "H, Z, H"]
    heads = [100 * summary[k]["p_heads"] for k in keys]
    low = [max(0.0, h - 100 * summary[k]["ci95_heads"][0]) for h, k in zip(heads, keys)]
    high = [max(0.0, 100 * summary[k]["ci95_heads"][1] - h) for h, k in zip(heads, keys)]
    bars.bar(range(3), heads, width=0.55, color=[grey, grey, accent], zorder=2)
    bars.errorbar(range(3), heads, yerr=[low, high], fmt="none", ecolor="#1E293B",
                  elinewidth=1, capsize=4, zorder=3)
    bars.axhline(50, color=quiet, linestyle=(0, (4, 3)), linewidth=1.2, zorder=1)
    for i, value in enumerate(heads):
        bars.text(i, value + 3, f"{value:.1f}%", ha="center", va="bottom",
                  fontsize=12, fontweight="bold", color=ink)
    bars.set_xticks(range(3))
    bars.set_xticklabels(labels, fontsize=9.5)
    bars.set_xlim(-0.6, 2.6)
    bars.set_ylim(0, 112)
    bars.set_yticks([0, 25, 50, 75, 100])
    bars.set_yticklabels(["0%", "25%", "50%", "75%", "100%"])
    bars.set_ylabel("Runs that came up heads (0)", color="#334155")

    # Right: the dial, measured points on top of the theory curve
    dial_keys = [(k, deg, theory) for k, _, theory, deg in EXPERIMENTS if deg is not None]
    fine = np.linspace(0, 360, 361)
    curve.plot(fine, 100 * np.cos(np.radians(fine) / 2) ** 2, color=grey, linewidth=2, zorder=1,
               label="theory: cos²(angle / 2)")
    degs = [deg for _, deg, _ in dial_keys]
    measured = [100 * summary[k]["p_heads"] for k, _, _ in dial_keys]
    curve.plot(degs, measured, "o", color=accent, markersize=7, zorder=3,
               markeredgecolor="white", markeredgewidth=1.2, label="measured")
    curve.set_xticks(range(0, 361, 45))
    curve.set_xticklabels([f"{d}°" for d in range(0, 361, 45)])
    curve.set_xlim(-10, 370)
    curve.set_ylim(-4, 108)
    curve.set_yticks([0, 25, 50, 75, 100])
    curve.set_yticklabels(["0%", "25%", "50%", "75%", "100%"])
    curve.set_xlabel("Dial setting: how far the arrow turns around the poles", color="#334155")
    curve.legend(loc="lower left", frameon=False, fontsize=9.5)
    curve.annotate("180° = Z", xy=(180, measured[degs.index(180)]), xytext=(180, 22),
                   ha="center", fontsize=9.5, color=quiet,
                   arrowprops={"arrowstyle": "-", "color": quiet, "linewidth": 0.8})

    for ax in (bars, curve):
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color("#CBD5E1")
        ax.tick_params(colors="#334155", length=0)
        ax.yaxis.grid(True, color="#E2E8F0", linewidth=0.8)
        ax.set_axisbelow(True)

    if simulated:
        title, title_color = "DRY RUN: ideal simulation, not hardware", "#B91C1C"
        subtitle = (f"Noiseless simulator, {SHOTS:,} runs per setting. "
                    "Checks the code before any hardware time is used.")
    else:
        title, title_color = "One minus sign flips heads to tails", ink
        date = (record.get("collected_utc") or record.get("submitted_utc") or "")[:10]
        subtitle = (f"{record['backend']}, qubit {record['physical_qubit']}, "
                    f"{record['shots_per_experiment']:,} runs per setting, {date}. "
                    "Raw counts, no error mitigation.")
    fig.text(0.07, 1.02, title, ha="left", va="bottom", fontsize=15, fontweight="bold", color=title_color)
    fig.text(0.07, 0.975, subtitle, ha="left", va="bottom", fontsize=9.5, color="#475569")
    fig.text(0.07, -0.06, "Heads = 0, tails = 1. Error bars: 95% confidence intervals. "
             "Z is the dial at 180°.", ha="left", va="top", fontsize=8.5, color=quiet)

    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def open_file(path: Path) -> None:
    if sys.platform == "win32":
        try:
            os.startfile(str(path))  # noqa: S606 - opens the chart in the default viewer
        except OSError:
            pass


def archive_old_results() -> None:
    if not RESULTS.exists() or not any(RESULTS.iterdir()):
        return
    if JOB_FILE.exists() and not HW_JSON.exists():
        log("  Note: the job in the archived job.json may still run on IBM and use QPU time.")
    destination = HERE / "results_archive" / datetime.now().strftime("%Y%m%d-%H%M%S")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(RESULTS), str(destination))
    log(f"  Archived the previous results to {rel(destination)}")


# ---------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="The minus sign you can't see, on IBM quantum hardware.")
    parser.add_argument("--dry-run", action="store_true",
                        help="run every check, send nothing to the quantum computer")
    parser.add_argument("--backend", help="IBM machine to use, e.g. ibm_fez (default: least busy)")
    parser.add_argument("--qubit", type=int,
                        help="preferred physical qubit, used if it beats the chip's median today")
    parser.add_argument("--login", action="store_true", help="enter and save a new IBM Quantum API key")
    parser.add_argument("--new", action="store_true", help="fresh hardware run; archive older results")
    parser.add_argument("--yes", action="store_true", help="submit without asking, even after a warning")
    args = parser.parse_args(argv)

    if args.new:
        archive_old_results()
    RESULTS.mkdir(parents=True, exist_ok=True)

    if HW_JSON.exists() and not args.dry_run:
        record = json.loads(HW_JSON.read_text(encoding="utf-8"))
        log("These are the results of your earlier hardware run (add --new for a fresh run).")
        save_chart(record["results"], record)
        print_summary(record["results"], record)
        open_file(HW_PNG)
        return 0

    circuits = build_circuits()
    log("Step 1/4  Dry run on a noiseless simulator (nothing is sent to IBM)")
    ideal = ideal_dry_run(circuits)
    make_plot(ideal, {}, DRY_DIR / "ideal_simulation.png", simulated=True)
    for key in ("spin_look", "spin_between", "dial_090"):
        title = next(t for k, t, _, _ in EXPERIMENTS if k == key)
        log(f"  {title:<30} heads {100 * ideal[key]['p_heads']:5.1f}%")
    log(f"  ...and {len(DIAL_DEGREES)} dial settings from 0 to 360 deg. All match theory.")

    log("Step 2/4  Connecting to IBM Quantum")
    service = connect_service(force_login=args.login)

    if JOB_FILE.exists() and not args.dry_run:
        meta = json.loads(JOB_FILE.read_text(encoding="utf-8"))
        log(f"  Found the job you already submitted ({meta['job_id']} on {meta['backend']}).")
        log("  Collecting that one instead of submitting a second job.")
        return finish(find_job(service, meta), meta)

    backend = choose_backend(service, args.backend)
    # A preferred qubit only makes sense on the machine it was chosen for
    wanted = args.qubit if (args.backend is None or backend.name == args.backend) else None
    qubit, calibration = pick_qubit(backend, wanted)
    log(f"  Qubit {qubit}: readout error {100 * calibration['readout_error']:.2f}% "
        f"(device median {100 * calibration['device_median_readout_error']:.2f}%), "
        f"sqrt(X) pulse error {100 * calibration['sx_error']:.3f}%")
    isa = transpile_and_check(backend, circuits, qubit)
    write_circuits_txt(circuits, isa, backend.name, qubit)
    log("  Transpile check passed: every two-toss circuit keeps both H pulses, and every")
    log("  compiled circuit still gives the right answer on paper.")
    if args.dry_run:
        log("Dry run passed. Nothing was sent to the quantum computer.")
        return 0

    log(f"Step 3/4  Submitting {len(circuits)} circuits x {SHOTS:,} runs to {backend.name}")
    if not usage_and_queue_check(service, assume_yes=args.yes):
        log("Not submitted. Nothing was sent to the quantum computer.")
        return 2
    job = submit(backend, isa)
    meta = {
        "job_id": job.job_id(),
        "backend": backend.name,
        "physical_qubit": qubit,
        "submitted_utc": utc_now(),
        "shots_per_experiment": SHOTS,
        "calibration": calibration,
        "isa_ops": {key: dict(c.count_ops()) for (key, _, _, _), c in zip(EXPERIMENTS, isa)},
    }
    JOB_FILE.write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
    log(f"  Submitted. Job ID {meta['job_id']} (saved in {rel(JOB_FILE)})")
    return finish(job, meta)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except CheckFailed as exc:
        log(f"\nSTOPPED: {exc}")
        sys.exit(1)
    except KeyboardInterrupt:
        log("\nStopped. If a job was already submitted, paste the same block again to collect it.")
        sys.exit(130)
