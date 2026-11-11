from __future__ import annotations

import json
import socket
import sys
from dataclasses import dataclass
from typing import Any, BinaryIO

from sbud.endpoint import default_endpoint
from sbud.errors import SbudError


@dataclass(frozen=True)
class ServiceInfo:
	api_version: int
	version: str
	pid: int
	endpoint: str
	started_at: str


class SbudClient:
	"""Talks to the sbud service over its named pipe (Windows) or Unix socket (macOS, Linux)."""

	def __init__(self, endpoint: str | None = None) -> None:
		self.endpoint = endpoint or default_endpoint()

	def service(self) -> ServiceInfo:
		"""GET /v1/service: the running service's API version, version, process and endpoint."""
		body = self._request("GET", "/v1/service")
		return ServiceInfo(
			api_version=body["apiVersion"],
			version=body["version"],
			pid=body["pid"],
			endpoint=body["endpoint"],
			started_at=body["startedAt"],
		)

	def _request(self, method: str, path: str) -> Any:
		request = f"{method} {path} HTTP/1.1\r\nHost: sbud\r\nAccept: application/json\r\nConnection: close\r\n\r\n".encode("ascii")
		try:
			raw = self._exchange(request)
		except OSError as error:
			raise SbudError("unavailable", f"The sbud service is not answering at {self.endpoint}.") from error
		status, _headers, body = _parse_response(raw)
		data = json.loads(body) if body else None
		if status >= 400:
			problem = data if isinstance(data, dict) else {}
			raise SbudError(str(problem.get("kind", "failed")), str(problem.get("title", f"HTTP {status}")), status)
		return data

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
