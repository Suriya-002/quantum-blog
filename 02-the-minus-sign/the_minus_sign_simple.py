"""The minus sign you can't see, the short version of the experiment.

    python the_minus_sign_simple.py          runs on a perfect simulator on your computer
    python the_minus_sign_simple.py --ibm    runs on a real IBM quantum computer

Install first:  pip install "qiskit>=2.3,<3" "qiskit-ibm-runtime>=0.50,<0.51"
Heads = 0, tails = 1.
"""

import math
import sys

from qiskit import ClassicalRegister, QuantumCircuit, QuantumRegister

SHOTS = 4000


def one_qubit():
    return QuantumCircuit(QuantumRegister(1, "q"), ClassicalRegister(1, "final"))


# Experiment 1: spin, then look
spin_look = one_qubit()
spin_look.h(0)                  # the toss
spin_look.barrier()
spin_look.z(0)                  # the minus sign: half a turn around the poles
spin_look.measure(0, 0)         # look at the qubit

# Experiment 2: spin between two tosses
spin_between = one_qubit()
spin_between.h(0)
spin_between.barrier()          # stops the compiler from merging H, Z, H into one gate
spin_between.z(0)
spin_between.barrier()
spin_between.h(0)
spin_between.measure(0, 0)

# Experiment 3: the dial, a turn around the poles by any angle
angles = list(range(0, 361, 30))
dials = []
for angle in angles:
    dial = one_qubit()
    dial.h(0)
    dial.barrier()
    dial.p(math.radians(angle), 0)  # p() turns the arrow around the poles
    dial.barrier()
    dial.h(0)
    dial.measure(0, 0)
    dials.append(dial)

circuits = [spin_look, spin_between] + dials
names = ["Spin, then look", "Spin between tosses"] + [f"Dial at {a} deg" for a in angles]

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

    # Pick the qubit that misreads least often, from IBM's latest calibration
    readout = backend.target["measure"]

    def misread_rate(qargs):
        props = readout[qargs]
        return props.error if props and props.error is not None else 1.0

    best_qubit = min(readout, key=misread_rate)[0]
    print("Using qubit", best_qubit)

    # Compile the circuits for this chip, on that qubit
    pass_manager = generate_preset_pass_manager(
        optimization_level=1, backend=backend, initial_layout=[best_qubit]
    )
    chip_circuits = pass_manager.run(circuits)

    # Raw hardware: keep IBM's optional noise-reduction tricks off (off is the default)
    sampler = Sampler(mode=backend)
    sampler.options.dynamical_decoupling.enable = False
    sampler.options.twirling.enable_gates = False
    sampler.options.twirling.enable_measure = False

    job = sampler.run(chip_circuits, shots=SHOTS)
    print("Job ID:", job.job_id(), "(waiting for the result)")
    result = job.result()

for name, run in zip(names, result):
    counts = run.data.final.get_counts()
    print(f"{name:<20} heads {counts.get('0', 0) / SHOTS:6.1%}")
