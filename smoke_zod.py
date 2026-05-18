"""Drive the Jupytur kernel against ~zod/%dojo and report what comes back.

Like smoke_nec.py but defaults to the standard fakezod setup
(http://localhost:8080, ship=zod, code=lidlut-tabwed-pillex-ridrup).
Boot one with:

    urbit -F zod

Then run this script.  Override any default via env, e.g.:

    JUPYTUR_URL=http://localhost:8082 python smoke_zod.py

We don't go through ipykernel's ZMQ frontend; we instantiate just the
pieces of JupyturKernel that talk to Eyre (login, subscribe, send_command,
collect) and print every effect we observe.  Useful for protocol-level
debugging without launching a full notebook.
"""

import os
import sys
import time

os.environ.setdefault("JUPYTUR_URL", "http://localhost:8080")
os.environ.setdefault("JUPYTUR_CODE", "lidlut-tabwed-pillex-ridrup")
os.environ.setdefault("JUPYTUR_SHIP", "zod")
os.environ.setdefault("JUPYTUR_AGENT", "dojo")
os.environ.setdefault("JUPYTUR_SESSION", f"jupytur-smoke-{os.getpid()}")

from jupytur.kernel import JupyturKernel, SoleClock


class HeadlessKernel(JupyturKernel):
    """Skip ipykernel's full init; we just want the Eyre client wiring."""

    def __init__(self):
        import queue, threading, requests
        self._q = queue.Queue()
        self._id_lock = threading.Lock()
        self._poke_id = 0

        self._url = os.environ["JUPYTUR_URL"].rstrip("/")
        self._ship = os.environ["JUPYTUR_SHIP"]
        self._channel = f"jupytur-smoke-{os.getpid()}"
        self._agent = os.environ["JUPYTUR_AGENT"]
        self._session = os.environ["JUPYTUR_SESSION"]
        self._sub_id = None
        self._clock = SoleClock()

        self._http = requests.Session()
        self._login(os.environ["JUPYTUR_CODE"])

        print(f">> subscribing to /sole/~{self._ship}/{self._session} on %{self._agent}")
        # PUT subscribe before opening SSE GET — Eyre 404s on empty channels.
        self._subscribe()
        t = threading.Thread(target=self._sse_reader, daemon=True)
        t.start()
        self._sse_thread = t
        out, err = self._collect_until_pro(timeout=15)
        print(f"   initial pro received | err={err} | text={out!r}")
        print(f"   clock after init: own={self._clock.own} his={self._clock.his}")

        # Run JUPYTUR_INIT_COMMAND if set — matches production _connect path.
        init = os.environ.get("JUPYTUR_INIT_COMMAND", "").strip()
        if init:
            print(f">> running init command: {init!r}")
            self._send_command(init)
            iout, _ = self._collect_until_pro()
            print(f"   init reply: {iout!r}")


def run_cell(k, src, timeout=15):
    print(f"\n>> cell: {src!r}")
    t0 = time.monotonic()
    k._send_command(src)
    out, err = k._collect_until_pro(timeout=timeout)
    elapsed = (time.monotonic() - t0) * 1000
    print(f"   text: {out!r}   ({elapsed:.0f}ms, err={err})")
    print(f"   clock: own={k._clock.own} his={k._clock.his}")
    return out, err


def main():
    k = HeadlessKernel()
    try:
        # Standard Hoon arithmetic and dojo conveniences.
        run_cell(k, "(add 2 2)")
        run_cell(k, "=x 42")
        run_cell(k, "x")
        run_cell(k, "(mul x 3)")
        run_cell(k, "(weld \"hello \" \"world\")")
        # Type info — a typical dojo workflow.
        run_cell(k, "? (add 2 2)")
    finally:
        try:
            k._unsubscribe()
        except Exception as e:
            print(f"!! unsubscribe failed: {e}")


if __name__ == "__main__":
    main()
