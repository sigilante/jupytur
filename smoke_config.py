"""Drive the JupyturKernel's %config magic without going through ZMQ.

We construct the kernel with no env vars set so it starts unconnected,
then invoke do_execute with a %config cell, then with normal code cells.
A tiny stub captures send_response so we can see what would have been
streamed to the notebook frontend.
"""

import os
import sys

# Clear any env vars so we exercise the unconnected path.
for k in ["JUPYTUR_URL", "JUPYTUR_CODE", "JUPYTUR_SHIP", "JUPYTUR_AGENT", "JUPYTUR_SESSION"]:
    os.environ.pop(k, None)

from jupytur.kernel import JupyturKernel


class _StubSocket:
    pass


class HeadlessKernel(JupyturKernel):
    """Skip the ZMQ Kernel.__init__ but reuse the JupyturKernel logic."""

    iopub_socket = _StubSocket()
    execution_count = 0

    def __init__(self):
        import queue, threading, requests
        self._q = queue.Queue()
        self._id_lock = threading.Lock()
        self._poke_id = 0

        # Same defaults as JupyturKernel.__init__ but with env all cleared.
        self._url = "http://localhost:8080"
        self._code = None
        self._ship = None
        self._agent = "dojo"
        self._session = f"jupyter-{os.getpid()}"
        self._channel = f"jupytur-smoke-{os.getpid()}"
        self._sub_id = None
        self._http = requests.Session()
        self._sse_thread = None
        self._connected = False
        self._init_error = None

        from jupytur.kernel import SoleClock
        self._clock = SoleClock()

    def send_response(self, socket, msg_type, content):
        name = content.get("name", "?")
        text = content.get("text", "").rstrip("\n")
        print(f"   [{name}] {text}")


def run(k, code, label):
    print(f"\n>> {label}: {code!r}")
    print(f"   qsize_before={k._q.qsize()} connected={k._connected}"
          f"  agent={k._agent} session={k._session}"
          f"  clock=(own={k._clock.own},his={k._clock.his})")
    k.execution_count += 1
    result = k.do_execute(code, silent=False)
    print(f"   qsize_after={k._q.qsize()}"
          f"  clock=(own={k._clock.own},his={k._clock.his})")
    print(f"   status={result['status']}")
    return result


def main():
    # Connection details come from env so this script can run against any
    # ship.  All five vars must be set.
    required = {
        "url":   os.environ.get("SMOKE_URL", "http://localhost:8080"),
        "ship":  os.environ.get("SMOKE_SHIP"),
        "code":  os.environ.get("SMOKE_CODE"),
        "agent": os.environ.get("SMOKE_AGENT", "dojo"),
        "alt_agent":   os.environ.get("SMOKE_ALT_AGENT", "north"),
        "alt_session": os.environ.get("SMOKE_ALT_SESSION", "jupytur-north-config"),
    }
    missing = [k for k in ("ship", "code") if not required[k]]
    if missing:
        sys.stderr.write(
            "smoke_config.py: set the ship and code env, e.g.:\n"
            "    SMOKE_SHIP=nec SMOKE_CODE=ropnys-batwyd-nossyt-mapwet "
            "SMOKE_URL=http://localhost:80 python smoke_config.py\n"
        )
        sys.exit(2)

    k = HeadlessKernel()

    # 1) Before config, normal cell should yield ConfigError hint.
    run(k, "(add 2 2)", "pre-config eval")

    # 2) %config to wire up the primary agent.
    cfg = (
        f"%config url={required['url']} ship={required['ship']}"
        f" code={required['code']} agent={required['agent']}"
    )
    run(k, cfg, "config")

    # 3) Now real cells should work.
    run(k, "(add 2 2)", f"{required['agent']} eval after config")
    run(k, "=x 7", "set var")
    run(k, "(mul x 6)", "use var")

    # 4) Switch agents mid-notebook.
    run(
        k,
        f"%config agent={required['alt_agent']} session={required['alt_session']}",
        "switch agent",
    )
    run(k, "2 3 + .", f"{required['alt_agent']} eval after switch")

    # 5) Bad token should error without changing connection.
    run(k, "%config nope=foo", "bad config")
    run(k, "10 20 +", f"still on {required['alt_agent']}")

    try:
        k._unsubscribe()
    except Exception:
        pass


if __name__ == "__main__":
    main()
