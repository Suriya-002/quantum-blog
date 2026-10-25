# My raw results

These are the files my full script saved from my run on ibm_fez, 24 October 2026. I copied them here exactly as the script wrote them.

| File | What's inside |
|---|---|
| `four_numbers_hardware.png` | The chart my script drew from the run. |
| `hardware_results.json` | All the numbers. The next section shows how to read them. |
| `job.json` | What the script saved when it sent the job: the job ID, the machine, the two qubits and IBM's calibration. |
| `circuits.txt` | Each circuit two ways: as I wrote it, and as the chip really ran it. |
| `dryrun/ideal_simulation.png` | The practice run on a perfect simulator, before any hardware time was used. |

## How do I read hardware_results.json?

Look under `results`. There's one entry per circuit: `toss_both`, `sign_look`, `toss_b_again`, `sign_toss_b`, and `dial_000` to `dial_360` for the 13 dial settings. Every result is written A first, then B.

- `counts` says how many of the 4,000 runs gave each result: `"00"`, `"01"`, `"10"` and `"11"`.
- `p` is the share of each result, from 0 to 1.
- `ci95` is the 95% range around each share. It's the error bars on the chart.
- `theory_p` is what a perfect machine would give, and `largest_gap_from_theory` is the biggest difference.
- `p_agree` is the share of runs where A and B agreed, 00 or 11. It comes with `ci95_agree` and `theory_p_agree`.
- `p_a_heads` and `p_b_heads` look at each qubit on its own.

The `_dial` entry sums up the dial. It has the largest gap from theory, and a fitted wave whose `swing` would be 0.5 on a perfect machine.

The rest describes the setup:

- `physical_qubits` says which qubits on the chip played A and B.
- The `calibration` entries hold IBM's error rates for both qubits and for the CZ pulse between them, around the time of the run.
- `transpiled_gate_counts` lists the gates the chip really ran.
- `ibm_usage` shows the QPU time charged.
- `software` lists the Python and Qiskit versions I used.

## Did you clean up the noise?

No. I kept IBM's noise-reduction options off on purpose. These are the hardware's raw answers, noise and all.
