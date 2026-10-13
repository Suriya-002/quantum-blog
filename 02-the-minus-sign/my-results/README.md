# My raw results

These are the files my full script saved from my run on ibm_fez, 11 October 2026. I copied them here exactly as the script wrote them.

| File | What's inside |
|---|---|
| `the_minus_sign_hardware.png` | The chart my script drew from the run. |
| `hardware_results.json` | All the numbers. The next section shows how to read them. |
| `job.json` | What the script saved when it sent the job: the job ID, the machine, the qubit and IBM's calibration. |
| `circuits.txt` | Each circuit two ways: as I wrote it, and as the chip really ran it. |
| `dryrun/ideal_simulation.png` | The practice run on a perfect simulator, before any hardware time was used. |

## How do I read hardware_results.json?

Look under `results`. There's one entry per circuit: `spin_look`, `spin_between`, and `dial_000` to `dial_360` for the 13 dial settings.

- `counts` says how many of the 4,000 runs gave `"0"` (heads) and `"1"` (tails).
- `p_heads` is the share of heads, from 0 to 1.
- `ci95_heads` is the 95% range around `p_heads`. It's the error bars on the chart.
- `theory_p_heads` is what a perfect machine would give, and `gap_from_theory` is the difference.

The `_dial` entry sums up the dial. It has the largest gap from theory, and a fitted wave whose `swing` would be 1 on a perfect machine.

The rest describes the setup:

- The `calibration` entries hold IBM's error rates for qubit 22 around the time of the run.
- `transpiled_gate_counts` lists the gates the chip really ran.
- `ibm_usage` shows the QPU time charged.
- `software` lists the Python and Qiskit versions I used.

## Did you clean up the noise?

No. I kept IBM's noise-reduction options off on purpose. These are the hardware's raw answers, noise and all.
