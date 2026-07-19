from __future__ import annotations

import threading
import time
import uuid
from typing import Callable


TERMINAL_OBSERVATION_STATUSES = frozenset({"completed", "cancelled", "timed_out"})


class ObservationCoordinator:
    """Coordinate one-shot observations without letting publishers consume results."""

    def __init__(self) -> None:
        self._condition = threading.Condition(threading.RLock())
        self._requests: dict[str, dict] = {}

    def register(
        self,
        kind: str,
        *,
        owner: str,
        timeout: float,
        payload: dict | None = None,
        retention: float | None = None,
    ) -> str:
        now = time.monotonic()
        observation_id = uuid.uuid4().hex
        request = {
            "id": observation_id,
            "kind": str(kind),
            "owner": str(owner),
            "status": "pending",
            "created_at": now,
            "deadline": now + max(0.0, float(timeout)),
            "completed_at": None,
            "expires_at": None,
            "retention": max(0.0, float(timeout if retention is None else retention)),
            "payload": payload if isinstance(payload, dict) else {},
            "result": None,
            "reason": None,
        }
        with self._condition:
            self._requests[observation_id] = request
            self._condition.notify_all()
        return observation_id

    @staticmethod
    def _view(request: dict) -> dict:
        return {
            **request,
            "payload": dict(request.get("payload") or {}),
            "result": request.get("result"),
        }

    def _expire_locked(self, request: dict, now: float | None = None) -> bool:
        if request.get("status") != "pending":
            return False
        current = time.monotonic() if now is None else now
        if current < float(request.get("deadline") or current):
            return False
        request["status"] = "timed_out"
        request["completed_at"] = current
        request["expires_at"] = current + float(request.get("retention") or 0)
        request["reason"] = "timeout"
        return True

    def _prune_locked(self, observation_id: str, request: dict, now: float | None = None) -> bool:
        if request.get("status") not in TERMINAL_OBSERVATION_STATUSES:
            return False
        expires_at = request.get("expires_at")
        current = time.monotonic() if now is None else now
        if expires_at is None or current < float(expires_at):
            return False
        self._requests.pop(observation_id, None)
        return True

    def get(self, observation_id: str | None) -> dict | None:
        if not observation_id:
            return None
        with self._condition:
            request = self._requests.get(str(observation_id))
            if request is None:
                return None
            if self._expire_locked(request):
                self._condition.notify_all()
            if self._prune_locked(str(observation_id), request):
                return None
            return self._view(request)

    def active(self, *, kind: str | None = None, owner: str | None = None) -> list[dict]:
        with self._condition:
            changed = False
            result = []
            now = time.monotonic()
            for observation_id, request in list(self._requests.items()):
                changed = self._expire_locked(request, now) or changed
                if self._prune_locked(observation_id, request, now):
                    changed = True
                    continue
                if request.get("status") != "pending":
                    continue
                if kind is not None and request.get("kind") != kind:
                    continue
                if owner is not None and request.get("owner") != owner:
                    continue
                result.append(self._view(request))
            if changed:
                self._condition.notify_all()
            return result

    def update_payload(
        self,
        observation_id: str,
        updater: Callable[[dict], None] | None = None,
        **updates,
    ) -> dict | None:
        with self._condition:
            request = self._requests.get(str(observation_id))
            if request is None:
                return None
            if self._expire_locked(request):
                self._condition.notify_all()
            if request.get("status") != "pending":
                return self._view(request) if request is not None else None
            payload = request.setdefault("payload", {})
            if updater is not None:
                updater(payload)
            payload.update(updates)
            self._condition.notify_all()
            return self._view(request)

    def complete(self, observation_id: str, result: dict | None = None) -> dict | None:
        with self._condition:
            request = self._requests.get(str(observation_id))
            if request is None:
                return None
            if self._expire_locked(request):
                self._condition.notify_all()
            if request.get("status") == "pending":
                request["status"] = "completed"
                request["completed_at"] = time.monotonic()
                request["expires_at"] = request["completed_at"] + float(request.get("retention") or 0)
                request["result"] = result
                self._condition.notify_all()
            return self._view(request)

    def cancel(self, observation_id: str | None, reason: str = "cancelled") -> dict | None:
        if not observation_id:
            return None
        with self._condition:
            request = self._requests.get(str(observation_id))
            if request is None:
                return None
            if request.get("status") == "pending":
                request["status"] = "cancelled"
                request["completed_at"] = time.monotonic()
                request["expires_at"] = request["completed_at"] + float(request.get("retention") or 0)
                request["reason"] = str(reason)
                self._condition.notify_all()
            return self._view(request)

    def cancel_all(
        self,
        reason: str,
        *,
        kind: str | None = None,
        owner: str | None = None,
    ) -> int:
        with self._condition:
            cancelled = 0
            now = time.monotonic()
            for request in self._requests.values():
                if request.get("status") != "pending":
                    continue
                if kind is not None and request.get("kind") != kind:
                    continue
                if owner is not None and request.get("owner") != owner:
                    continue
                request["status"] = "cancelled"
                request["completed_at"] = now
                request["expires_at"] = now + float(request.get("retention") or 0)
                request["reason"] = str(reason)
                cancelled += 1
            if cancelled:
                self._condition.notify_all()
            return cancelled

    def clear_all(self, reason: str) -> int:
        with self._condition:
            count = len(self._requests)
            self._requests.clear()
            if count:
                self._condition.notify_all()
            return count

    def wait(
        self,
        observation_id: str,
        *,
        timeout: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict | None:
        caller_deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        with self._condition:
            while True:
                request = self._requests.get(str(observation_id))
                if request is None:
                    return None
                if self._expire_locked(request):
                    self._condition.notify_all()
                if self._prune_locked(str(observation_id), request):
                    return None
                if request.get("status") in TERMINAL_OBSERVATION_STATUSES:
                    return self._view(request)
                if cancel_event is not None and cancel_event.is_set():
                    return self.cancel(observation_id, "cancelled")
                now = time.monotonic()
                deadlines = [float(request.get("deadline") or now)]
                if caller_deadline is not None:
                    deadlines.append(caller_deadline)
                remaining = min(deadlines) - now
                if remaining <= 0:
                    if caller_deadline is not None and now >= caller_deadline:
                        return self._view(request)
                    continue
                self._condition.wait(timeout=min(remaining, 0.1))

    def consume(self, observation_id: str | None, *, owner: str | None = None) -> dict | None:
        if not observation_id:
            return None
        with self._condition:
            request = self._requests.get(str(observation_id))
            if request is None:
                return None
            if owner is not None and request.get("owner") != owner:
                return None
            if self._expire_locked(request):
                self._condition.notify_all()
            if self._prune_locked(str(observation_id), request):
                return None
            if request.get("status") not in TERMINAL_OBSERVATION_STATUSES:
                return self._view(request)
            return self._view(self._requests.pop(str(observation_id)))
