"""
Jupytur — Jupyter kernel backed by a live Urbit ship.

Each kernel instance opens one persistent Eyre channel, subscribes to a
dedicated %sole session (/sole/~ship/session-name) on a chosen agent, and
drives it via %eval-command pokes.  Results arrive as %sole-effect SSE events
on that channel and are collected until the session emits %pro (the prompt),
which signals that the command has finished.

Required environment variables:
  JUPYTUR_URL    HTTP URL of the ship, e.g. http://localhost:8080
  JUPYTUR_CODE   Web login code (get with +code in Dojo)
  JUPYTUR_SHIP   Ship name without ~, e.g. zod or sampel-palnet

Optional environment variables:
  JUPYTUR_AGENT  Gall agent to connect to (default: dojo).
                 Any agent built with /lib/shoe or /app/dojo qualifies.
  JUPYTUR_SESSION
                 Session name to use (default: auto-generated from PID).
                 Must be a valid @ta cord.

Magic commands (run in a notebook cell):
  %sessions              list sessions open on the current agent
  %sessions <agent>      list sessions open on a different agent
  %connect <agent> <session>
                         switch to a different agent / session
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
        agent = os.environ.get("JUPYTUR_AGENT", "dojo")
        session = os.environ.get("JUPYTUR_SESSION", f"jupyter-{os.getpid()}")

        self._url = url
        self._ship = ship
        self._channel = f"jupytur-{os.getpid()}"
        self._agent = agent
        self._session = session
        self._sub_id = None

        self._http = requests.Session()
        self._login(code)

        # SSE reader runs for the life of the kernel.
        t = threading.Thread(target=self._sse_reader, daemon=True)
        t.start()

        # Subscribing to /sole creates the session; the first event is %pro.
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
            "app": self._agent,
            "path": f"/sole/~{self._ship}/{self._session}",
        }])

    def _unsubscribe(self):
        if self._sub_id is None:
            return
        try:
            self._put([{
                "id": self._next_id(),
                "action": "unsubscribe",
                "subscription": self._sub_id,
            }])
        except Exception:
            pass
        self._sub_id = None

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
            "app": self._agent,
            "mark": "eval-command",
            "json": {"ses": self._session, "src": src},
        }])

    def _scry_sessions(self, agent):
        """Return the list of active session name strings for an agent."""
        r = self._http.get(
            f"{self._url}/~/scry/{agent}/sole/sessions.json",
        )
        r.raise_for_status()
        data = r.json()
        # The scry returns a JSON array of @ta cord strings.
        if isinstance(data, list):
            return data
        return []

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
          {"txt": "string"}     — plain text line (shoe agents use this)
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
                    lines.append(f"{self._agent}: {err_msg}")
                    had_error = True
                    break

        return "\n".join(lines), had_error

    # ------------------------------------------------------------------ magic commands

    def _handle_magic(self, code):
        """Handle %magic commands entered in notebook cells."""
        parts = code.strip().split()
        cmd = parts[0].lower()

        if cmd == "%sessions":
            agent = parts[1] if len(parts) > 1 else self._agent
            try:
                sessions = self._scry_sessions(agent)
                label = "" if not sessions else "\n".join(f"  {s or '$'}" for s in sessions)
                text = f"Agent {agent!r} — active sessions:\n{label or '  (none)'}\n"
                text += f"\nCurrent: agent={self._agent!r} session={self._session!r}\n"
            except Exception as e:
                text = f"Error querying {agent!r}: {e}\n"
            self.send_response(
                self.iopub_socket, "stream", {"name": "stdout", "text": text}
            )
            return {
                "status": "ok",
                "execution_count": self.execution_count,
                "payload": [],
                "user_expressions": {},
            }

        if cmd == "%connect":
            if len(parts) < 3:
                self.send_response(
                    self.iopub_socket,
                    "stream",
                    {"name": "stderr", "text": "Usage: %connect <agent> <session>\n"},
                )
                return {
                    "status": "error",
                    "execution_count": self.execution_count,
                    "ename": "UsageError",
                    "evalue": "Usage: %connect <agent> <session>",
                    "traceback": [],
                }
            new_agent, new_session = parts[1], parts[2]
            self._unsubscribe()
            self._agent = new_agent
            self._session = new_session
            self._subscribe()
            _, had_error = self._collect_until_pro(timeout=15)
            if had_error:
                text = f"Warning: connection to {new_agent!r}/{new_session!r} returned an error.\n"
                self.send_response(
                    self.iopub_socket, "stream", {"name": "stderr", "text": text}
                )
            else:
                text = f"Connected: agent={new_agent!r} session={new_session!r}\n"
                self.send_response(
                    self.iopub_socket, "stream", {"name": "stdout", "text": text}
                )
            return {
                "status": "ok",
                "execution_count": self.execution_count,
                "payload": [],
                "user_expressions": {},
            }

        # Unknown magic command — pass through as a parse error hint.
        self.send_response(
            self.iopub_socket,
            "stream",
            {"name": "stderr", "text": f"Unknown magic command: {cmd}\n"},
        )
        return {
            "status": "error",
            "execution_count": self.execution_count,
            "ename": "UsageError",
            "evalue": f"Unknown magic command: {cmd}",
            "traceback": [],
        }

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

        if code.startswith("%"):
            return self._handle_magic(code)

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
                    {"name": "stderr", "text": f"{self._agent}: parse error\n"},
                )

        if had_error:
            return {
                "status": "error",
                "execution_count": self.execution_count,
                "ename": "HoonError",
                "evalue": output or "parse error",
                "traceback": [output or f"{self._agent}: parse error"],
            }

        return {
            "status": "ok",
            "execution_count": self.execution_count,
            "payload": [],
            "user_expressions": {},
        }

    def do_is_complete(self, code):
        # Hoon has tall/wide form; full completeness detection requires the
        # agent's parser.  Return 'complete' and let the user manage multi-line
        # input via cell continuation.
        return {"status": "complete"}

    def do_shutdown(self, restart):
        self._unsubscribe()
        return {"status": "ok", "restart": restart}


if __name__ == "__main__":
    from ipykernel.kernelapp import IPKernelApp
    IPKernelApp.launch_instance(kernel_class=JupyturKernel)
