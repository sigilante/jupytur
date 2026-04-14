"""
Jupytur — Jupyter kernel backed by a live Urbit ship.

Each kernel instance opens one persistent Eyre channel, subscribes to a
dedicated Dojo session (/sole/~ship/session-name), and drives it via
%eval-command pokes.  Results arrive as %sole-effect SSE events on that
channel and are collected until the session emits %pro (the prompt), which
signals that the command has finished.

Required environment variables:
  JUPYTUR_URL   HTTP URL of the ship, e.g. http://localhost:8080
  JUPYTUR_CODE  Web login code (get with +code in Dojo)
  JUPYTUR_SHIP  Ship name without ~, e.g. zod or sampel-palnet
"""

import json
import os
import queue
import threading
import time

import requests
from ipykernel.kernelbase import Kernel


class JupyturKernel(Kernel):
    implementation = "jupytur"
    implementation_version = "0.1.0"
    language = "hoon"
    language_version = "140"
    language_info = {
        "name": "hoon",
        "mimetype": "text/x-hoon",
        "file_extension": ".hoon",
        "codemirror_mode": "hoon",
    }
    banner = "Jupytur — Hoon notebook backed by a live Urbit ship"
    help_links = [
        {"text": "Hoon School", "url": "https://developers.urbit.org/courses/hoon-school"},
        {"text": "Hoon Reference", "url": "https://developers.urbit.org/reference/hoon/overview"},
    ]

    # ------------------------------------------------------------------ setup

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        self._q = queue.Queue()
        self._id_lock = threading.Lock()
        self._poke_id = 0

        url = os.environ.get("JUPYTUR_URL", "http://localhost:8080").rstrip("/")
        code = os.environ.get("JUPYTUR_CODE", "")
        ship = os.environ.get("JUPYTUR_SHIP", "zod")

        self._url = url
        self._ship = ship
        self._channel = f"jupytur-{os.getpid()}"
        self._session = f"jupyter-{os.getpid()}"
        self._sub_id = None

        self._http = requests.Session()
        self._login(code)

        # SSE reader runs for the life of the kernel.
        t = threading.Thread(target=self._sse_reader, daemon=True)
        t.start()

        # Subscribing to /sole creates the Dojo session; the first event is %pro.
        self._subscribe()
        self._collect_until_pro(timeout=15)

    def _login(self, code):
        r = self._http.post(
            f"{self._url}/~/login",
            data={"password": code},
            allow_redirects=False,
        )
        r.raise_for_status()

    # ------------------------------------------------------------------ ids

    def _next_id(self):
        with self._id_lock:
            self._poke_id += 1
            return self._poke_id

    # ------------------------------------------------------------------ channel API

    def _put(self, actions):
        """Submit one or more channel actions via PUT."""
        r = self._http.put(
            f"{self._url}/~/channel/{self._channel}",
            json=actions,
        )
        r.raise_for_status()

    def _subscribe(self):
        sid = self._next_id()
        self._sub_id = sid
        self._put([{
            "id": sid,
            "action": "subscribe",
            "ship": self._ship,
            "app": "dojo",
            "path": f"/sole/~{self._ship}/{self._session}",
        }])

    def _ack(self, event_id):
        self._put([{
            "id": self._next_id(),
            "action": "ack",
            "event-id": event_id,
        }])

    def _eval(self, src):
        self._put([{
            "id": self._next_id(),
            "action": "poke",
            "ship": self._ship,
            "app": "dojo",
            "mark": "eval-command",
            "json": {"ses": self._session, "src": src},
        }])

    # ------------------------------------------------------------------ SSE reader

    def _sse_reader(self):
        """Background thread: parse the Eyre SSE stream into self._q."""
        url = f"{self._url}/~/channel/{self._channel}"
        with self._http.get(url, stream=True, timeout=None) as resp:
            event_id = None
            data_buf = None
            for line in resp.iter_lines(decode_unicode=True):
                # iter_lines yields '' for blank lines (SSE event delimiters).
                if line == "":
                    if data_buf is not None:
                        try:
                            parsed = json.loads(data_buf)
                            self._q.put({"event_id": event_id, "data": parsed})
                        except json.JSONDecodeError:
                            pass
                    event_id = None
                    data_buf = None
                elif line.startswith("id:"):
                    event_id = int(line[3:].strip())
                elif line.startswith("data:"):
                    data_buf = line[5:].strip()

    # ------------------------------------------------------------------ effect parsing

    def _flatten_effects(self, effect_json):
        """
        Recursively flatten a sole-effect JSON value into:
          (output_lines: list[str], had_parse_error: bool, is_pro: bool)

        Sole-effect JSON shapes (from mar/sole/effect.hoon grow/json):
          {"tan": "string"}     — rendered tang output (result or runtime error)
          {"txt": "string"}     — plain text line
          {"pro": {...}}        — command complete (prompt)
          {"hop": N}            — parse error at character N (%err)
          {"act": "bel"|...}    — %bel, %nex, %clr, %bye (ignore)
          {"det": {...}}        — cursor change (ignore)
          [effect, ...]         — %mor: multiple effects
        """
        lines = []
        had_parse_error = False
        is_pro = False

        if isinstance(effect_json, list):
            # %mor — flatten recursively
            for item in effect_json:
                sub_lines, sub_err, sub_pro = self._flatten_effects(item)
                lines.extend(sub_lines)
                had_parse_error = had_parse_error or sub_err
                is_pro = is_pro or sub_pro

        elif isinstance(effect_json, dict):
            if "tan" in effect_json:
                text = effect_json["tan"].strip()
                if text:
                    lines.append(text)
            elif "txt" in effect_json:
                text = effect_json["txt"].strip()
                if text:
                    lines.append(text)
            elif "pro" in effect_json:
                is_pro = True
            elif "hop" in effect_json:
                # %err — character position of parse failure, no text message
                had_parse_error = True

        return lines, had_parse_error, is_pro

    # ------------------------------------------------------------------ collect loop

    def _collect_until_pro(self, timeout=30):
        """
        Drain self._q, ACKing every event, until %pro arrives.
        Returns (output: str, had_error: bool).
        """
        lines = []
        had_error = False
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                item = self._q.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue

            event_id = item.get("event_id")
            if event_id is not None:
                self._ack(event_id)

            data = item["data"]
            resp_type = data.get("response")

            if resp_type == "diff":
                effect = data.get("json")
                if effect is not None:
                    sub_lines, sub_err, is_pro = self._flatten_effects(effect)
                    lines.extend(sub_lines)
                    had_error = had_error or sub_err
                    if is_pro:
                        break

            elif resp_type == "poke":
                # If the poke itself was rejected, stop waiting.
                if data.get("ok") is False:
                    err_msg = data.get("err", "poke failed (unknown reason)")
                    lines.append(f"dojo: {err_msg}")
                    had_error = True
                    break

        return "\n".join(lines), had_error

    # ------------------------------------------------------------------ Jupyter protocol

    def do_execute(
        self,
        code,
        silent,
        store_history=True,
        user_expressions=None,
        allow_stdin=False,
    ):
        code = code.strip()
        if not code:
            return {
                "status": "ok",
                "execution_count": self.execution_count,
                "payload": [],
                "user_expressions": {},
            }

        self._eval(code)
        output, had_error = self._collect_until_pro()

        if not silent:
            if output:
                stream = "stderr" if had_error else "stdout"
                self.send_response(
                    self.iopub_socket,
                    "stream",
                    {"name": stream, "text": output + "\n"},
                )
            elif had_error:
                self.send_response(
                    self.iopub_socket,
                    "stream",
                    {"name": "stderr", "text": "dojo: parse error\n"},
                )

        if had_error:
            return {
                "status": "error",
                "execution_count": self.execution_count,
                "ename": "HoonError",
                "evalue": output or "parse error",
                "traceback": [output or "dojo: parse error"],
            }

        return {
            "status": "ok",
            "execution_count": self.execution_count,
            "payload": [],
            "user_expressions": {},
        }

    def do_is_complete(self, code):
        # Hoon has tall/wide form; full completeness detection requires the
        # Dojo parser.  Return 'complete' and let the user manage multi-line
        # input via cell continuation.
        return {"status": "complete"}

    def do_shutdown(self, restart):
        try:
            if self._sub_id is not None:
                self._put([{
                    "id": self._next_id(),
                    "action": "unsubscribe",
                    "subscription": self._sub_id,
                }])
        except Exception:
            pass
        return {"status": "ok", "restart": restart}


if __name__ == "__main__":
    from ipykernel.kernelapp import IPKernelApp
    IPKernelApp.launch_instance(kernel_class=JupyturKernel)
