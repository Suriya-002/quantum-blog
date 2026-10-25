"""Why do two qubits need four numbers? The short version of the experiment.

    python four_numbers_simple.py          runs on a perfect simulator on your computer
    python four_numbers_simple.py --ibm    runs on a real IBM quantum computer

Install first:  pip install "qiskit>=2.3,<3" "qiskit-ibm-runtime>=0.50,<0.51"
Heads = 0, tails = 1. Qubit A is q0 and qubit B is q1. Every result is printed A first.
"""

import math
import sys

from qiskit import ClassicalRegister, QuantumCircuit, QuantumRegister

SHOTS = 4000


def two_qubits():
    # Qubit 0 is A and qubit 1 is B. Bit 0 records A and bit 1 records B.
    return QuantumCircuit(QuantumRegister(2, "q"), ClassicalRegister(2, "final"))


# Experiment 1: toss both
toss_both = two_qubits()
toss_both.h([0, 1])                 # toss A and B
toss_both.barrier()
toss_both.measure([0, 1], [0, 1])   # look at both

# Experiment 2: sign, then look
sign_look = two_qubits()
sign_look.h([0, 1])
sign_look.barrier()
sign_look.cz(0, 1)                  # a minus sign on the weight of 11, and only 11
sign_look.barrier()                 # stops the compiler from deleting the CZ before the look
sign_look.measure([0, 1], [0, 1])

# Experiment 3: toss B again
toss_b = two_qubits()
toss_b.h([0, 1])
toss_b.barrier()                    # stops the compiler from cancelling B's two H gates
toss_b.h(1)
toss_b.barrier()
toss_b.measure([0, 1], [0, 1])

# Experiment 4: sign, then toss B again
sign_toss_b = two_qubits()
sign_toss_b.h([0, 1])                   # toss A and B
sign_toss_b.barrier()
sign_toss_b.cz(0, 1)                    # a minus sign on the weight of 11, and only 11
sign_toss_b.barrier()
sign_toss_b.h(1)                        # toss B again
sign_toss_b.barrier()                   # stops the compiler from deleting anything before the look
sign_toss_b.measure([0, 1], [0, 1])     # look at both

# Experiment 5: the dial, a turn of the weight of 11 by any angle
angles = list(range(0, 361, 30))
dials = []
for angle in angles:
    dial = two_qubits()
    dial.h([0, 1])
    dial.barrier()
    dial.cp(math.radians(angle), 0, 1)  # cp() turns the weight of 11 by the angle
    dial.barrier()
    dial.h(1)
    dial.barrier()
    dial.measure([0, 1], [0, 1])
    dials.append(dial)

circuits = [toss_both, sign_look, toss_b, sign_toss_b] + dials
names = ["Toss both", "Sign, then look", "Toss B again", "Sign, then toss B"] + [f"Dial at {a} deg" for a in angles]

if "--ibm" not in sys.argv:
    # A perfect simulator on your own computer. No account needed.
    from qiskit.primitives import BackendSamplerV2
    from qiskit.providers.basic_provider import BasicSimulator

    sampler = BackendSamplerV2(backend=BasicSimulator(), options={"seed_simulator": 2026})
    result = sampler.run(circuits, shots=SHOTS).result()
else:
    # A real IBM quantum computer.
    from qiskit.transpiler import generate_preset_pass_manager
    from qiskit_ibm_runtime import QiskitRuntimeService, SamplerV2 as Sampler

    # First time only: save the API key from your IBM Quantum dashboard.
    # QiskitRuntimeService.save_account(token="YOUR_API_KEY", set_as_default=True)

    service = QiskitRuntimeService()
    backend = service.least_busy(operational=True, simulator=False)
    print("Running on", backend.name)

    # Pick a connected pair of qubits with small errors, from IBM's latest calibration
    target = backend.target
    gate = next(name for name in ("cz", "ecr", "cx") if name in target.operation_names)

    def misread(qubit):
        props = target["measure"][(qubit,)]
        return props.error if props and props.error is not None else 1.0

    def pair_error(pair):
        return misread(pair[0]) + misread(pair[1]) + 2 * target[gate][pair].error

    pairs = [pair for pair, props in target[gate].items() if props and props.error is not None]
    a, b = min(pairs, key=pair_error)
    print("Using qubits", a, "and", b)

    # Compile the circuits for this chip, on that pair
    pass_manager = generate_preset_pass_manager(optimization_level=2, backend=backend, initial_layout=[a, b])
    chip_circuits = pass_manager.run(circuits)

    # Raw hardware: keep IBM's optional noise-reduction tricks off (off is the default)
    sampler = Sampler(mode=backend)
    sampler.options.dynamical_decoupling.enable = False
    sampler.options.twirling.enable_gates = False
    sampler.options.twirling.enable_measure = False

    job = sampler.run(chip_circuits, shots=SHOTS)
    print("Job ID:", job.job_id(), "(waiting for the result)")
    result = job.result()

print(f"{'':<18}{'00':>7}{'01':>7}{'10':>7}{'11':>7}{'agree':>8}")
for name, run in zip(names, result):
    counts = run.data.final.get_counts()
    # Qiskit writes B first, so each result is flipped to put A first
    share = {ab: counts.get(ab[::-1], 0) / SHOTS for ab in ("00", "01", "10", "11")}
    cells = "".join(f"{share[ab]:7.1%}" for ab in share)
    print(f"{name:<18}{cells}{share['00'] + share['11']:8.1%}")
