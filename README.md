# Jupytur — a Jupyter kernel interface for Urbit

![](./img/hero.png)

A Jupyter notebook interface to a running Urbit ship over Eyre SSE.

Drives Dojo or any `/lib/shoe`-built Gall agent through the existing
`%sole-action` protocol — **no Arvo-side changes required**.  Jupytur is
purely a client.

![](./img/gui.png)

## Kernels

`jupytur-install --all` registers two kernels with Jupyter:

| Kernel name      | Display                | Default agent | Language          |
|------------------|------------------------|---------------|-------------------|
| `jupytur`        | Jupytur (Hoon)         | `%dojo`       | Hoon              |
| `jupytur-north`  | Jupytur (North)        | `%north`      | Forth (ANSI-ish)  |

The "Hoon" kernel talks to `%dojo` — the canonical Hoon REPL.  The
"North" kernel talks to [`%north`](https://github.com/sigilante/north) —
a Forth interpreter that runs as a `/lib/shoe` agent.  Adding a kernel
for another shoe agent is one entry in `VARIANTS` in
[`jupytur/install.py`](jupytur/install.py).

## Installation

```sh
git clone https://github.com/sigilante/jupytur.git
cd jupytur
pip install -e .
jupytur-install --all      # or omit --all to install only the Hoon kernel
jupyter notebook
```

Install just one variant: `jupytur-install --variant north`.  List
variants: `jupytur-install --list`.

## Usage

Pick **Jupytur (Hoon)** or **Jupytur (North)** from the New Notebook
menu, then configure the connection in the first cell.

### Example: Hoon against a local `~zod`

```
%config url=http://localhost:8080 ship=zod code=lidlut-tabwed-pillex-ridrup agent=dojo
```

```hoon
(add 2 2)
```
→ `4`

```hoon
=x 42
(mul x 3)
```
→ `126`

### Example: Forth against `~nec` running `%north`

Use the North kernel.  `%north` must be installed on the ship; see the
[North repo](https://github.com/sigilante/north) for installation.

```
%config url=http://localhost:80 ship=nec code=ropnys-batwyd-nossyt-mapwet agent=north
```

```forth
2 3 + .                 \ prints  5
: SQUARE DUP * ;
5 SQUARE .              \ prints  25
SON                     \ stack display on
10 20 +                 \ ok  ~[30]
```

The Forth kernel auto-runs `: SCLR BEGIN DEPTH WHILE DROP REPEAT ; SCLR`
on every connect so the data stack starts empty (North maintains stack
state globally on the agent across sole sessions).

## Configuration

There are two ways to point the kernel at a ship.

### 1. `%config` cell (per notebook)

```
%config url=http://localhost:8080 ship=<ship> code=<+code> agent=<agent>
```

All keys are optional; missing keys keep their current values.  Valid keys:

| Key       | Meaning                                                    |
|-----------|------------------------------------------------------------|
| `url`     | HTTP URL of the ship (e.g. `http://localhost:8080`)         |
| `ship`    | Ship name without `~` (e.g. `zod`, `sampel-palnet`)        |
| `code`    | Web login code, from `+code` in dojo                       |
| `agent`   | Gall agent to talk to (e.g. `dojo`, `north`)               |
| `session` | Sole session name (default: auto-generated per kernel boot) |

Re-running `%config` mid-notebook tears down the current subscription
and reconnects — useful for switching agents or pointing at a different
ship.  Note that Jupyter sets syntax highlighting at kernel-launch time;
swapping the agent via `%config` does not switch the highlighter.

### 2. Environment variables (shared across notebooks)

```sh
export JUPYTUR_URL=http://localhost:8080
export JUPYTUR_CODE=<+code output>
export JUPYTUR_SHIP=zod
jupyter notebook
```

If both `JUPYTUR_SHIP` and `JUPYTUR_CODE` are set, the kernel
auto-connects at boot — no `%config` cell required.  Otherwise the
first code cell errors with a hint until you run `%config`.

The default `JUPYTUR_AGENT` is determined by the kernel variant
(`dojo` for "Jupytur (Hoon)", `north` for "Jupytur (North)") and can be
overridden in either the environment or via `%config`.

## Architecture notes

- **Protocol.** The kernel sends two `%sole-action` pokes per cell: one
  `[%det %set <src>]` to load the prompt buffer, then `[%ret ~]` to
  submit.  Effects stream back as `%sole-effect` facts.
- **Vector clock.** `SoleClock` in `jupytur/kernel.py` mirrors
  `+$sole-share` from `pkg/base-dev/sur/sole.hoon`; `++receive` in
  `lib/sole.hoon` rejects any `%det` whose clock doesn't match.
- **Completion sentinel.** Dojo emits `%pro` after each command.  Shoe
  agents (including North) don't — the kernel falls back to a 500ms
  idle-after-activity timeout in that case.
- **Channel ordering.** Eyre returns 404 on `GET /~/channel/<id>` if the
  channel hasn't been created yet, which silently kills the SSE reader.
  The kernel PUTs `subscribe` first, then opens the SSE GET; Eyre
  replays events from event-id 0.
