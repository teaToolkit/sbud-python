from __future__ import annotations


class SbudError(Exception):
	"""A request the service refused or could not answer.

	`kind` is the problem kind the service sent, such as `not-found`, or `unavailable` when
	the service did not answer at all. `status` is the HTTP status, or None when there was none.
	"""

	def __init__(self, kind: str, title: str, status: int | None = None) -> None:
		super().__init__(title)
		self.kind = kind
		self.title = title
		self.status = status
