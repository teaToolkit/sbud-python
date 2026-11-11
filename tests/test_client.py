from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
from collections.abc import Iterator

import pytest

from sbud import SbudClient, SbudError


def _serve_once(path: str, status: str, body: dict[str, object], chunked: bool) -> threading.Thread:
	server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
	server.bind(path)
	server.listen(1)

	def run() -> None:
		conn, _ = server.accept()
		with conn:
			request = b""
			while b"\r\n\r\n" not in request:
				request += conn.recv(4096)
			assert request.startswith(b"GET /v1/service HTTP/1.1\r\n")
			payload = json.dumps(body).encode()
			if chunked:
				head = f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n".encode()
				conn.sendall(head + f"{len(payload):x}\r\n".encode() + payload + b"\r\n0\r\n\r\n")
			else:
				head = f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {len(payload)}\r\n\r\n".encode()
				conn.sendall(head + payload)
		server.close()

	thread = threading.Thread(target=run)
	thread.start()
	return thread


@pytest.fixture
def sock_path() -> Iterator[str]:
	with tempfile.TemporaryDirectory() as folder:
		yield os.path.join(folder, "sbud.sock")


INFO = {"apiVersion": 1, "version": "0.1.0", "pid": 4242, "endpoint": "/run/user/1000/sbud.sock", "startedAt": "2026-11-11T10:00:00Z"}


@pytest.mark.parametrize("chunked", [False, True])
def test_service_reads_service_info(sock_path: str, chunked: bool) -> None:
	thread = _serve_once(sock_path, "200 OK", INFO, chunked)
	info = SbudClient(sock_path).service()
	thread.join()
	assert (info.api_version, info.version, info.pid, info.started_at) == (1, "0.1.0", 4242, "2026-11-11T10:00:00Z")


def test_problem_details_become_sbud_error(sock_path: str) -> None:
	thread = _serve_once(
		sock_path, "401 Unauthorized", {"status": 401, "kind": "unauthenticated", "title": "Not the service's user."}, False
	)
	with pytest.raises(SbudError) as caught:
		SbudClient(sock_path).service()
	thread.join()
	assert (caught.value.kind, caught.value.status, caught.value.title) == ("unauthenticated", 401, "Not the service's user.")


def test_no_service_is_unavailable(sock_path: str) -> None:
	with pytest.raises(SbudError) as caught:
		SbudClient(sock_path).service()
	assert caught.value.kind == "unavailable"
	assert caught.value.status is None


def test_sbud_socket_overrides_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.setenv("SBUD_SOCKET", "/tmp/elsewhere.sock")
	assert SbudClient().endpoint == "/tmp/elsewhere.sock"
