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


def _serve_raw(path: str, response: bytes, seen: list[bytes]) -> threading.Thread:
	server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
	server.bind(path)
	server.listen(1)

	def run() -> None:
		conn, _ = server.accept()
		with conn:
			request = b""
			while b"\r\n\r\n" not in request:
				request += conn.recv(4096)
			head, _, body = request.partition(b"\r\n\r\n")
			length = next((int(line.split(b":")[1]) for line in head.split(b"\r\n") if line.lower().startswith(b"content-length:")), 0)
			while len(body) < length:
				body += conn.recv(4096)
			seen.append(head + b"\r\n\r\n" + body)
			conn.sendall(response)
		server.close()

	thread = threading.Thread(target=run)
	thread.start()
	return thread


def test_a_change_goes_as_a_json_patch_with_its_query(sock_path: str) -> None:
	seen: list[bytes] = []
	payload = json.dumps({"name": "a", "changes": []}).encode()
	thread = _serve_raw(sock_path, b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s" % (len(payload), payload), seen)
	plan = SbudClient(sock_path).change_sandbox(
		"a", [{"op": "replace", "path": "/ramGiB", "value": 8}], mode="clone", dry_run=True, confirm_stops=["a", "b"]
	)
	thread.join()
	assert plan == {"name": "a", "changes": []}
	request = seen[0]
	assert request.startswith(b"PATCH /v1/sandboxes/a?mode=clone&dryRun=true&confirmStops=a%2Cb HTTP/1.1\r\n")
	assert b"Content-Type: application/json-patch+json\r\n" in request
	assert request.endswith(b'[{"op": "replace", "path": "/ramGiB", "value": 8}]')


def test_every_faulty_field_comes_with_the_error(sock_path: str) -> None:
	problem = json.dumps(
		{
			"status": 422,
			"kind": "invalid",
			"title": "Some fields are wrong.",
			"errors": [{"field": "ramGiB", "message": "RAM must be a whole number of GiB."}],
		}
	).encode()
	thread = _serve_raw(sock_path, b"HTTP/1.1 422 Unprocessable Entity\r\nContent-Length: %d\r\n\r\n%s" % (len(problem), problem), [])
	with pytest.raises(SbudError) as caught:
		SbudClient(sock_path).create_sandbox({"name": "x"}, dry_run=True)
	thread.join()
	assert (caught.value.status, caught.value.kind) == (422, "invalid")
	assert caught.value.errors == [{"field": "ramGiB", "message": "RAM must be a whole number of GiB."}]


def test_events_are_read_from_a_chunked_stream_as_they_arrive(sock_path: str) -> None:
	stream = b': connected\n\nevent: task\ndata: {"id": "t1"}\n\n: keep-alive\n\nevent: sandboxes\ndata: {}\n\n'
	chunks = b"".join(b"%x\r\n%s\r\n" % (len(stream[i : i + 7]), stream[i : i + 7]) for i in range(0, len(stream), 7))
	thread = _serve_raw(
		sock_path, b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n" + chunks + b"0\r\n\r\n", []
	)
	events = list(SbudClient(sock_path).events(["task", "sandboxes"]))
	thread.join()
	assert [(e.channel, e.data) for e in events] == [("task", {"id": "t1"}), ("sandboxes", {})]


def test_an_extra_folder_is_named_by_its_path_inside_the_sandbox(sock_path: str) -> None:
	seen: list[bytes] = []
	thread = _serve_raw(sock_path, b"HTTP/1.1 202 Accepted\r\nContent-Length: 2\r\n\r\n{}", seen)
	SbudClient(sock_path).remove_mount("a", "D:\\work\\shared\\")
	thread.join()
	assert seen[0].startswith(b"DELETE /v1/sandboxes/a/mounts/d/work/shared HTTP/1.1\r\n")


def test_a_single_change_carries_the_stops_the_user_agreed_to(sock_path: str) -> None:
	seen: list[bytes] = []
	thread = _serve_raw(sock_path, b"HTTP/1.1 202 Accepted\r\nContent-Length: 2\r\n\r\n{}", seen)
	SbudClient(sock_path).set_mount_read_only("a", "D:\\work\\shared", True, confirm_stops=["a", "b"])
	thread.join()
	assert seen[0].startswith(b"PATCH /v1/sandboxes/a/mounts/d/work/shared?confirmStops=a%2Cb HTTP/1.1\r\n")
	assert seen[0].endswith(b'{"readOnly": true}')
