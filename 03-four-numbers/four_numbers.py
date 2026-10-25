"""Why do two qubits need four numbers? The experiment behind the third Quantum Basics post.

Two-qubit experiments on a real IBM quantum computer, all on the same pair of
connected physical qubits, A and B, 4,000 runs each. Heads = 0, tails = 1.
Every result is written A first, then B: "10" means A gave tails and B gave heads.

  1. Toss both                 H on A and B, look              -> 00, 01, 10, 11, about 25% each
  2. Sign, then look           H, H, CZ, look                  -> still about 25% each: a look can't see the sign
  3. Toss B again              H, H, H on B, look              -> 00 and 10, 50% each: B is back to heads
  4. Sign, then toss B again   H, H, CZ, H on B, look          -> 00 and 11, 50% each: B now matches A
  5. The dial, 13 settings     H, H, CP(angle), H on B, look   -> A and B agree in 50% of runs at 0 deg and
                                                                  100% at 180 deg, following 3/4 - cos(angle)/4

Two qubits have four possible results, 00, 01, 10 and 11, and the state gives
each one its own weight. CZ puts a minus sign on the weight of 11 and on nothing
else. No sign on A alone or on B alone can do that, and experiment 4 shows the
sign is real: B's answer now matches A's. CP(angle) turns the weight of 11 by
any angle. CP(180 deg) is exactly CZ, and CP(0) does nothing, so the dial's
first setting is experiment 3.

Barriers stop Qiskit from merging or dropping gates before the job is sent.
Without them, the compiler would cancel B's two H gates in experiment 3, and it
would delete the CZ in experiment 2, because a sign right before a look changes
nothing a look can see. The script checks that the hardware will really run
every H and every CZ, and that the compiled circuits still give the right
answers on paper, before it submits anything.

Run from this folder:
  python four_numbers.py                     dry run, submit to IBM, wait, save results
  python four_numbers.py --dry-run           every check, nothing sent to the quantum computer
  python four_numbers.py --backend NAME      use a named machine instead of the least busy one
  python four_numbers.py --qubits 22 23      prefer this pair as A and B, if it beats the chip's median today
  python four_numbers.py --login             enter a new IBM Quantum API key
  python four_numbers.py --new               fresh hardware run; older results are archived

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
    from qiskit.quantum_info import Operator, Statevector
    from qiskit.transpiler import generate_preset_pass_manager
except ImportError as exc:  # pragma: no cover
    sys.exit(f"Missing package ({exc.name}). Run the PowerShell setup block first.")

SHOTS = 4000
SEED = 2026
JOB_TAGS = ["quantum-blog", "03-four-numbers"]
DIAL_DEGREES = list(range(0, 361, 30))
OUTCOMES = ("00", "01", "10", "11")      # A first, then B
TWO_QUBIT_GATES = ("cz", "ecr", "cx")    # Heron chips use cz, older Eagle chips ecr

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
JOB_FILE = RESULTS / "job.json"
HW_JSON = RESULTS / "hardware_results.json"
HW_PNG = RESULTS / "four_numbers_hardware.png"
CIRCUITS_TXT = RESULTS / "circuits.txt"
DRY_DIR = RESULTS / "dryrun"


def _dial_theory(deg: int) -> dict[str, float]:
    """Chance of each result for H, H, CP(deg), H on B, look."""
    half = math.radians(deg) / 2
    return {
        "00": 0.5,
        "01": 0.0,
        "10": round(math.cos(half) ** 2 / 2, 12),
        "11": round(math.sin(half) ** 2 / 2, 12),
    }


def _experiments() -> list[tuple[str, str, dict[str, float], int | None]]:
    """(key, label in the console, chance of each result in theory, dial angle in degrees or None)."""
    even = {ab: 0.25 for ab in OUTCOMES}
    rows: list[tuple[str, str, dict[str, float], int | None]] = [
        ("toss_both", "Toss both", dict(even), None),
        ("sign_look", "Sign, then look", dict(even), None),
        ("toss_b_again", "Toss B again", {"00": 0.5, "01": 0.0, "10": 0.5, "11": 0.0}, None),
        ("sign_toss_b", "Sign, then toss B again", {"00": 0.5, "01": 0.0, "10": 0.0, "11": 0.5}, None),
    ]
    for deg in DIAL_DEGREES:
        rows.append((f"dial_{deg:03d}", f"Dial at {deg} deg", _dial_theory(deg), deg))
    return rows


EXPERIMENTS = _experiments()
TITLES = {key: title for key, title, _, _ in EXPERIMENTS}
MAIN_FOUR = ("toss_both", "sign_look", "toss_b_again", "sign_toss_b")
# B is tossed twice in these, so it must keep at least 2 physical pulses after compiling.
# Fewer would mean its two H gates were merged.
B_TOSSED_TWICE = {key for key, _, _, _ in EXPERIMENTS if key not in ("toss_both", "sign_look")}
# The sign in these must survive compiling as at least one real two-qubit pulse.
# The dial at 0 and 360 deg turns nothing, so it needs none.
HAS_SIGN = {"sign_look", "sign_toss_b"} | {f"dial_{deg:03d}" for deg in DIAL_DEGREES if deg % 360}
# These have no sign, so a two-qubit pulse here would mean the compiler moved the qubits.
NO_SIGN = {"toss_both", "toss_b_again"}

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

def _two_qubits(name: str) -> QuantumCircuit:
    """Qubit 0 is A and qubit 1 is B. Bit 0 records A and bit 1 records B."""
    return QuantumCircuit(QuantumRegister(2, "q"), ClassicalRegister(2, "final"), name=name)


def _look(circuit: QuantumCircuit) -> None:
    circuit.barrier()              # without it, a sign right before the look gets deleted
    circuit.measure([0, 1], [0, 1])


def build_circuits() -> list[QuantumCircuit]:
    """The experiments, in the order of EXPERIMENTS."""
    toss_both = _two_qubits("toss_both")
    toss_both.h([0, 1])
    _look(toss_both)

    sign_look = _two_qubits("sign_look")
    sign_look.h([0, 1])
    sign_look.barrier()
    sign_look.cz(0, 1)             # the minus sign on the weight of 11, and only 11
    _look(sign_look)

    toss_b_again = _two_qubits("toss_b_again")
    toss_b_again.h([0, 1])
    toss_b_again.barrier()         # without it, B's two H gates cancel before the job is sent
    toss_b_again.h(1)
    _look(toss_b_again)

    sign_toss_b = _two_qubits("sign_toss_b")
    sign_toss_b.h([0, 1])
    sign_toss_b.barrier()
    sign_toss_b.cz(0, 1)
    sign_toss_b.barrier()
    sign_toss_b.h(1)
    _look(sign_toss_b)

    circuits = [toss_both, sign_look, toss_b_again, sign_toss_b]
    for deg in DIAL_DEGREES:
        dial = _two_qubits(f"dial_{deg:03d}")
        dial.h([0, 1])
        dial.barrier()
        dial.cp(math.radians(deg), 0, 1)   # turns the weight of 11 by `deg` degrees
        dial.barrier()
        dial.h(1)
        _look(dial)
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
    """Least-squares fit of agree = middle - (swing / 2) * cos(angle) to the dial points."""
    angles = np.radians([deg for deg, _ in points])
    agree = np.array([p for _, p in points])
    design = np.column_stack([np.ones_like(angles), -np.cos(angles)])
    (middle, half_swing), *_ = np.linalg.lstsq(design, agree, rcond=None)
    return {"middle": float(middle), "swing": float(2 * half_swing)}


def ab_counts(raw: dict) -> dict[str, int]:
    """Counts keyed A first. Qiskit writes bit 1 (B) on the left, so each key is flipped."""
    counts = {ab: 0 for ab in OUTCOMES}
    for bits, n in raw.items():
        counts[bits[::-1]] += n
    return counts


def summarize(pub_results) -> dict:
    """Counts, shares, 95% intervals and gaps from theory for each experiment, A first."""
    out: dict = {}
    for (key, _, theory, deg), pub in zip(EXPERIMENTS, pub_results):
        counts = ab_counts(pub.data.final.get_counts())
        n = sum(counts.values())
        p = {ab: counts[ab] / n for ab in OUTCOMES}
        agree = counts["00"] + counts["11"]
        entry = {
            "counts": counts,
            "runs": n,
            "p": p,
            "ci95": {ab: wilson_95(counts[ab], n) for ab in OUTCOMES},
            "theory_p": theory,
            "largest_gap_from_theory": max(abs(p[ab] - theory[ab]) for ab in OUTCOMES),
            "p_agree": agree / n,
            "ci95_agree": wilson_95(agree, n),
            "theory_p_agree": round(theory["00"] + theory["11"], 12),
            "p_a_heads": (counts["00"] + counts["01"]) / n,
            "p_b_heads": (counts["00"] + counts["10"]) / n,
        }
        if deg is not None:
            entry["dial_degrees"] = deg
        out[key] = entry
    dial = [entry for entry in out.values() if "dial_degrees" in entry]
    out["_dial"] = {
        "largest_gap_from_theory": max(abs(e["p_agree"] - e["theory_p_agree"]) for e in dial),
        "fit": fit_dial([(e["dial_degrees"], e["p_agree"]) for e in dial]),
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
        for ab in OUTCOMES:
            got, want = summary[key]["p"][ab], theory[ab]
            if (want == 0.0 and got != 0.0) or (want != 0.0 and abs(got - want) > 0.035):
                problems.append(f"{title} gave {ab} in {100 * got:.1f}% of runs, theory says {100 * want:.1f}%")
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


def ask_yes(prompt: str) -> bool:
    try:
        answer = input(prompt)
    except (EOFError, KeyboardInterrupt):
        answer = ""
    return answer.strip().lower() in ("y", "yes")


def choose_backend(service, name: str | None, confirm: bool = False):
    """The named machine, or the least busy one.

    With confirm, ask before running anywhere other than the named machine.
    """
    from qiskit.providers.exceptions import QiskitBackendNotFoundError

    def switch(reason: str):
        log(f"  {reason}")
        if confirm and not ask_yes("  Use the least busy machine instead? [y/N]: "):
            raise CheckFailed(f"Nothing was sent to the quantum computer. Paste the same block again "
                              f"later to try {name} again.")
        log("  Using the least busy machine instead.")
        return choose_backend(service, None)

    try:
        if name:
            backend = service.backend(name)
        else:
            backend = service.least_busy(operational=True, simulator=False)
    except QiskitBackendNotFoundError:
        if name:
            return switch(f"No machine called {name} on this account right now.")
        raise CheckFailed("No IBM machine is available right now.") from None
    try:
        status = backend.status()
    except Exception:  # noqa: BLE001
        log(f"  Machine: {backend.name}")
        return backend
    if name and not getattr(status, "operational", True):
        detail = str(getattr(status, "status_msg", "") or "").strip()
        return switch(f"{backend.name} is not taking jobs right now" + (f" ({detail})." if detail else "."))
    log(f"  Machine: {backend.name} ({status.pending_jobs} jobs in its queue right now)")
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


def two_qubit_gate(target) -> str:
    for name in TWO_QUBIT_GATES:
        if name in target.operation_names:
            return name
    raise CheckFailed(f"The chip offers none of the two-qubit gates {', '.join(TWO_QUBIT_GATES)}.")


def pick_pair(backend, requested: tuple[int, int] | None = None) -> tuple[tuple[int, int], dict]:
    """Qubits A and B: the requested pair if it beats the chip's median today, else the best pair.

    "Best" = smallest error budget for a dial circuit: both readouts, two two-qubit
    pulses, one pulse on A and two on B.
    """
    target = backend.target
    if "measure" not in target.operation_names or "sx" not in target.operation_names:
        raise CheckFailed(f"{backend.name} does not report measure and sx calibration data.")
    gate = two_qubit_gate(target)
    measure, sx, link = target["measure"], target["sx"], target[gate]

    single: dict[int, tuple[float, float]] = {}
    for qargs, props in measure.items():
        if not qargs or props is None or props.error is None:
            continue
        sx_props = sx.get((qargs[0],))
        if sx_props is None or sx_props.error is None:
            continue
        if props.error >= 0.5 or sx_props.error >= 0.5:
            continue
        single[qargs[0]] = (props.error, sx_props.error)

    links: dict[tuple[int, int], float] = {}
    for qargs, props in link.items():
        if not qargs or len(qargs) != 2 or props is None or props.error is None or props.error >= 0.5:
            continue
        if qargs[0] not in single or qargs[1] not in single:
            continue
        pair = (min(qargs), max(qargs))
        # A one-way gate (ecr) still links both qubits; the compiler turns it around.
        links[pair] = min(links.get(pair, 1.0), props.error)
    if not links:
        raise CheckFailed(f"{backend.name} reports no usable pair of connected qubits right now. Try again later.")

    rows = []
    for (p, q), link_error in links.items():
        for a, b in ((p, q), (q, p)):
            (read_a, sx_a), (read_b, sx_b) = single[a], single[b]
            cost = read_a + read_b + 2 * link_error + sx_a + 2 * sx_b
            rows.append((cost, a, b, link_error))
    rows.sort()
    median_cost = statistics.median(r[0] for r in rows)

    chosen = rows[0]
    rule = "lowest (both readout errors + 2 x two-qubit error + 1 sx on A + 2 sx on B) on the device"
    if requested is not None:
        match = [r for r in rows if (r[1], r[2]) == tuple(requested)]
        if match and match[0][0] <= median_cost:
            chosen = match[0]
            rule = f"qubits {requested[0]} and {requested[1]} as requested (better than the device median today)"
        elif match:
            log(f"  Qubits {requested[0]} and {requested[1]} are worse than the typical pair on {backend.name} "
                f"today. Using {rows[0][1]} and {rows[0][2]} instead.")
        else:
            log(f"  Qubits {requested[0]} and {requested[1]} are not a connected pair with usable calibration "
                f"on {backend.name} today. Using {rows[0][1]} and {rows[0][2]} instead.")
    _, a, b, link_error = chosen
    info = {
        "physical_qubits": {"A": a, "B": b},
        "two_qubit_gate": gate,
        "two_qubit_gate_error": link_error,
        "readout_error": {"A": single[a][0], "B": single[b][0]},
        "sx_error": {"A": single[a][1], "B": single[b][1]},
        "device_median_readout_error": statistics.median(e[0] for e in single.values()),
        "device_median_sx_error": statistics.median(e[1] for e in single.values()),
        "device_median_two_qubit_gate_error": statistics.median(links.values()),
        "pairs_considered": len(links),
        "selection_rule": rule,
        "best_pair_today": {"A": rows[0][1], "B": rows[0][2]},
    }
    for role, qubit in (("A", a), ("B", b)):
        details: dict = {}
        try:
            qubit_props = target.qubit_properties[qubit]
            if qubit_props.t1 is not None:
                details["t1_us"] = qubit_props.t1 * 1e6
            if qubit_props.t2 is not None:
                details["t2_us"] = qubit_props.t2 * 1e6
        except Exception:  # noqa: BLE001
            pass
        try:
            details.update(readout_details(backend.properties(), qubit))
        except Exception:  # noqa: BLE001
            pass
        if details:
            info[f"qubit_{role}"] = details
    return (a, b), info


# ---------------------------------------------------------------- transpile

def outcomes_on_paper(circuit: QuantumCircuit) -> tuple[dict[str, float], dict[int, int]]:
    """Chance of each result (A first) for a compiled circuit, worked out exactly from its gates.

    Also returns which physical qubit each classical bit reads: {0: A's qubit, 1: B's qubit}.
    """
    local: dict[int, int] = {}      # physical qubit -> 0 or 1 in a two-qubit state
    reads: dict[int, int] = {}      # classical bit -> physical qubit
    state = Statevector.from_label("00")
    for instruction in circuit.data:
        name = instruction.operation.name
        if name in ("barrier", "delay"):
            continue
        qubits = [circuit.find_bit(q).index for q in instruction.qubits]
        for qubit in qubits:
            local.setdefault(qubit, len(local))
        if len(local) > 2:
            raise ValueError(f"it touches qubits {sorted(local)}, more than two")
        if name == "measure":
            reads[circuit.find_bit(instruction.clbits[0]).index] = qubits[0]
            continue
        if any(qubit in reads.values() for qubit in qubits):
            raise ValueError("a gate comes after the look")
        state = state.evolve(Operator(instruction.operation), qargs=[local[q] for q in qubits])
    if sorted(reads) != [0, 1] or len(local) != 2:
        raise ValueError("it does not look at both qubits")
    local_a, local_b = local[reads[0]], local[reads[1]]
    chances = {ab: 0.0 for ab in OUTCOMES}
    for index, amplitude in enumerate(state.data):
        chances[f"{(index >> local_a) & 1}{(index >> local_b) & 1}"] += float(abs(amplitude) ** 2)
    return chances, reads


def transpile_and_check(backend, circuits: list[QuantumCircuit], pair: tuple[int, int]) -> list[QuantumCircuit]:
    """Map the circuits onto qubits A and B and confirm nothing was merged, dropped or moved."""
    a, b = pair
    # Level 2 compiles each sign into as few two-qubit pulses as the math allows:
    # none for CP(0) and CP(360 deg), one for CZ, two for the other dial settings.
    pass_manager = generate_preset_pass_manager(
        optimization_level=2, backend=backend, initial_layout=[a, b], seed_transpiler=SEED
    )
    isa = pass_manager.run(circuits)
    problems = []
    for (key, title, theory, _), circuit in zip(EXPERIMENTS, isa):
        ops = circuit.count_ops()
        pulses = {a: 0, b: 0}
        for instruction in circuit.data:
            if instruction.operation.name in ("sx", "x"):
                qubit = circuit.find_bit(instruction.qubits[0]).index
                pulses[qubit] = pulses.get(qubit, 0) + 1
        need_b = 2 if key in B_TOSSED_TWICE else 1
        if pulses[a] < 1:
            problems.append(f"'{title}': qubit A has no pulse left for its H gate")
        if pulses[b] < need_b:
            problems.append(f"'{title}': qubit B has {pulses[b]} pulse(s) after compiling, needs {need_b} "
                            "(its H gates were merged)")
        # Every two-qubit operation counts, whatever the chip calls it
        two = sum(1 for instruction in circuit.data
                  if instruction.operation.num_qubits == 2 and instruction.operation.name != "barrier")
        if key in HAS_SIGN and two < 1:
            problems.append(f"'{title}' has no two-qubit pulse after compiling (the sign was dropped)")
        if key in NO_SIGN and two:
            problems.append(f"'{title}' gained {two} two-qubit pulse(s) it should not have")
        if ops.get("measure", 0) != 2:
            problems.append(f"'{title}' has {ops.get('measure', 0)} measurements, needs 2")
        used = {
            circuit.find_bit(q).index
            for instruction in circuit.data
            if instruction.operation.name != "barrier"
            for q in instruction.qubits
        }
        if used != {a, b}:
            problems.append(f"'{title}' landed on qubits {sorted(used)} instead of {a} and {b}")
            continue
        try:
            chances, reads = outcomes_on_paper(circuit)
        except ValueError as exc:
            problems.append(f"'{title}': {exc}")
            continue
        if reads != {0: a, 1: b}:
            problems.append(f"'{title}' reads A from qubit {reads.get(0)} and B from qubit {reads.get(1)}, "
                            f"not {a} and {b}")
        if max(abs(chances[ab] - theory[ab]) for ab in OUTCOMES) > 1e-6:
            problems.append(f"'{title}' no longer gives the right answers after compiling")
    if problems:
        raise CheckFailed("Transpile check failed: " + "; ".join(problems))
    return isa


def write_circuits_txt(circuits, isa, backend_name: str, pair: tuple[int, int], gate: str,
                       path: Path = CIRCUITS_TXT) -> None:
    a, b = pair
    parts = [
        f"The four numbers circuits on {backend_name}, qubit A = physical qubit {a}, "
        f"qubit B = physical qubit {b}. Written {utc_now()}.",
        "Results are written A first, then B. In the drawings, q_0 is A and q_1 is B.",
        "",
    ]
    for (_, title, _, _), circuit, isa_circuit in zip(EXPERIMENTS, circuits, isa):
        parts += [
            f"=== {title} ===",
            "As written:",
            str(circuit.draw(output="text")),
            "",
            f"As the hardware runs it (Rz is a free frame change, sqrt(X) is a real microwave pulse, "
            f"{gate.upper()} is a real two-qubit pulse):",
            str(isa_circuit.draw(output="text", idle_wires=False)),
            "",
        ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(parts), encoding="utf-8")


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
    return ask_yes("  Submit anyway? [y/N]: ")


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
        "experiment": "Why do two qubits need four numbers? (Quantum, Explained: Quantum Basics #3)",
        "data": "Raw counts from IBM quantum hardware. No error mitigation, no post-selection.",
        "heads_tails": "heads = 0, tails = 1",
        "result_order": "A first, then B: '10' means A gave tails and B gave heads.",
        "backend": meta["backend"],
        "physical_qubits": meta["physical_qubits"],
        "two_qubit_gate": meta["two_qubit_gate"],
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
        properties = job.properties()
        at_run = {role: readout_details(properties, qubit) for role, qubit in meta["physical_qubits"].items()}
        if any(at_run.values()):
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
    qubits = record["physical_qubits"]
    gate = record.get("two_qubit_gate", "cz").upper()
    log("")
    log("=" * 84)
    log(f" RESULT  {record['backend']}, qubits A = {qubits['A']} and B = {qubits['B']}, "
        f"{record['shots_per_experiment']:,} runs each, no error mitigation")
    log("=" * 84)
    log(" Each result is A first, then B. Heads = 0, tails = 1.")
    log(f" {'':<25}" + "".join(f"{ab:>7} " for ab in OUTCOMES) + f"{'agree':>9} ")
    for key in MAIN_FOUR:
        entry = summary[key]
        cells = "".join(f"{100 * entry['p'][ab]:7.1f}%" for ab in OUTCOMES)
        log(f" {TITLES[key]:<25}{cells}{100 * entry['p_agree']:9.1f}%")
    log("-" * 84)
    log(" The dial: H, H, turn the weight of 11 by the angle, H on B, look")
    for key, _, _, deg in EXPERIMENTS:
        if deg is None:
            continue
        entry = summary[key]
        log(f"   {deg:3d} deg   A and B agree in {100 * entry['p_agree']:5.1f}% of runs   "
            f"(theory {100 * entry['theory_p_agree']:5.1f}%)")
    dial = summary["_dial"]
    log(f" Largest gap from theory: {100 * dial['largest_gap_from_theory']:.1f} points. "
        f"Fitted swing from bottom to top: {100 * dial['fit']['swing']:.1f} points (theory 50).")
    log("-" * 84)
    at_submit = record.get("calibration_at_submit") or {}
    if "readout_error" in at_submit:
        readout = at_submit["readout_error"]
        log(f" Readout error: A {100 * readout['A']:.2f}%, B {100 * readout['B']:.2f}% "
            f"(device median {100 * at_submit['device_median_readout_error']:.2f}%).")
        log(f" {gate} pulse error between them: {100 * at_submit['two_qubit_gate_error']:.2f}% "
            f"(device median {100 * at_submit['device_median_two_qubit_gate_error']:.2f}%).")
    charged = qpu_seconds(record)
    if charged is not None:
        log(f" QPU time charged: {charged:.1f} s")
    log(f" Saved: {rel(HW_JSON)}, {rel(HW_PNG)}, {rel(CIRCUITS_TXT)}")
    log("=" * 84)


def run_date(record: dict) -> str:
    """The day the quantum computer ran the job, in this PC's time zone."""
    stamps = record.get("ibm_timestamps") or {}
    for value in (stamps.get("finished"), stamps.get("running"), record.get("collected_utc"),
                  record.get("submitted_utc")):
        if not value:
            continue
        try:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone().strftime("%Y-%m-%d")
    return ""


def make_plot(summary: dict, record: dict, path: Path, simulated: bool = False) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch

    plt.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.sans-serif": ["Segoe UI", "Helvetica Neue", "Arial", "DejaVu Sans"],
            "font.size": 11,
        }
    )
    agree_color, differ_color = "#2a78d6", "#eb6834"
    ink, ink_2, muted, grid_color, axis_color = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"

    fig = plt.figure(figsize=(12, 9), dpi=200)
    layout = fig.add_gridspec(2, 4, left=0.07, right=0.985, top=0.80, bottom=0.09,
                              height_ratios=[1, 1.08], hspace=0.62, wspace=0.12)
    bar_axes = [fig.add_subplot(layout[0, i]) for i in range(4)]
    curve = fig.add_subplot(layout[1, :])

    # Top: the four named experiments, one small chart each, results A first
    steps = {
        "toss_both": "H, H, look",
        "sign_look": "H, H, CZ, look",
        "toss_b_again": "H, H, H on B, look",
        "sign_toss_b": "H, H, CZ, H on B, look",
    }
    x = np.arange(len(OUTCOMES))
    for i, (ax, key) in enumerate(zip(bar_axes, MAIN_FOUR)):
        entry = summary[key]
        values = [100 * entry["p"][ab] for ab in OUTCOMES]
        theory = [100 * entry["theory_p"][ab] for ab in OUTCOMES]
        low = [max(0.0, v - 100 * entry["ci95"][ab][0]) for v, ab in zip(values, OUTCOMES)]
        high = [max(0.0, 100 * entry["ci95"][ab][1] - v) for v, ab in zip(values, OUTCOMES)]
        colors = [agree_color if ab in ("00", "11") else differ_color for ab in OUTCOMES]
        ax.bar(x, values, width=0.62, color=colors, zorder=2)
        ax.errorbar(x, values, yerr=[low, high], fmt="none", ecolor=ink_2, elinewidth=0.9, capsize=3, zorder=3)
        ax.hlines(theory, x - 0.38, x + 0.38, colors=ink, linewidth=1.6, zorder=4)
        for xi, v, t, h in zip(x, values, theory, high):
            ax.text(xi, max(v + h, t) + 2.0, f"{v:.1f}%", ha="center", va="bottom", fontsize=8.5, color=ink)
        ax.set_xticks(x)
        ax.set_xticklabels(OUTCOMES, fontsize=10.5)
        ax.set_xlim(-0.6, 3.6)
        ax.set_ylim(0, 64)
        ax.set_yticks([0, 25, 50])
        ax.set_yticklabels(["0%", "25%", "50%"] if i == 0 else [])
        ax.text(0.0, 1.15, TITLES[key], transform=ax.transAxes, ha="left", va="bottom",
                fontsize=11.5, fontweight="bold", color=ink)
        ax.text(0.0, 1.06, steps[key], transform=ax.transAxes, ha="left", va="bottom",
                fontsize=9.5, color=ink_2)
    bar_axes[0].set_ylabel("Share of runs", color=ink_2)

    # Bottom: the dial, how often A and B agree, measured points on the theory curve
    dial = [(entry["dial_degrees"], entry) for key, entry in summary.items()
            if isinstance(entry, dict) and "dial_degrees" in entry]
    dial.sort(key=lambda item: item[0])
    fine = np.linspace(0, 360, 361)
    curve.plot(fine, 100 * (0.75 - np.cos(np.radians(fine)) / 4), color=muted, linewidth=2, zorder=1,
               label="theory")
    degs = [deg for deg, _ in dial]
    measured = [100 * entry["p_agree"] for _, entry in dial]
    low = [max(0.0, m - 100 * entry["ci95_agree"][0]) for m, (_, entry) in zip(measured, dial)]
    high = [max(0.0, 100 * entry["ci95_agree"][1] - m) for m, (_, entry) in zip(measured, dial)]
    curve.errorbar(degs, measured, yerr=[low, high], fmt="none", ecolor=ink_2, elinewidth=0.9, capsize=3, zorder=2)
    curve.plot(degs, measured, "o", color=agree_color, markersize=8, zorder=3,
               markeredgecolor="white", markeredgewidth=1.5, label="measured")
    curve.set_xticks(range(0, 361, 45))
    curve.set_xticklabels([f"{d}°" for d in range(0, 361, 45)])
    curve.set_xlim(-10, 370)
    curve.set_ylim(40, 106)
    curve.set_yticks([50, 75, 100])
    curve.set_yticklabels(["50%", "75%", "100%"])
    curve.set_ylabel("Runs where A and B agree", color=ink_2)
    curve.set_xlabel("Dial setting: how far the weight of 11 is turned", color=ink_2)
    curve.legend(loc="lower center", frameon=False, fontsize=9.5, ncol=2)
    if 180 in degs:
        curve.annotate("180° = CZ", xy=(180, measured[degs.index(180)] - 1.5), xytext=(180, 84),
                       ha="center", va="top", fontsize=9.5, color=ink_2,
                       arrowprops={"arrowstyle": "-", "color": muted, "linewidth": 0.8})
    curve.text(0.0, 1.10, "The dial", transform=curve.transAxes, ha="left", va="bottom",
               fontsize=11.5, fontweight="bold", color=ink)
    curve.text(0.0, 1.03, "H, H, turn the weight of 11 by the angle, H on B, look", transform=curve.transAxes,
               ha="left", va="bottom", fontsize=9.5, color=ink_2)

    for ax in bar_axes + [curve]:
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(axis_color)
        ax.tick_params(colors=ink_2, length=0)
        ax.yaxis.grid(True, color=grid_color, linewidth=0.8)
        ax.set_axisbelow(True)

    if simulated:
        title, title_color = "DRY RUN: ideal simulation, not hardware", "#B91C1C"
        subtitle = (f"Noiseless simulator, {SHOTS:,} runs per circuit. "
                    "Checks the code before any hardware time is used.")
    else:
        title, title_color = "One sign on 11 makes A and B agree", ink
        qubits = record["physical_qubits"]
        subtitle = (f"{record['backend']}, qubits A = {qubits['A']} and B = {qubits['B']}, "
                    f"{record['shots_per_experiment']:,} runs per circuit, {run_date(record)}. "
                    "Raw counts, no error mitigation.")
    fig.text(0.07, 0.955, title, ha="left", va="bottom", fontsize=16, fontweight="bold", color=title_color)
    fig.text(0.07, 0.928, subtitle, ha="left", va="bottom", fontsize=10, color=ink_2)
    handles = [
        Patch(color=agree_color, label="A and B agree (00 or 11)"),
        Patch(color=differ_color, label="A and B differ (01 or 10)"),
        Line2D([0], [0], color=ink, linewidth=1.6, label="theory"),
    ]
    fig.legend(handles=handles, loc="lower left", bbox_to_anchor=(0.064, 0.872), ncol=3, frameon=False,
               fontsize=9.5, handlelength=1.4, columnspacing=1.6)
    fig.text(0.07, 0.012, "Each result is A first, then B. Heads = 0, tails = 1. "
             "Error bars: 95% confidence intervals. CZ is the dial at 180°.",
             ha="left", va="bottom", fontsize=8.5, color=muted)

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
    parser = argparse.ArgumentParser(description="Why do two qubits need four numbers? On IBM quantum hardware.")
    parser.add_argument("--dry-run", action="store_true",
                        help="run every check, send nothing to the quantum computer")
    parser.add_argument("--backend", help="IBM machine to use, e.g. ibm_fez (default: least busy)")
    parser.add_argument("--qubits", type=int, nargs=2, metavar=("A", "B"),
                        help="preferred connected pair, used if it beats the chip's median today")
    parser.add_argument("--login", action="store_true", help="enter and save a new IBM Quantum API key")
    parser.add_argument("--new", action="store_true", help="fresh hardware run; archive older results")
    parser.add_argument("--yes", action="store_true",
                        help="submit without asking, even after a warning or a switch of machine")
    args = parser.parse_args(argv)
    if args.qubits and args.qubits[0] == args.qubits[1]:
        parser.error("--qubits needs two different qubits")

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
    for key in MAIN_FOUR:
        cells = "  ".join(f"{ab} {100 * ideal[key]['p'][ab]:4.1f}%" for ab in OUTCOMES)
        log(f"  {TITLES[key]:<25} {cells}")
    log(f"  ...and {len(DIAL_DEGREES)} dial settings from 0 to 360 deg. All match theory.")

    log("Step 2/4  Connecting to IBM Quantum")
    service = connect_service(force_login=args.login)

    if JOB_FILE.exists() and not args.dry_run:
        meta = json.loads(JOB_FILE.read_text(encoding="utf-8"))
        log(f"  Found the job you already submitted ({meta['job_id']} on {meta['backend']}).")
        log("  Collecting that one instead of submitting a second job.")
        return finish(find_job(service, meta), meta)

    # A dry run spends nothing, so it may switch machines without asking
    backend = choose_backend(service, args.backend, confirm=not (args.dry_run or args.yes))
    # A preferred pair only makes sense on the machine it was chosen for
    wanted = tuple(args.qubits) if args.qubits and (args.backend is None or backend.name == args.backend) else None
    pair, calibration = pick_pair(backend, wanted)
    gate = calibration["two_qubit_gate"]
    readout = calibration["readout_error"]
    log(f"  Qubits A = {pair[0]} and B = {pair[1]}: readout errors {100 * readout['A']:.2f}% and "
        f"{100 * readout['B']:.2f}% (device median {100 * calibration['device_median_readout_error']:.2f}%)")
    log(f"  {gate.upper()} pulse error between them {100 * calibration['two_qubit_gate_error']:.2f}% "
        f"(device median {100 * calibration['device_median_two_qubit_gate_error']:.2f}%)")
    isa = transpile_and_check(backend, circuits, pair)
    # A dry run keeps its drawings apart, so they never mix with a hardware run's files
    drawings = DRY_DIR / "circuits.txt" if args.dry_run else CIRCUITS_TXT
    write_circuits_txt(circuits, isa, backend.name, pair, gate, drawings)
    log(f"  Transpile check passed: B's two H gates stay separate, every sign is a real {gate.upper()}")
    log("  pulse, and every compiled circuit still gives the right answers on paper.")
    if args.dry_run:
        log(f"Dry run passed. Nothing was sent to the quantum computer. Drawings: {rel(drawings)}")
        return 0

    log(f"Step 3/4  Submitting {len(circuits)} circuits x {SHOTS:,} runs to {backend.name}")
    if not usage_and_queue_check(service, assume_yes=args.yes):
        log("Not submitted. Nothing was sent to the quantum computer.")
        return 2
    job = submit(backend, isa)
    meta = {
        "job_id": job.job_id(),
        "backend": backend.name,
        "physical_qubits": {"A": pair[0], "B": pair[1]},
        "two_qubit_gate": gate,
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
