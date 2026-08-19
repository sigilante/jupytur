"""
Jupytur — Jupyter kernel backed by a live Urbit ship.

Each kernel instance opens one persistent Eyre channel, subscribes to a
dedicated %sole session (/sole/~ship/session-name) on a chosen agent, and
drives it by sending %sole-action pokes that mirror what a terminal would
do: set the prompt to the cell's text with [%det %set ...], then submit
with [%ret ~].  Results arrive as %sole-effect SSE events on the same
channel; a trailing %sole-effect %pro signals that the command has finished.

This kernel intentionally tracks the sole vector clock locally (see
+$sole-share in pkg/base-dev/sur/sole.hoon and ++receive / ++transmit in
pkg/base-dev/lib/sole.hoon).  The agent rejects any %det whose clock does
not match its view, so we maintain own/his counters per session.

Configuration:
  The kernel can be configured two ways. Either set environment variables
  before launching Jupyter, or put a %config cell at the top of the
  notebook. %config takes precedence and can be re-run to switch agents
  or sessions mid-notebook.

  %config example (notebook cell):
    %config url=http://localhost:80 ship=nec code=ropnys-...-mapwet agent=dojo

  Environment variables:
    JUPYTUR_URL      HTTP URL of the ship (default: http://localhost:8080)
    JUPYTUR_CODE     Web login code (get with +code in Dojo)
    JUPYTUR_SHIP     Ship name without ~, e.g. zod or sampel-palnet
    JUPYTUR_AGENT    Gall agent to connect to (default: dojo).
                     Any /lib/shoe-built agent qualifies.
    JUPYTUR_SESSION  Session name (default: auto-generated from PID).
                     Must be a valid @ta cord.

  If JUPYTUR_SHIP and JUPYTUR_CODE are set in the environment the kernel
  auto-connects at boot. Otherwise it waits for a %config cell — the first
  code cell will error with a hint until the kernel is connected.

Session discovery:
  On connect, the kernel scries /x/sole/sessions (urbit/urbit#7379) to
  check whether the chosen session name is already live on the agent,
  and picks a fresh one instead of colliding with it. This matters
  because the default session name is PID-based, and PIDs are only
  unique per machine. A %sessions cell lists everything currently live
  on the agent.

  This only works against agents that actually route through
  /lib/shoe's on-peek wrapper (e.g. %north, or a custom shoe agent) —
  %dojo's on-peek (pkg/arvo/app/dojo.hoon) is a hand-rolled stub that
  never delegates to shoe, so it has no session data to give regardless
  of #7379. Against %dojo (or any ship predating #7379), the scry comes
  back empty-handed and the kernel just skips the check silently — no
  error, no collision protection either.

  Also: on agents that don't override shoe's default on-leave (the
  example /app/shoe.hoon included), a session that unsubscribed cleanly
  still shows up in this list — shoe's default on-leave doesn't prune
  `soles`. Treat %sessions as "has been used," not "is currently open."
"""

import json
import os
import queue
import sys
import threading
import time

import requests
from ipykernel.kernelbase import Kernel


class SoleClock:
    """Mirror of +$sole-share, tracking the vector clock for one session.

    Field names follow the Hoon source (own.ven / his.ven) from the
    *client's* point of view: 'own' is our own clock, 'his' is our view
    of the agent's clock.
    """

    def __init__(self):
        self.own = 0
        self.his = 0
        self.buf = []  # list of unicode codepoints

    def make_change(self, edit):
        """Mirror of ++transmit: build the wire-format sole-change for our edit.

        sole-clock encodes as JSON array [own, his].  When we transmit, we
        send our pair as ler=[his.ven, own.ven] (see ++transmit at
        pkg/base-dev/lib/sole.hoon:122).  We omit haw; the mark defaults
        it to 0v0, which the receiver accepts (see ++receive at line 105).
        """
        change = {
            "ler": [self.his, self.own],
            "ted": _edit_to_json(edit),
        }
        self._commit(edit)
        return change

    def apply_remote(self, change):
        """Mirror of ++receive: apply an incoming sole-change from the agent.

        We do the minimal accounting needed to stay in sync: bump his
        and update buf.  We don't validate clocks (the agent already did
        before sending) and we don't transmute through pending edits
        (the Jupytur kernel is the only writer, so leg is always empty).
        """
        ted = change.get("ted", {})
        self._apply_edit(ted)
        self.his += 1

    def _commit(self, edit):
        self._apply_edit(edit)
        self.own += 1

    def _apply_edit(self, edit):
        # edit is the JSON shape: {"set": "..."} | {"del": N} | {"ins": {...}}
        #                       | {"mor": [...]} | "nop"
        if edit == "nop" or edit is None:
            return
        if not isinstance(edit, dict):
            return
        if "set" in edit:
            self.buf = [ord(c) for c in edit["set"]]
        elif "del" in edit:
            pos = edit["del"]
            if 0 <= pos < len(self.buf):
                del self.buf[pos]
        elif "ins" in edit:
            pos = edit["ins"]["at"]
            cha = edit["ins"]["cha"]
            self.buf.insert(pos, ord(cha) if isinstance(cha, str) else cha)
        elif "mor" in edit:
            for sub in edit["mor"]:
                self._apply_edit(sub)


def _edit_to_json(edit):
    """Translate a Python edit tuple into the JSON shape sole/action expects.

    Only %set is used by this kernel, but the helper accepts the common
    shapes for completeness.
    """
    kind = edit[0]
    if kind == "set":
        text = edit[1]
        if isinstance(text, list):
            text = "".join(chr(c) for c in text)
        return {"set": text}
    if kind == "nop":
        return "nop"
    if kind == "del":
        return {"del": edit[1]}
    if kind == "ins":
        return {"ins": {"at": edit[1], "cha": chr(edit[2]) if isinstance(edit[2], int) else edit[2]}}
    raise ValueError(f"unsupported sole-edit kind: {kind}")


# Per-language metadata. The kernelspec passes JUPYTUR_LANGUAGE in its env
# (see jupytur/install.py); the kernel reads it at boot to pick the right
# block.  Default is hoon for back-compat with the original Dojo workflow.
_LANGUAGES = {
    "hoon": {
        "language_version": "140",
        "language_info": {
            "name": "hoon",
            "mimetype": "text/x-hoon",
            "file_extension": ".hoon",
            "codemirror_mode": "hoon",
        },
        "banner": "Jupytur — Hoon notebook backed by a live Urbit ship",
    },
    "forth": {
        "language_version": "ANSI-94",
        "language_info": {
            "name": "forth",
            "mimetype": "text/x-forth",
            "file_extension": ".fs",
            "codemirror_mode": "forth",
        },
        "banner": "Jupytur — Forth (North) notebook backed by a live Urbit ship",
    },
}


def _lang_meta():
    key = os.environ.get("JUPYTUR_LANGUAGE", "hoon").lower()
    return _LANGUAGES.get(key, _LANGUAGES["hoon"])


class JupyturKernel(Kernel):
    implementation = "jupytur"
    implementation_version = "0.3.0"
    language = os.environ.get("JUPYTUR_LANGUAGE", "hoon").lower()
    language_version = _lang_meta()["language_version"]
    language_info = _lang_meta()["language_info"]
    banner = _lang_meta()["banner"]
    help_links = [
        {"text": "Jupytur on GitHub", "url": "https://github.com/sigilante/jupytur"},
    ]

    # ------------------------------------------------------------------ setup

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        self._q = queue.Queue()
        self._id_lock = threading.Lock()
        self._poke_id = 0

        # Configuration — populated by %config or fallback env vars. None
        # means "not yet set"; only _url and _agent have safe defaults so
        # a totally empty %config still has something to subscribe to.
        self._url = os.environ.get("JUPYTUR_URL", "http://localhost:8080").rstrip("/")
        self._code = os.environ.get("JUPYTUR_CODE")
        self._ship = os.environ.get("JUPYTUR_SHIP")
        self._agent = os.environ.get("JUPYTUR_AGENT", "dojo")
        self._session = os.environ.get("JUPYTUR_SESSION", f"jupyter-{os.getpid()}")
        self._channel = f"jupytur-{os.getpid()}"
        self._sub_id = None
        self._clock = SoleClock()
        self._http = requests.Session()
        self._sse_thread = None
        self._connected = False

        # If env supplies enough to connect, do it eagerly — keeps the
        # existing JUPYTUR_* workflow working. Otherwise wait for %config.
        if self._code and self._ship:
            try:
                self._connect()
            except Exception as e:
                # Don't crash the kernel; surface the error on first cell.
                self._init_error = f"auto-connect failed: {e}"
            else:
                self._init_error = None
        else:
            self._init_error = None

    def _connect(self):
        """Log in, subscribe to /sole on a fresh channel, then start the SSE reader.

        Subscribe must happen before the SSE GET, otherwise Eyre returns
        404 (the channel doesn't exist until at least one action is PUT
        on it). The SSE GET replays buffered events from event-id 0, so
        we won't miss the subscribe ACK or the initial %pro.

        Each call uses a fresh channel ID. Reusing a channel across
        reconnects is fragile — Eyre may close the stream when the last
        subscription is removed, which crashes any active SSE reader.

        After the subscription is established, run JUPYTUR_INIT_COMMAND
        (if set) silently. This is how variants ship per-agent setup —
        e.g., the North variant clears the Forth stack on every boot.
        """
        if not self._code or not self._ship:
            raise RuntimeError(
                "missing config: need at least ship and code"
                " (set via %config or JUPYTUR_SHIP / JUPYTUR_CODE env)"
            )
        self._login(self._code)
        self._avoid_session_collision()
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass
        self._channel = f"jupytur-{os.getpid()}-{self._next_id()}"
        self._poke_id = 0
        self._clock = SoleClock()
        self._subscribe()
        self._sse_thread = threading.Thread(target=self._sse_reader, daemon=True)
        self._sse_thread.start()
        self._collect_until_pro(timeout=15)
        self._connected = True

        init = os.environ.get("JUPYTUR_INIT_COMMAND", "").strip()
        if init:
            self._send_command(init)
            self._collect_until_pro()

    def _login(self, code):
        r = self._http.post(
            f"{self._url}/~/login",
            data={"password": code},
            allow_redirects=False,
        )
        r.raise_for_status()

    def _scry_sessions(self):
        """List sole sessions ever opened on the current agent.

        Reads /x/sole/sessions (urbit/urbit#7379, not yet merged). Any
        non-200 — 404/500 for a ship predating #7379, or for an agent
        like %dojo whose on-peek doesn't route through shoe at all — is
        treated as "unknown", not an error, since this is an optional
        nicety the kernel doesn't depend on to function.
        """
        try:
            r = self._http.get(
                f"{self._url}/~/scry/{self._agent}/sole/sessions.json",
                timeout=5,
            )
        except requests.RequestException:
            return None
        if r.status_code != 200:
            return None
        try:
            entries = r.json()
        except ValueError:
            return None
        return {(e["ship"], e["session"]) for e in entries}

    def _avoid_session_collision(self):
        """Rename self._session if it's already live on this agent.

        JUPYTUR_SESSION defaults to a PID-based name, and PIDs are only
        unique per machine — two notebooks on two different hosts (or
        containers) can easily pick the same name and end up sharing one
        sole vector-clock on the ship. Sidestep that by checking first.
        """
        sessions = self._scry_sessions()
        if sessions is None:
            return
        want = (f"~{self._ship}", self._session)
        if want not in sessions:
            return
        original = self._session
        suffix = 2
        while (f"~{self._ship}", f"{original}-{suffix}") in sessions:
            suffix += 1
        self._session = f"{original}-{suffix}"
        sys.stderr.write(
            f"jupytur: session '{original}' already active on "
            f"~{self._ship}/%{self._agent}; using '{self._session}' instead\n"
        )

    # ------------------------------------------------------------------ ids

    def _next_id(self):
        with self._id_lock:
            self._poke_id += 1
            return self._poke_id

    def _sole_id(self):
        return {"who": f"~{self._ship}", "ses": self._session}

    # ------------------------------------------------------------------ channel API

    def _put(self, actions):
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

    def _poke_sole(self, dat):
        self._put([{
            "id": self._next_id(),
            "action": "poke",
            "ship": self._ship,
            "app": self._agent,
            "mark": "sole-action",
            "json": {"id": self._sole_id(), "dat": dat},
        }])

    def _send_command(self, src):
        # Replace the prompt buffer wholesale, then press return.
        change = self._clock.make_change(("set", src))
        self._poke_sole({"det": change})
        self._poke_sole("ret")

    # ------------------------------------------------------------------ SSE reader

    def _sse_reader(self):
        url = f"{self._url}/~/channel/{self._channel}"
        with self._http.get(url, stream=True, timeout=None) as resp:
            if resp.status_code != 200:
                sys.stderr.write(
                    f"jupytur: SSE GET {url} returned HTTP {resp.status_code}; "
                    f"reader exiting. The channel may not have been created yet — "
                    f"ensure subscribe is PUT before opening SSE.\n"
                )
                return
            event_id = None
            data_buf = None
            for line in resp.iter_lines(decode_unicode=True):
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
          {"det": {...}}        — agent-side buffer change (apply to local clock)
          [effect, ...]         — %mor: multiple effects
        """
        lines = []
        had_parse_error = False
        is_pro = False

        if isinstance(effect_json, list):
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
                had_parse_error = True
            elif "det" in effect_json:
                self._clock.apply_remote(effect_json["det"])

        return lines, had_parse_error, is_pro

    # ------------------------------------------------------------------ collect loop

    # Idle-after-activity window. Dojo signals completion with %pro, which
    # exits the loop immediately. Shoe agents emit no %pro — they just
    # stop producing effects when the command is done. After the first
    # effect of a response, if the agent goes quiet for this many seconds
    # we treat the command as finished.
    IDLE_TIMEOUT = 0.5

    def _collect_until_pro(self, timeout=30):
        lines = []
        had_error = False
        deadline = time.monotonic() + timeout
        last_event = None  # None until we see the first effect for this command

        while time.monotonic() < deadline:
            try:
                item = self._q.get(timeout=0.1)
            except queue.Empty:
                if last_event is not None and (
                    time.monotonic() - last_event >= self.IDLE_TIMEOUT
                ):
                    break
                continue

            event_id = item.get("event_id")
            if event_id is not None:
                self._ack(event_id)

            data = item["data"]
            resp_type = data.get("response")

            if resp_type == "diff":
                last_event = time.monotonic()
                effect = data.get("json")
                if effect is not None:
                    sub_lines, sub_err, is_pro = self._flatten_effects(effect)
                    lines.extend(sub_lines)
                    had_error = had_error or sub_err
                    if is_pro:
                        break

            elif resp_type == "poke":
                if data.get("ok") is False:
                    err_msg = data.get("err", "poke failed (unknown reason)")
                    lines.append(f"{self._agent}: {err_msg}")
                    had_error = True
                    break

            elif resp_type == "quit":
                if data.get("subscription") != self._sub_id:
                    continue
                lines.append(
                    f"{self._agent}: subscription to /{self._session} was kicked"
                    " (check can-connect, or that the session exists)"
                )
                had_error = True
                self._sub_id = None
                break

        return "\n".join(lines), had_error

    # ------------------------------------------------------------------ magic

    _CONFIG_KEYS = {"url", "code", "ship", "agent", "session"}

    def _handle_config(self, args):
        """%config url=... code=... ship=... agent=... session=...

        Tokenized as whitespace-separated key=value pairs.  Any key may be
        omitted; missing keys keep their current value.  After applying
        the new values we tear down any existing subscription and (re)connect.
        """
        updates = {}
        bad = []
        for tok in args.split():
            if "=" not in tok:
                bad.append(tok)
                continue
            k, v = tok.split("=", 1)
            k = k.strip().lower()
            v = v.strip().strip('"').strip("'")
            if k not in self._CONFIG_KEYS:
                bad.append(tok)
                continue
            updates[k] = v

        if bad:
            return self._magic_error(
                f"unknown config tokens: {' '.join(bad)}. "
                f"valid keys: {', '.join(sorted(self._CONFIG_KEYS))}"
            )

        if "url" in updates:
            self._url = updates["url"].rstrip("/")
        if "code" in updates:
            self._code = updates["code"]
        if "ship" in updates:
            self._ship = updates["ship"]
        if "agent" in updates:
            self._agent = updates["agent"]
        if "session" in updates:
            self._session = updates["session"]

        # Tear down any active subscription before swapping connection.
        if self._connected:
            try:
                self._unsubscribe()
            except Exception:
                pass
            self._connected = False

        try:
            self._connect()
        except Exception as e:
            return self._magic_error(f"connect failed: {e}")

        text = (
            f"connected: ~{self._ship}/%{self._agent}"
            f" session={self._session} url={self._url}\n"
        )
        self.send_response(
            self.iopub_socket, "stream", {"name": "stdout", "text": text}
        )
        return self._magic_ok()

    def _handle_sessions(self):
        """%sessions — list active sole sessions on the current agent.

        Surfaces urbit/urbit#7379's /x/sole/sessions scry directly, so
        users can see stale sessions from crashed kernels or collisions
        with other notebooks/dojo without leaving the notebook.
        """
        sessions = self._scry_sessions()
        if sessions is None:
            text = (
                f"session listing unavailable on ~{self._ship}/%{self._agent}"
                " (needs urbit/urbit#7379, and an agent whose on-peek routes"
                " through /lib/shoe — %dojo's does not)\n"
            )
            self.send_response(
                self.iopub_socket, "stream", {"name": "stderr", "text": text}
            )
            return self._magic_ok()

        mine = (f"~{self._ship}", self._session)
        if not sessions:
            text = f"no active sessions on ~{self._ship}/%{self._agent}\n"
        else:
            lines = [
                f"  {ship}/{ses}" + ("  (this session)" if (ship, ses) == mine else "")
                for ship, ses in sorted(sessions)
            ]
            text = f"active sessions on %{self._agent}:\n" + "\n".join(lines) + "\n"
        self.send_response(
            self.iopub_socket, "stream", {"name": "stdout", "text": text}
        )
        return self._magic_ok()

    def _magic_ok(self):
        return {
            "status": "ok",
            "execution_count": self.execution_count,
            "payload": [],
            "user_expressions": {},
        }

    def _magic_error(self, msg):
        self.send_response(
            self.iopub_socket, "stream", {"name": "stderr", "text": msg + "\n"}
        )
        return {
            "status": "error",
            "execution_count": self.execution_count,
            "ename": "ConfigError",
            "evalue": msg,
            "traceback": [msg],
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
            return self._magic_ok()

        if code.startswith("%config"):
            return self._handle_config(code[len("%config"):].strip())

        if code.startswith("%sessions"):
            if not self._connected:
                return self._magic_error(
                    "kernel is not connected. run %config first"
                )
            return self._handle_sessions()

        if not self._connected:
            hint = self._init_error or (
                "kernel is not connected. "
                "run a config cell, e.g.:  "
                f"%config url=http://localhost:8080 ship=<ship> code=<+code>"
                f" agent={self._agent}"
            )
            return self._magic_error(hint)

        self._send_command(code)
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
        return {"status": "complete"}

    def do_shutdown(self, restart):
        self._unsubscribe()
        return {"status": "ok", "restart": restart}


if __name__ == "__main__":
    from ipykernel.kernelapp import IPKernelApp
    IPKernelApp.launch_instance(kernel_class=JupyturKernel)
