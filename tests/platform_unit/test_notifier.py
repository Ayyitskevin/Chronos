"""SafeNotifierFanout: failure isolation and history retention."""

from __future__ import annotations

from chronos.notifications.notifier import (
    Notification,
    NotificationKind,
    SafeNotifierFanout,
)


class RecordingNotifier:
    def __init__(self) -> None:
        self.received: list[Notification] = []

    def send(self, notification: Notification) -> bool:
        self.received.append(notification)
        return True


class RaisingNotifier:
    def send(self, notification: Notification) -> bool:
        raise RuntimeError("simulated transport failure")


class FalseReturningNotifier:
    def send(self, notification: Notification) -> bool:
        return False


class TestFailureIsolation:
    def test_raising_notifier_does_not_stop_delivery(self) -> None:
        recording = RecordingNotifier()
        # The raising notifier comes FIRST so a leaked exception would have
        # prevented the recording notifier from being reached.
        fanout = SafeNotifierFanout(notifiers=(RaisingNotifier(), recording))

        fanout.notify(NotificationKind.KILL_SWITCH, "halted", "unit test")

        assert fanout.failure_count == 1
        assert len(recording.received) == 1
        assert recording.received[0].kind is NotificationKind.KILL_SWITCH
        assert recording.received[0].summary == "halted"
        assert recording.received[0].detail == "unit test"

    def test_false_return_counts_as_failure(self) -> None:
        fanout = SafeNotifierFanout(notifiers=(FalseReturningNotifier(),))
        fanout.notify(NotificationKind.FILL, "filled")
        fanout.notify(NotificationKind.FILL, "filled again")
        assert fanout.failure_count == 2

    def test_all_failures_accumulate_across_notifiers(self) -> None:
        fanout = SafeNotifierFanout(
            notifiers=(RaisingNotifier(), FalseReturningNotifier(), RecordingNotifier())
        )
        fanout.notify(NotificationKind.RISK_REJECTION, "denied")
        assert fanout.failure_count == 2


class TestHistory:
    def test_history_retained_even_when_all_notifiers_fail(self) -> None:
        fanout = SafeNotifierFanout(notifiers=(RaisingNotifier(),))
        fanout.notify(NotificationKind.ORDER_SUBMITTED, "first", "d1")
        fanout.notify(NotificationKind.CANCELLATION, "second", "d2")

        history = fanout.history
        assert [n.kind for n in history] == [
            NotificationKind.ORDER_SUBMITTED,
            NotificationKind.CANCELLATION,
        ]
        assert [n.summary for n in history] == ["first", "second"]
        assert history[0].at_utc.tzinfo is not None

    def test_history_is_a_snapshot_tuple(self) -> None:
        fanout = SafeNotifierFanout(notifiers=())
        fanout.notify(NotificationKind.STARTUP, "boot")
        snapshot = fanout.history
        fanout.notify(NotificationKind.SHUTDOWN, "bye")
        assert len(snapshot) == 1
        assert len(fanout.history) == 2
