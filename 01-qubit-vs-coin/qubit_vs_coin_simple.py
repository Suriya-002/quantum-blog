"""Qubit vs coin, the short version of the experiment.

    python qubit_vs_coin_simple.py          runs on a perfect simulator on your computer
    python qubit_vs_coin_simple.py --ibm    runs on a real IBM quantum computer

Install first:  pip install "qiskit>=2.3,<3" "qiskit-ibm-runtime>=0.50,<0.51"
Heads = 0, tails = 1.
"""

import sys

from qiskit import ClassicalRegister, QuantumCircuit, QuantumRegister

SHOTS = 4000

# Experiment 1: toss once
toss_once = QuantumCircuit(QuantumRegister(1, "q"), ClassicalRegister(1, "final"))
toss_once.h(0)                  # the toss
toss_once.measure(0, 0)         # look at the qubit

# Experiment 2: toss twice
toss_twice = QuantumCircuit(QuantumRegister(1, "q"), ClassicalRegister(1, "final"))
toss_twice.h(0)
toss_twice.barrier()            # stops the compiler from deleting the two H gates
toss_twice.h(0)
toss_twice.measure(0, 0)

# Experiment 3: toss, peek, toss
peek, final = ClassicalRegister(1, "peek"), ClassicalRegister(1, "final")
toss_peek = QuantumCircuit(QuantumRegister(1, "q"), peek, final)
toss_peek.h(0)
toss_peek.measure(0, peek[0])   # the peek
toss_peek.barrier()
toss_peek.h(0)
toss_peek.measure(0, final[0])  # the final answer

circuits = [toss_once, toss_twice, toss_peek]
names = ["Toss once", "Toss twice", "Toss, peek, toss"]

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

    # Pick the qubit that misreads least often, from IBM's latest calibration.
    # Left alone, the compiler would put a one-qubit circuit on qubit 0.
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
    print(f"{name:<18} heads {counts.get('0', 0) / SHOTS:6.1%}")
