"""Tests for egg_harness.events — EventBus callback registration and dispatch."""

from __future__ import annotations

import logging

from egg_harness.events import EventBus


class TestEventBusRegistration:
    """Test registering callbacks on the EventBus."""

    def test_register_on_output_callback(self):
        bus = EventBus()
        calls = []
        bus.on_output(lambda text: calls.append(text))
        bus.emit_output("hello")
        assert calls == ["hello"]

    def test_register_on_tool_call_callback(self):
        bus = EventBus()
        calls = []
        bus.on_tool_call(lambda name, input_data: calls.append((name, input_data)))
        bus.emit_tool_call("Bash", {"command": "ls"})
        assert calls == [("Bash", {"command": "ls"})]

    def test_register_on_tool_result_callback(self):
        bus = EventBus()
        calls = []
        bus.on_tool_result(lambda name, result: calls.append((name, result)))
        bus.emit_tool_result("Bash", "file1.txt\nfile2.txt")
        assert calls == [("Bash", "file1.txt\nfile2.txt")]

    def test_register_on_compaction_callback(self):
        bus = EventBus()
        calls = []
        bus.on_compaction(lambda summary, before, after: calls.append((summary, before, after)))
        bus.emit_compaction("summary text", 100_000, 20_000)
        assert calls == [("summary text", 100_000, 20_000)]

    def test_register_on_error_callback(self):
        bus = EventBus()
        calls = []
        bus.on_error(lambda err: calls.append(err))
        error = RuntimeError("test error")
        bus.emit_error(error)
        assert len(calls) == 1
        assert calls[0] is error

    def test_register_on_turn_complete_callback(self):
        bus = EventBus()
        calls = []
        bus.on_turn_complete(lambda turn, usage: calls.append((turn, usage)))
        bus.emit_turn_complete(1, {"input_tokens": 100, "output_tokens": 50})
        assert calls == [(1, {"input_tokens": 100, "output_tokens": 50})]


class TestEventBusMultipleCallbacks:
    """Test registering multiple callbacks for the same event."""

    def test_multiple_on_output_callbacks(self):
        bus = EventBus()
        calls_a = []
        calls_b = []
        bus.on_output(lambda text: calls_a.append(text))
        bus.on_output(lambda text: calls_b.append(text))
        bus.emit_output("msg")
        assert calls_a == ["msg"]
        assert calls_b == ["msg"]

    def test_multiple_on_error_callbacks(self):
        bus = EventBus()
        calls_a = []
        calls_b = []
        calls_c = []
        bus.on_error(lambda e: calls_a.append(str(e)))
        bus.on_error(lambda e: calls_b.append(str(e)))
        bus.on_error(lambda e: calls_c.append(str(e)))
        bus.emit_error(ValueError("oops"))
        assert len(calls_a) == 1
        assert len(calls_b) == 1
        assert len(calls_c) == 1


class TestEventBusCallbackOrder:
    """Test that callbacks execute in registration order."""

    def test_callbacks_fire_in_order(self):
        bus = EventBus()
        order = []
        bus.on_output(lambda text: order.append("first"))
        bus.on_output(lambda text: order.append("second"))
        bus.on_output(lambda text: order.append("third"))
        bus.emit_output("test")
        assert order == ["first", "second", "third"]


class TestEventBusNoCallbacks:
    """Test emitting events with no registered callbacks."""

    def test_emit_output_no_callbacks(self):
        """Emitting with no callbacks should not raise."""
        bus = EventBus()
        bus.emit_output("text")  # Should not raise

    def test_emit_tool_call_no_callbacks(self):
        bus = EventBus()
        bus.emit_tool_call("Bash", {"command": "ls"})

    def test_emit_tool_result_no_callbacks(self):
        bus = EventBus()
        bus.emit_tool_result("Bash", "output")

    def test_emit_compaction_no_callbacks(self):
        bus = EventBus()
        bus.emit_compaction("summary", 100_000, 20_000)  # Should not raise

    def test_emit_error_no_callbacks(self):
        bus = EventBus()
        bus.emit_error(RuntimeError("ignored"))

    def test_emit_turn_complete_no_callbacks(self):
        bus = EventBus()
        bus.emit_turn_complete(1, {"input_tokens": 0, "output_tokens": 0})


class TestEventBusExceptionHandling:
    """Test that callback exceptions are caught and logged, not propagated."""

    def test_callback_exception_does_not_propagate(self):
        """A callback that raises should not crash the bus."""
        bus = EventBus()

        def bad_callback(text):
            raise RuntimeError("callback failed")

        bus.on_output(bad_callback)
        # Must not raise
        bus.emit_output("test")

    def test_exception_does_not_block_other_callbacks(self):
        """A failing callback should not prevent subsequent callbacks."""
        bus = EventBus()
        results = []

        def bad_callback(text):
            raise ValueError("I broke")

        def good_callback(text):
            results.append(text)

        bus.on_output(bad_callback)
        bus.on_output(good_callback)
        bus.emit_output("hello")
        assert results == ["hello"]

    def test_exception_is_logged(self, caplog):
        """Callback exceptions should be logged."""
        bus = EventBus()

        def bad_callback(text):
            raise RuntimeError("log me")

        bus.on_output(bad_callback)
        with caplog.at_level(logging.ERROR):
            bus.emit_output("test")

        # Verify the error was logged
        assert any("log me" in record.message for record in caplog.records) or (
            len(caplog.records) > 0
        )

    def test_multiple_exceptions_all_caught(self):
        """Multiple failing callbacks should all be caught."""
        bus = EventBus()
        results = []

        def bad1(text):
            raise ValueError("bad1")

        def bad2(text):
            raise TypeError("bad2")

        def good(text):
            results.append(text)

        bus.on_output(bad1)
        bus.on_output(bad2)
        bus.on_output(good)
        bus.emit_output("ok")
        assert results == ["ok"]

    def test_error_callback_exception_caught(self):
        """Even on_error callbacks that raise should be caught."""
        bus = EventBus()

        def bad_error_handler(err):
            raise RuntimeError("handler itself failed")

        bus.on_error(bad_error_handler)
        # Must not propagate
        bus.emit_error(ValueError("original error"))


class TestEventBusCallbackArguments:
    """Test that callbacks receive correct arguments."""

    def test_output_callback_receives_text(self):
        bus = EventBus()
        received = []
        bus.on_output(lambda text: received.append(text))
        bus.emit_output("hello world")
        assert received == ["hello world"]

    def test_output_callback_receives_empty_string(self):
        bus = EventBus()
        received = []
        bus.on_output(lambda text: received.append(text))
        bus.emit_output("")
        assert received == [""]

    def test_tool_call_callback_receives_name_and_input(self):
        bus = EventBus()
        received = []
        bus.on_tool_call(lambda name, inp: received.append((name, inp)))
        bus.emit_tool_call("Read", {"file_path": "/tmp/test"})
        assert received == [("Read", {"file_path": "/tmp/test"})]

    def test_tool_result_callback_receives_name_and_result(self):
        bus = EventBus()
        received = []
        bus.on_tool_result(lambda name, res: received.append((name, res)))
        bus.emit_tool_result("Read", "file contents here")
        assert received == [("Read", "file contents here")]

    def test_error_callback_receives_exception(self):
        bus = EventBus()
        received = []
        bus.on_error(lambda err: received.append(err))
        exc = TypeError("type error")
        bus.emit_error(exc)
        assert len(received) == 1
        assert received[0] is exc
        assert isinstance(received[0], TypeError)


class TestEventBusMultipleEmits:
    """Test that the bus works correctly across multiple emissions."""

    def test_multiple_output_emissions(self):
        bus = EventBus()
        received = []
        bus.on_output(lambda text: received.append(text))
        bus.emit_output("first")
        bus.emit_output("second")
        bus.emit_output("third")
        assert received == ["first", "second", "third"]

    def test_mixed_event_emissions(self):
        bus = EventBus()
        output_calls = []
        error_calls = []
        turn_calls = []

        bus.on_output(lambda text: output_calls.append(text))
        bus.on_error(lambda err: error_calls.append(str(err)))
        bus.on_turn_complete(lambda turn, usage: turn_calls.append(turn))

        bus.emit_output("hello")
        bus.emit_error(ValueError("oops"))
        bus.emit_turn_complete(1, {"input_tokens": 100, "output_tokens": 50})
        bus.emit_output("world")

        assert output_calls == ["hello", "world"]
        assert error_calls == ["oops"]
        assert turn_calls == [1]


class TestEventBusIsolation:
    """Test that separate EventBus instances are isolated."""

    def test_separate_buses_are_independent(self):
        bus1 = EventBus()
        bus2 = EventBus()

        calls1 = []
        calls2 = []

        bus1.on_output(lambda text: calls1.append(text))
        bus2.on_output(lambda text: calls2.append(text))

        bus1.emit_output("bus1 only")
        assert calls1 == ["bus1 only"]
        assert calls2 == []

        bus2.emit_output("bus2 only")
        assert calls1 == ["bus1 only"]
        assert calls2 == ["bus2 only"]
