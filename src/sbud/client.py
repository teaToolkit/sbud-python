from __future__ import annotations

import json
import socket
import sys
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any, BinaryIO, cast
from urllib.parse import quote, urlencode

from sbud.endpoint import default_endpoint
from sbud.errors import SbudError


@dataclass(frozen=True)
class ServiceInfo:
	api_version: int
	version: str
	pid: int
	endpoint: str
	started_at: str


@dataclass(frozen=True)
class Event:
	"""One message from the service's event stream: its channel, such as `task` or `sandboxes`, and its data."""

	channel: str
	data: Any


def _q(value: str) -> str:
	return quote(value, safe="")


def _stops(path: str, confirm_stops: Iterable[str]) -> str:
	"""`path` with the running sandboxes the user agreed to stop, if any."""
	stops = ",".join(confirm_stops)
	return f"{path}?{urlencode({'confirmStops': stops})}" if stops else path


def _mount_id(host_path: str) -> str:
	"""The id of an extra folder: its path inside the sandbox without the leading slash. `D:\\work\\x` is `d/work/x`."""
	path = host_path.replace("\\", "/")
	if len(path) >= 2 and path[0].isascii() and path[0].isalpha() and path[1] == ":":
		path = "/" + path[0].lower() + path[2:]
	return path.rstrip("/").lstrip("/")


class SbudClient:
	"""Talks to the sbud service over its named pipe (Windows) or Unix socket (macOS, Linux).

	Each call sends one request and returns the service's JSON answer as Python dictionaries and lists, with the
	API's camelCase names. A refused or failed request raises SbudError.
	"""

	def __init__(self, endpoint: str | None = None) -> None:
		self.endpoint = endpoint or default_endpoint()

	# ---- Service, tasks and this computer ------------------------------------------------------------------------

	def service(self) -> ServiceInfo:
		"""GET /v1/service: the running service's API version, version, process and endpoint."""
		body = self.request("GET", "/v1/service")
		return ServiceInfo(
			api_version=body["apiVersion"],
			version=body["version"],
			pid=body["pid"],
			endpoint=body["endpoint"],
			started_at=body["startedAt"],
		)

	def stop_service(self) -> None:
		"""Stops the service, closing every chat and console."""
		self.request("POST", "/v1/service/stop")

	def tasks(self) -> list[dict[str, Any]]:
		"""Running tasks and those that finished in the last hour."""
		return self._list("GET", "/v1/tasks")

	def task(self, task_id: str) -> dict[str, Any]:
		"""One task, with its progress lines."""
		return self._dict("GET", f"/v1/tasks/{_q(task_id)}")

	def cancel_task(self, task_id: str) -> dict[str, Any]:
		"""Cancels a task of a kind that can be stopped safely."""
		return self._dict("DELETE", f"/v1/tasks/{_q(task_id)}")

	def wait_for_task(self, task: dict[str, Any], interval: float = 0.5, timeout: float | None = None) -> dict[str, Any]:
		"""Polls a task until it has ended, and returns it as it ended."""
		deadline = None if timeout is None else time.monotonic() + timeout
		while task["state"] == "running":
			if deadline is not None and time.monotonic() > deadline:
				raise TimeoutError(f"Task {task['id']} is still running.")
			time.sleep(interval)
			task = self.task(task["id"])
		return task

	def host(self) -> dict[str, Any]:
		"""This computer's CPU and memory use."""
		return self._dict("GET", "/v1/host")

	def events(self, channels: Iterable[str] | None = None) -> Iterator[Event]:
		"""Follows the service's event stream, yielding each event as it comes. Without channels, every channel except `stats`.

		The stream does not reconnect by itself: when it ends, reload what you show and call events() again.
		"""
		path = "/v1/events" + (f"?{urlencode({'channels': ','.join(channels)})}" if channels is not None else "")
		yield from self._stream(path)

	# ---- Settings and defaults -------------------------------------------------------------------------------------

	def settings(self) -> dict[str, Any]:
		"""The app-wide settings: the memory trimmer, the models offered and the storage folder."""
		return self._dict("GET", "/v1/settings")

	def patch_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
		"""Changes some app-wide settings with a JSON merge patch; null removes a key so its default applies."""
		return self._dict("PATCH", "/v1/settings", patch, "application/merge-patch+json")

	def sandbox_defaults(self) -> dict[str, Any]:
		"""The stored defaults for new sandboxes."""
		return self._dict("GET", "/v1/sandbox-defaults")

	def patch_sandbox_defaults(self, patch: dict[str, Any]) -> dict[str, Any]:
		"""Changes some of the stored defaults with a JSON merge patch."""
		return self._dict("PATCH", "/v1/sandbox-defaults", patch, "application/merge-patch+json")

	def defaults_for_folder(self, folder: str | None, agent: str | None = None) -> dict[str, Any]:
		"""What a new sandbox in `folder` would get: a NewSandbox ready to pass to create_sandbox()."""
		query = {k: v for k, v in {"folder": folder, "agent": agent}.items() if v is not None}
		return self._dict("GET", "/v1/sandbox-defaults/for-folder" + (f"?{urlencode(query)}" if query else ""))

	# ---- Sandboxes -------------------------------------------------------------------------------------------------

	def sandboxes(self, workspace: str | None = None, volumes: bool = False, proxy_ca: bool = False) -> list[dict[str, Any]]:
		"""The sandboxes, running first, then by name; `workspace` keeps only that folder's sandboxes."""
		query: dict[str, str] = {}
		if workspace is not None:
			query["workspace"] = workspace
		include = [name for name, wanted in (("volumes", volumes), ("proxy-ca", proxy_ca)) if wanted]
		if include:
			query["include"] = ",".join(include)
		return self._list("GET", "/v1/sandboxes" + (f"?{urlencode(query)}" if query else ""))

	def sandbox(self, name: str) -> dict[str, Any]:
		"""Everything about one sandbox. It never starts a stopped sandbox."""
		return self._dict("GET", f"/v1/sandboxes/{_q(name)}")

	def create_sandbox(self, new_sandbox: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
		"""Starts creating a sandbox and returns its task; with dry_run, returns {"lines": [...]}, the plan, instead."""
		return self._dict("POST", "/v1/sandboxes" + ("?dryRun=true" if dry_run else ""), new_sandbox)

	def change_sandbox(
		self,
		name: str,
		operations: list[dict[str, Any]],
		mode: str = "in-place",
		fresh: bool = False,
		dry_run: bool = False,
		confirm_stops: Iterable[str] = (),
	) -> dict[str, Any]:
		"""Makes changes to a sandbox, given as a JSON Patch. With dry_run, returns the plan; otherwise the task.

		`mode` is in-place, clone or rebuild. `confirm_stops` names the running sandboxes the user agreed to stop.
		"""
		query = {"mode": mode}
		if fresh:
			query["fresh"] = "true"
		if dry_run:
			query["dryRun"] = "true"
		stops = ",".join(confirm_stops)
		if stops:
			query["confirmStops"] = stops
		return self._dict("PATCH", f"/v1/sandboxes/{_q(name)}?{urlencode(query)}", operations, "application/json-patch+json")

	def put_sandbox_kit(self, name: str, kit: str, path: str | None = None, confirm_stops: Iterable[str] = ()) -> dict[str, Any]:
		"""Adds a kit to a sandbox, or reinstalls it at its current version. `path` is for a kit the catalogue does not list.

		`confirm_stops`, here and in the other single-change calls, names the running sandboxes the user agreed to stop.
		"""
		return self._dict("PUT", _stops(f"/v1/sandboxes/{_q(name)}/kits/{_q(kit)}", confirm_stops), {"path": path} if path else None)

	def remove_sandbox_kit(self, name: str, kit: str, confirm_stops: Iterable[str] = ()) -> dict[str, Any]:
		"""Removes a kit from a sandbox."""
		return self._dict("DELETE", _stops(f"/v1/sandboxes/{_q(name)}/kits/{_q(kit)}", confirm_stops))

	def set_workspace(self, name: str, host_path: str, read_only: bool = False, confirm_stops: Iterable[str] = ()) -> dict[str, Any]:
		"""Gives a sandbox a different main workspace."""
		return self._dict(
			"PUT", _stops(f"/v1/sandboxes/{_q(name)}/workspace", confirm_stops), {"hostPath": host_path, "readOnly": read_only}
		)

	def add_mount(self, name: str, host_path: str, read_only: bool = False, confirm_stops: Iterable[str] = ()) -> dict[str, Any]:
		"""Shares an extra folder into a sandbox."""
		return self._dict("POST", _stops(f"/v1/sandboxes/{_q(name)}/mounts", confirm_stops), {"hostPath": host_path, "readOnly": read_only})

	def set_mount_read_only(self, name: str, host_path: str, read_only: bool, confirm_stops: Iterable[str] = ()) -> dict[str, Any]:
		"""Makes an extra folder read-only or writable."""
		return self._dict(
			"PATCH",
			_stops(f"/v1/sandboxes/{_q(name)}/mounts/{_mount_id(host_path)}", confirm_stops),
			{"readOnly": read_only},
			"application/merge-patch+json",
		)

	def remove_mount(self, name: str, host_path: str, confirm_stops: Iterable[str] = ()) -> dict[str, Any]:
		"""Stops sharing an extra folder; the folder itself is untouched."""
		return self._dict("DELETE", _stops(f"/v1/sandboxes/{_q(name)}/mounts/{_mount_id(host_path)}", confirm_stops))

	def delete_sandbox(self, name: str) -> None:
		"""Deletes a sandbox and sbud's records of it, never the folders shared into it."""
		self.request("DELETE", f"/v1/sandboxes/{_q(name)}")

	def start_sandbox(self, name: str) -> None:
		"""Starts a sandbox and the loop that gives its spare memory back to this computer."""
		self.request("POST", f"/v1/sandboxes/{_q(name)}/start")

	def stop_sandbox(self, name: str) -> None:
		"""Stops a sandbox."""
		self.request("POST", f"/v1/sandboxes/{_q(name)}/stop")

	def restart_sandbox(self, name: str) -> None:
		"""Stops a sandbox, then starts it again."""
		self.request("POST", f"/v1/sandboxes/{_q(name)}/restart")

	def open_terminal_session(self, name: str, kind: str = "agent") -> dict[str, Any]:
		"""Gets a sandbox ready before running sbx in a terminal; returns {"id", "argv"}. `kind` is agent or shell."""
		return self._dict("POST", f"/v1/sandboxes/{_q(name)}/terminal-sessions", {"kind": kind})

	def close_terminal_session(self, name: str, session_id: str) -> None:
		"""Ends a terminal session, so the service can keep a Codex login renewed during it."""
		self.request("DELETE", f"/v1/sandboxes/{_q(name)}/terminal-sessions/{_q(session_id)}")

	# ---- Chats -----------------------------------------------------------------------------------------------------

	def chats(self, name: str, saved: bool = False, limit: int | None = None) -> list[dict[str, Any]]:
		"""A sandbox's open chats; with saved, also the conversations its agent saved (the sandbox must be running)."""
		query = {
			k: v
			for k, v in {"include": "saved" if saved else None, "limit": None if limit is None else str(limit)}.items()
			if v is not None
		}
		return self._list("GET", f"/v1/sandboxes/{_q(name)}/chats" + (f"?{urlencode(query)}" if query else ""))

	def open_chat(self, name: str, model: str | None = None, resume: str | None = None) -> dict[str, Any]:
		"""Opens a new chat, or with `resume` continues a saved chat."""
		return self._dict("POST", f"/v1/sandboxes/{_q(name)}/chats", {"model": model, "resume": resume})

	def chat(self, name: str, chat_id: str) -> dict[str, Any]:
		"""One open chat's title, model and whether the agent is working."""
		return self._dict("GET", f"/v1/sandboxes/{_q(name)}/chats/{_q(chat_id)}")

	def chat_items(self, name: str, chat_id: str) -> list[dict[str, Any]]:
		"""Everything a chat has shown so far; follow the `chat` events for what comes next."""
		return self._list("GET", f"/v1/sandboxes/{_q(name)}/chats/{_q(chat_id)}/items")

	def set_chat_model(self, name: str, chat_id: str, model: str | None) -> dict[str, Any]:
		"""Changes the model a chat uses from its next message; None means the sandbox's default."""
		return self._dict("PATCH", f"/v1/sandboxes/{_q(name)}/chats/{_q(chat_id)}", {"model": model}, "application/merge-patch+json")

	def send_chat_message(self, name: str, chat_id: str, text: str) -> None:
		"""Sends a message; the reply arrives as `chat` events."""
		self.request("POST", f"/v1/sandboxes/{_q(name)}/chats/{_q(chat_id)}/messages", {"text": text})

	def interrupt_chat(self, name: str, chat_id: str) -> None:
		"""Stops the agent's current reply and drops waiting messages."""
		self.request("POST", f"/v1/sandboxes/{_q(name)}/chats/{_q(chat_id)}/interrupt")

	def close_chat(self, name: str, chat_id: str) -> None:
		"""Closes a chat; the agent keeps the conversation as a saved chat."""
		self.request("DELETE", f"/v1/sandboxes/{_q(name)}/chats/{_q(chat_id)}")

	# ---- Consoles --------------------------------------------------------------------------------------------------

	def consoles(self, name: str) -> list[dict[str, Any]]:
		"""A sandbox's consoles."""
		return self._list("GET", f"/v1/sandboxes/{_q(name)}/consoles")

	def open_console(self, name: str, rows: int = 24, cols: int = 80) -> dict[str, Any]:
		"""Starts bash in a sandbox. Its terminal is a WebSocket at /v1/sandboxes/{name}/consoles/{id}/terminal."""
		return self._dict("POST", f"/v1/sandboxes/{_q(name)}/consoles", {"rows": rows, "cols": cols})

	def console(self, name: str, console_id: str) -> dict[str, Any]:
		"""One console's title and whether bash is still running."""
		return self._dict("GET", f"/v1/sandboxes/{_q(name)}/consoles/{_q(console_id)}")

	def close_console(self, name: str, console_id: str) -> None:
		"""Ends bash and closes the console."""
		self.request("DELETE", f"/v1/sandboxes/{_q(name)}/consoles/{_q(console_id)}")

	# ---- Kit catalogue ---------------------------------------------------------------------------------------------

	def kits(self) -> list[dict[str, Any]]:
		"""The kit catalogue."""
		return self._list("GET", "/v1/kits")

	def kit(self, name: str) -> dict[str, Any]:
		"""One kit."""
		return self._dict("GET", f"/v1/kits/{_q(name)}")

	def add_kit(self, name: str, path: str, default: bool = False) -> dict[str, Any]:
		"""Adds a kit to the catalogue."""
		return self._dict("POST", "/v1/kits", {"name": name, "path": path, "default": default})

	def patch_kit(self, name: str, patch: dict[str, Any]) -> dict[str, Any]:
		"""Renames a kit, points it at another folder, or changes whether new sandboxes get it."""
		return self._dict("PATCH", f"/v1/kits/{_q(name)}", patch, "application/merge-patch+json")

	def remove_kit(self, name: str) -> None:
		"""Removes a kit from the catalogue, leaving its files and the sandboxes that have it."""
		self.request("DELETE", f"/v1/kits/{_q(name)}")

	# ---- Logins ----------------------------------------------------------------------------------------------------

	def logins(self) -> list[dict[str, Any]]:
		"""The labels, each with its logins, patterns and sandboxes. No token is ever included."""
		return self._list("GET", "/v1/logins")

	def login(self, label: str) -> dict[str, Any]:
		"""One label."""
		return self._dict("GET", f"/v1/logins/{_q(label)}")

	def patch_login(self, label: str, patch: dict[str, Any]) -> dict[str, Any]:
		"""Renames a label with {"label": ...}, or replaces its patterns with {"patterns": [...]}."""
		return self._dict("PATCH", f"/v1/logins/{_q(label)}", patch, "application/merge-patch+json")

	def push_login(self, label: str) -> dict[str, Any]:
		"""Copies a label's logins into every sandbox assigned that label."""
		return self._dict("POST", f"/v1/logins/{_q(label)}/push")

	def delete_login(self, label: str, agent: str) -> None:
		"""Deletes a stored login; sandboxes keep the copy they already have."""
		self.request("DELETE", f"/v1/logins/{_q(label)}/{_q(agent)}")

	def host_login(self, agent: str) -> dict[str, Any]:
		"""Where the login of the agent installed on this computer is, and whether it exists."""
		return self._dict("GET", f"/v1/host-logins/{_q(agent)}")

	def copy_login_from_host(self, label: str, agent: str) -> dict[str, Any]:
		"""Stores the host's own login under a label, then pushes it. Ask the user first: the file belongs to whichever account this computer uses."""
		return self._dict("POST", f"/v1/logins/{_q(label)}/{_q(agent)}/copy-from-host")

	def start_sign_in(self, label: str, agent: str) -> dict[str, Any]:
		"""Starts signing a new login in under a label. Open its `url`; when its state is need-code, send the browser's code."""
		return self._dict("POST", f"/v1/logins/{_q(label)}/{_q(agent)}/sign-ins")

	def sign_in(self, sign_in_id: str) -> dict[str, Any]:
		"""A sign-in's progress, including the address to open."""
		return self._dict("GET", f"/v1/sign-ins/{_q(sign_in_id)}")

	def send_sign_in_code(self, sign_in_id: str, code: str) -> None:
		"""Sends the code the browser showed."""
		self.request("POST", f"/v1/sign-ins/{_q(sign_in_id)}/code", {"code": code})

	def cancel_sign_in(self, sign_in_id: str) -> None:
		"""Cancels a sign-in; the existing login is unchanged."""
		self.request("DELETE", f"/v1/sign-ins/{_q(sign_in_id)}")

	# ---- Maintenance -----------------------------------------------------------------------------------------------

	def diagnostics(self) -> list[dict[str, Any]]:
		"""Problems that need a repair, each with the repair that fixes it, ready to pass to repair()."""
		return self._list("GET", "/v1/diagnostics")

	def repair(self, repair: dict[str, Any]) -> dict[str, Any]:
		"""Runs one repair, such as {"type": "sbx-login"}, and returns its task."""
		return self._dict("POST", "/v1/repairs", repair)

	def openapi(self) -> dict[str, Any]:
		"""The OpenAPI description of the API."""
		return self._dict("GET", "/openapi/v1.json")

	# ---- Transport -------------------------------------------------------------------------------------------------

	def _dict(self, method: str, path: str, body: Any = None, content_type: str = "application/json") -> dict[str, Any]:
		return cast("dict[str, Any]", self.request(method, path, body, content_type))

	def _list(self, method: str, path: str, body: Any = None, content_type: str = "application/json") -> list[dict[str, Any]]:
		return cast("list[dict[str, Any]]", self.request(method, path, body, content_type))

	def request(self, method: str, path: str, body: Any = None, content_type: str = "application/json") -> Any:
		"""Sends one request and returns the parsed JSON answer, or None when there is none."""
		payload = b"" if body is None else json.dumps(body).encode("utf-8")
		head = f"{method} {path} HTTP/1.1\r\nHost: sbud\r\nAccept: application/json\r\nConnection: close\r\n"
		if body is not None:
			head += f"Content-Type: {content_type}\r\n"
		head += f"Content-Length: {len(payload)}\r\n\r\n"
		try:
			raw = self._exchange(head.encode("ascii") + payload)
		except OSError as error:
			raise SbudError("unavailable", f"The sbud service is not answering at {self.endpoint}.") from error
		status, _headers, data = _parse_response(raw)
		parsed = json.loads(data) if data else None
		if status >= 400:
			problem = parsed if isinstance(parsed, dict) else {}
			raise SbudError(
				str(problem.get("kind", "failed")), str(problem.get("title", f"HTTP {status}")), status, problem.get("errors") or []
			)
		return parsed

	def _exchange(self, request: bytes) -> bytes:
		if sys.platform == "win32":
			pipe: BinaryIO
			with open(self.endpoint, "r+b", buffering=0) as pipe:
				pipe.write(request)
				return _read_all(pipe.read)
		with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
			sock.connect(self.endpoint)
			sock.sendall(request)
			return _read_all(lambda n: sock.recv(n))

	def _stream(self, path: str) -> Iterator[Event]:
		request = f"GET {path} HTTP/1.1\r\nHost: sbud\r\nAccept: text/event-stream\r\n\r\n".encode("ascii")
		try:
			if sys.platform == "win32":
				pipe = open(self.endpoint, "r+b", buffering=0)  # noqa: SIM115
				pipe.write(request)
				read = pipe.read
				close = pipe.close
			else:
				sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
				sock.connect(self.endpoint)
				sock.sendall(request)
				read = sock.recv
				close = sock.close
		except OSError as error:
			raise SbudError("unavailable", f"The sbud service is not answering at {self.endpoint}.") from error
		try:
			yield from _events(read)
		finally:
			close()


def _events(read: Any) -> Iterator[Event]:
	"""Parses a chunked text/event-stream response as it arrives."""
	buffer = b""
	while b"\r\n\r\n" not in buffer:
		chunk = read(65536)
		if not chunk:
			return
		buffer += chunk
	head, _, buffer = buffer.partition(b"\r\n\r\n")
	lines = head.decode("iso-8859-1").split("\r\n")
	status = int(lines[0].split(" ", 2)[1])
	if status >= 400:
		raise SbudError("failed", f"The event stream was refused (HTTP {status}).", status)
	chunked = any(line.lower().startswith("transfer-encoding:") and "chunked" in line.lower() for line in lines[1:])
	text = b""
	channel: str = ""
	data: list[str] = []

	def more() -> bool:
		nonlocal buffer, text
		if not chunked:
			chunk = buffer or read(65536)
			buffer = b""
			text += chunk
			return bool(chunk)
		while True:
			if b"\r\n" in buffer:
				size_line, _, rest = buffer.partition(b"\r\n")
				size = int(size_line.split(b";")[0], 16)
				if size == 0:
					return False
				if len(rest) >= size + 2:
					text += rest[:size]
					buffer = rest[size + 2 :]
					return True
			chunk = read(65536)
			if not chunk:
				return False
			buffer += chunk

	while True:
		while b"\n" not in text:
			if not more():
				return
		raw, _, text = text.partition(b"\n")
		line = raw.decode("utf-8").rstrip("\r")
		if line == "":
			if channel and data:
				yield Event(channel, json.loads("\n".join(data)))
			channel, data = "", []
		elif line.startswith("event:"):
			channel = line[len("event:") :].strip()
		elif line.startswith("data:"):
			data.append(line[len("data:") :].lstrip())


def _read_all(read: Any) -> bytes:
	chunks: list[bytes] = []
	while True:
		chunk = read(65536)
		if not chunk:
			return b"".join(chunks)
		chunks.append(chunk)


def _parse_response(raw: bytes) -> tuple[int, dict[str, str], bytes]:
	head, _, body = raw.partition(b"\r\n\r\n")
	lines = head.decode("iso-8859-1").split("\r\n")
	status = int(lines[0].split(" ", 2)[1])
	headers = {}
	for line in lines[1:]:
		name, _, value = line.partition(":")
		headers[name.strip().lower()] = value.strip()
	if headers.get("transfer-encoding", "").lower() == "chunked":
		body = _unchunk(body)
	elif "content-length" in headers:
		body = body[: int(headers["content-length"])]
	return status, headers, body


def _unchunk(data: bytes) -> bytes:
	out = bytearray()
	while data:
		size_line, _, data = data.partition(b"\r\n")
		size = int(size_line.split(b";")[0], 16)
		if size == 0:
			break
		out += data[:size]
		data = data[size + 2 :]
	return bytes(out)
