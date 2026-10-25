# quantum-blog

This is the code for my blog series on quantum computing. I explain one idea at a time, starting from zero. When I can, I test the idea on a real IBM quantum computer. Every test I run ends up here, with my raw results.

## Which posts have code?

| Folder | Post | The question it tests |
|---|---|---|
| [01-qubit-vs-coin](01-qubit-vs-coin) | What a coin can't do | Toss a qubit twice. Does it act like a coin? |
| [02-the-minus-sign](02-the-minus-sign) | The minus sign you can't see | Put one step you can't see between two tosses. Does it change the answer? |
| [03-four-numbers](03-four-numbers) | Why do two qubits need four numbers? | Put a minus sign on a pair of qubits. Does a second toss make them agree? |

Each folder has its own README with my results and the steps to run it yourself.

## How do I set it up?

You need Python 3.10 or newer. Open a terminal in this folder and make a fresh environment for the libraries.

On Windows, in PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

On a Mac or Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

That installs three libraries. `qiskit` builds quantum circuits and simulates them. `qiskit-ibm-runtime` sends them to IBM's real machines. `matplotlib` draws the charts.

If Windows says running scripts is disabled, run this once and try again:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

## Do I need an IBM account?

Not to start. Each post has a version that runs on a simulator on your own computer. For a real quantum computer, you need a free IBM Quantum account. Each folder shows how to save your key safely.

## Can I reuse the code?

Yes. It's under the MIT License. Keep the license file with any copy you share.
