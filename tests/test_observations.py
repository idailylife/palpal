import threading
import time

from palpal.observations import ObservationCoordinator


def test_observation_completion_survives_until_owner_consumes():
    observations = ObservationCoordinator()
    observation_id = observations.register(
        "transition",
        owner="navigation",
        timeout=1,
        payload={"target_id": 48},
    )

    observations.complete(observation_id, {"status": "transitioned"})

    assert observations.wait(observation_id)["status"] == "completed"
    assert observations.consume(observation_id, owner="other") is None
    consumed = observations.consume(observation_id, owner="navigation")
    assert consumed["result"] == {"status": "transitioned"}
    assert observations.get(observation_id) is None


def test_observation_waiter_is_notified_by_publisher():
    observations = ObservationCoordinator()
    observation_id = observations.register("dialog_gate", owner="dialog_advance", timeout=1)
    received = []

    waiter = threading.Thread(
        target=lambda: received.append(observations.wait(observation_id, timeout=0.5)),
        daemon=True,
    )
    waiter.start()
    observations.complete(observation_id, {"status": "ready"})
    waiter.join(timeout=1)

    assert received[0]["status"] == "completed"
    assert received[0]["result"]["status"] == "ready"


def test_observation_timeout_and_cancel_wake_waiters():
    observations = ObservationCoordinator()
    timed_out_id = observations.register("interaction", owner="navigation", timeout=0.01)
    time.sleep(0.02)
    assert observations.wait(timed_out_id)["status"] == "timed_out"

    cancelled_id = observations.register("interaction", owner="navigation", timeout=1)
    observations.cancel(cancelled_id, "agent_interrupt")
    cancelled = observations.wait(cancelled_id)
    assert cancelled["status"] == "cancelled"
    assert cancelled["reason"] == "agent_interrupt"


def test_clear_all_removes_pending_and_completed_requests():
    observations = ObservationCoordinator()
    pending_id = observations.register("interaction", owner="navigation", timeout=1)
    completed_id = observations.register("transition", owner="navigation", timeout=1)
    observations.complete(completed_id, {"status": "transitioned"})

    assert observations.clear_all("game_reset") == 2
    assert observations.get(pending_id) is None
    assert observations.get(completed_id) is None
