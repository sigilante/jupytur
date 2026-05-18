"""Drive the Jupytur kernel against ~nec/%north and report what comes back.

We don't go through ipykernel's ZMQ frontend; we instantiate just the pieces of
JupyturKernel that talk to Eyre (login, subscribe, send_command, collect) and
print every effect we observe. This validates the vector-clock plumbing in
SoleClock and the end-to-end %sole-action round trip.
"""

import os
import sys
import time

os.environ.setdefault("JUPYTUR_URL", "http://localhost:8080")
os.environ.setdefault("JUPYTUR_SHIP", "nec")
os.environ.setdefault("JUPYTUR_AGENT", "north")
os.environ.setdefault("JUPYTUR_SESSION", f"jupytur-smoke-{os.getpid()}")

if not os.environ.get("JUPYTUR_CODE"):
    sys.stderr.write(
        "smoke_nec.py: JUPYTUR_CODE not set. Export the ship's +code first, e.g.\n"
        "    export JUPYTUR_CODE=ropnys-batwyd-nossyt-mapwet\n"
    )
    sys.exit(2)

# Import after env so __init__ picks them up.
from jupytur.kernel import JupyturKernel, SoleClock


class HeadlessKernel(JupyturKernel):
    """Skip ipykernel's full init; we just want the Eyre client wiring."""

    def __init__(self):
        # Don't call Kernel.__init__ — it expects a ZMQ session. Mimic just
        # the attributes JupyturKernel.__init__ sets after super().__init__.
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
        print(f"   clock after init: own={self._clock.own} his={self._clock.his} buf={self._clock.buf!r}")

        # Run JUPYTUR_INIT_COMMAND if set — matches production _connect path.
        init = os.environ.get("JUPYTUR_INIT_COMMAND", "").strip()
        if init:
            print(f">> running init command: {init!r}")
            self._send_command(init)
            iout, _ = self._collect_until_pro()
            print(f"   init reply: {iout!r}")


def run_cell(k, src, timeout=15):
    print(f"\n>> cell: {src!r}")
    print(f"   pre-clock: own={k._clock.own} his={k._clock.his}")
    k._send_command(src)
    print(f"   post-send clock: own={k._clock.own} his={k._clock.his}")
    out, err = k._collect_until_pro(timeout=timeout)
    print(f"   pro received | err={err}")
    print(f"   text: {out!r}")
    print(f"   post-recv clock: own={k._clock.own} his={k._clock.his}")
    return out, err


def main():
    k = HeadlessKernel()

    # Patch the SSE reader so we can also see the raw effects, not just the
    # flattened text. The smoke output for ~nec/%dojo proved the flow; for
    # %north we want to know exactly what's coming back over the wire.
    orig_flatten = k._flatten_effects

    def loud_flatten(effect_json):
        print(f"     raw effect: {effect_json!r}")
        return orig_flatten(effect_json)

    k._flatten_effects = loud_flatten

    try:
        # Forth syntax — north is a Forth interpreter, not Hoon.
        run_cell(k, "2 3 + .")
        run_cell(k, ": SQUARE DUP * ;")
        run_cell(k, "5 SQUARE .")
        run_cell(k, "SON")
        run_cell(k, "10 20 +")
    finally:
        try:
            k._unsubscribe()
        except Exception as e:
            print(f"!! unsubscribe failed: {e}")


if __name__ == "__main__":
    main()
