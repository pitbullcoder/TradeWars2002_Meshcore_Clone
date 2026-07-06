"""
Unit tests for messaging.py: chunking, stale-message filtering, and the
acked multipart delivery added after a field test showed a 7-part menu
arriving as just "(1/7)".

The delivery tests pin the guarantees that fix depends on:

    1. Every chunk of a reply waits for its delivery ACK before the next
       chunk transmits (and min_timeout is passed so an optimistic
       firmware-suggested timeout can't cut the wait short).
    2. A chunk whose ACK is lost once gets a settle pause and one more
       full retry round, and the reply still completes.
    3. A chunk that can't be delivered at all aborts the reply -- the
       remaining chunks are never transmitted into the dead link.
    4. Two concurrent multipart sends never interleave on the radio:
       _TX_LOCK serializes them (including channel broadcasts against
       DMs), so one reply's chunk sequence finishes before the next
       transmission of any kind starts.

Like main_test.py, this stubs out the `db` and `meshcore` modules that
messaging.py imports, so it runs standalone -- no real database, radio,
or network needed. Run it with either of:

    python3 messaging_test.py
    python3 -m unittest messaging_test -v

It expects messaging.py to be importable from the same directory.
"""

import asyncio
import contextlib
import io
import sys
import time
import types
import unittest

# ---------------------------------------------------------------------------
# Stub out `db` and `meshcore` *before* messaging.py is imported, since it
# does `from db import log_message` and `from meshcore import EventType` at
# module load time.
# ---------------------------------------------------------------------------

# Every log_message() call lands here as a (direction, pubkey, sender, text)
# tuple; tests reset it in setUp() and assert that only *delivered* chunks
# were logged.
MESSAGE_LOG = []


def _install_stub_modules():
    db_stub = types.ModuleType("db")
    db_stub.log_message = lambda *args: MESSAGE_LOG.append(args)
    sys.modules["db"] = db_stub

    meshcore_stub = types.ModuleType("meshcore")

    class EventType:
        ERROR = "ERROR"
        OK = "OK"

    meshcore_stub.EventType = EventType
    sys.modules["meshcore"] = meshcore_stub


_install_stub_modules()
import messaging  # noqa: E402  (must come after the stubs are installed)
from meshcore import EventType  # noqa: E402  (the stub, same as messaging sees)


def run(coro):
    """Drive a coroutine to completion on a fresh event loop, muting the
    send functions' console prints (mirrors galaxy_test.py silencing the
    generation summary)."""
    with contextlib.redirect_stdout(io.StringIO()):
        return asyncio.run(coro)


def strip_prefix(chunk):
    """Drop the '(i/n) ' multipart prefix, if present."""
    if chunk.startswith("(") and ") " in chunk:
        return chunk.split(") ", 1)[1]
    return chunk


class FakeResult:
    def __init__(self, event_type):
        self.type = event_type
        self.payload = {"reason": "test"}


class FakeCommands:
    """
    Stand-in for mc.commands that records every radio interaction in
    order. Each entry in `events` is one of:

        ("tx", pubkey, chunk)      DM chunk transmitted
        ("ack", pubkey, chunk)     ...and its delivery ACK received
        ("noack", pubkey, chunk)   ...and its retries exhausted instead
        ("chan_tx", chan, chunk)   channel chunk broadcast

    `fail_once` chunks return no ACK the first time they're sent (then
    succeed); `fail_always` chunks never ACK. `chan_fail` chunks make
    send_chan_msg return an ERROR event.
    """

    def __init__(self):
        self.events = []
        self.fail_once = set()
        self.fail_always = set()
        self.chan_fail = set()
        self.min_timeouts_seen = []

    async def send_msg_with_retry(self, pubkey, chunk, min_timeout=0):
        self.min_timeouts_seen.append(min_timeout)
        self.events.append(("tx", pubkey, chunk))
        if chunk in self.fail_always:
            self.events.append(("noack", pubkey, chunk))
            return None
        if chunk in self.fail_once:
            self.fail_once.discard(chunk)
            self.events.append(("noack", pubkey, chunk))
            return None
        self.events.append(("ack", pubkey, chunk))
        return object()  # any non-None value means "acked"

    async def send_chan_msg(self, channel_idx, chunk):
        self.events.append(("chan_tx", channel_idx, chunk))
        if chunk in self.chan_fail:
            return FakeResult(EventType.ERROR)
        return FakeResult(EventType.OK)


class FakeMC:
    def __init__(self):
        self.commands = FakeCommands()


class RecordingSleep:
    """
    Replacement for asyncio.sleep installed into messaging's namespace so
    tests can assert on the pacing behavior (inter-chunk pause, settle
    delay before a chunk's second retry round) without actually waiting.
    Records each requested duration and yields control without delay.
    """

    def __init__(self):
        self.calls = []

    async def __call__(self, seconds):
        self.calls.append(seconds)
        await _real_sleep(0)


_real_sleep = asyncio.sleep


class MessagingTestCase(unittest.TestCase):
    """Shared fixture: fresh fake radio, fresh TX lock, recorded sleeps,
    cleared tx log. Delay constants are left at their real values --
    RecordingSleep never actually waits, so tests can assert the real
    durations were requested and still run instantly."""

    def setUp(self):
        MESSAGE_LOG.clear()
        self.mc = FakeMC()
        # A fresh lock per test: asyncio.run() gives every test its own
        # event loop, and a lock must not straddle loops.
        self._saved_lock = messaging._TX_LOCK
        messaging._TX_LOCK = asyncio.Lock()
        self.sleep = RecordingSleep()
        self._saved_sleep = messaging.asyncio.sleep
        messaging.asyncio.sleep = self.sleep

    def tearDown(self):
        messaging.asyncio.sleep = self._saved_sleep
        messaging._TX_LOCK = self._saved_lock

    # -- helpers ----------------------------------------------------------

    def multipart_text(self, lines=10):
        """Text long enough to need several chunks at the 130-char limit."""
        return "\n".join(f"line {i} " + "x" * 100 for i in range(lines))

    def tx_chunks(self, kind="tx"):
        return [e[2] for e in self.mc.commands.events if e[0] == kind]


class ChunkMessageTests(MessagingTestCase):
    def test_short_text_is_a_single_unprefixed_chunk(self):
        self.assertEqual(messaging.chunk_message("hello\nthere"), ["hello\nthere"])

    def test_empty_text_yields_one_empty_chunk(self):
        self.assertEqual(messaging.chunk_message(""), [""])

    def test_multipart_chunks_are_prefixed_and_within_limit(self):
        chunks = messaging.chunk_message(self.multipart_text())
        self.assertGreater(len(chunks), 1)
        n = len(chunks)
        for i, chunk in enumerate(chunks):
            self.assertLessEqual(len(chunk), messaging.MAX_MSG_LEN)
            self.assertTrue(chunk.startswith(f"({i + 1}/{n}) "),
                            f"chunk {i} missing its (i/n) prefix: {chunk!r}")

    def test_no_content_is_lost_across_chunks(self):
        text = self.multipart_text()
        chunks = messaging.chunk_message(text)
        rejoined = "\n".join(strip_prefix(c) for c in chunks)
        # Word-wrapping may move line breaks, but every word survives in order.
        self.assertEqual(rejoined.split(), text.split())

    def test_single_overlong_line_is_word_wrapped(self):
        text = "word " * 60  # one 300-char line, no newlines
        chunks = messaging.chunk_message(text.strip())
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), messaging.MAX_MSG_LEN)

    def test_ten_plus_chunks_stay_within_limit(self):
        # Crossing from 9 to 10+ chunks widens the "(i/n) " prefix by a
        # digit; the re-pack pass must keep every chunk <= the limit.
        chunks = messaging.chunk_message(self.multipart_text(lines=25))
        self.assertGreaterEqual(len(chunks), 10)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), messaging.MAX_MSG_LEN)

    def test_custom_limit_is_respected(self):
        chunks = messaging.chunk_message(self.multipart_text(), limit=60)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 60)


class IsStaleMessageTests(MessagingTestCase):
    def test_fresh_message_is_not_stale(self):
        payload = {"sender_timestamp": time.time() - 5}
        self.assertFalse(messaging.is_stale_message(payload))

    def test_old_message_is_stale(self):
        payload = {
            "sender_timestamp":
                time.time() - (messaging.MAX_MESSAGE_AGE_SECONDS + 30)
        }
        self.assertTrue(messaging.is_stale_message(payload))

    def test_missing_timestamp_is_never_stale(self):
        self.assertFalse(messaging.is_stale_message({}))


class SendReplyTests(MessagingTestCase):
    def test_single_chunk_reply_is_sent_and_logged(self):
        run(messaging.send_reply(self.mc, "PK", "Tester", "hi"))
        self.assertEqual(self.tx_chunks(), ["hi"])
        self.assertEqual(MESSAGE_LOG, [("tx", "PK", "Tester", "hi")])

    def test_every_chunk_waits_for_its_ack_before_the_next_transmits(self):
        text = self.multipart_text()
        run(messaging.send_reply(self.mc, "PK", "Tester", text))
        chunks = messaging.chunk_message(text)
        # The event stream must be strictly tx, ack, tx, ack, ... in chunk
        # order -- a tx appearing before the previous chunk's ack would
        # mean we fired the next part without waiting.
        expected = []
        for chunk in chunks:
            expected.append(("tx", "PK", chunk))
            expected.append(("ack", "PK", chunk))
        self.assertEqual(self.mc.commands.events, expected)

    def test_min_timeout_is_passed_on_every_send(self):
        run(messaging.send_reply(self.mc, "PK", "Tester", self.multipart_text()))
        self.assertTrue(self.mc.commands.min_timeouts_seen)
        for seen in self.mc.commands.min_timeouts_seen:
            self.assertEqual(seen, messaging.MIN_ACK_TIMEOUT_SECONDS)

    def test_chunks_are_paced_with_the_inter_chunk_delay(self):
        text = self.multipart_text()
        run(messaging.send_reply(self.mc, "PK", "Tester", text))
        n = len(messaging.chunk_message(text))
        # One pause after every chunk except the last, at the real duration.
        self.assertEqual(self.sleep.calls,
                         [messaging.INTER_CHUNK_DELAY_SECONDS] * (n - 1))

    def test_transiently_lost_chunk_is_retried_after_settle_and_delivered(self):
        text = self.multipart_text()
        chunks = messaging.chunk_message(text)
        self.mc.commands.fail_once = {chunks[1]}
        run(messaging.send_reply(self.mc, "PK", "Tester", text))
        # Chunk 2 transmitted twice, everything delivered exactly once.
        tx = self.tx_chunks()
        self.assertEqual(tx.count(chunks[1]), 2)
        self.assertEqual(self.tx_chunks("ack"), chunks)
        # The settle pause preceded the second attempt.
        self.assertIn(messaging.FAILED_CHUNK_RETRY_DELAY_SECONDS, self.sleep.calls)
        # And the full reply was logged, in order, with nothing dropped.
        self.assertEqual([entry[3] for entry in MESSAGE_LOG], chunks)

    def test_dead_chunk_aborts_without_transmitting_the_rest(self):
        text = self.multipart_text()
        chunks = messaging.chunk_message(text)
        self.mc.commands.fail_always = {chunks[1]}
        run(messaging.send_reply(self.mc, "PK", "Tester", text))
        # Chunk 1 delivered; chunk 2 tried twice (initial + settle retry);
        # chunks 3+ never hit the radio.
        self.assertEqual(self.tx_chunks(), [chunks[0], chunks[1], chunks[1]])
        self.assertEqual([entry[3] for entry in MESSAGE_LOG], [chunks[0]])

    def test_concurrent_replies_do_not_interleave(self):
        text = self.multipart_text()
        n = len(messaging.chunk_message(text))

        async def both():
            await asyncio.gather(
                messaging.send_reply(self.mc, "AAAA", "alice", text),
                messaging.send_reply(self.mc, "BBBB", "bob", text),
            )

        run(both())
        pubkeys = [e[1] for e in self.mc.commands.events if e[0] == "tx"]
        self.assertEqual(len(pubkeys), 2 * n)
        # All of the first sender's chunks, then all of the second's --
        # any alternation means the TX lock failed to serialize them.
        self.assertEqual(pubkeys, [pubkeys[0]] * n + [pubkeys[n]] * n)
        self.assertNotEqual(pubkeys[0], pubkeys[n])


class SendChannelReplyTests(MessagingTestCase):
    def test_multipart_broadcast_sends_every_chunk_in_order(self):
        text = self.multipart_text()
        run(messaging.send_channel_reply(self.mc, 0, text))
        chunks = messaging.chunk_message(text)
        self.assertEqual(self.tx_chunks("chan_tx"), chunks)
        self.assertEqual([entry[3] for entry in MESSAGE_LOG], chunks)
        self.assertEqual(self.sleep.calls,
                         [messaging.INTER_CHUNK_DELAY_SECONDS] * (len(chunks) - 1))

    def test_error_result_aborts_remaining_chunks(self):
        text = self.multipart_text()
        chunks = messaging.chunk_message(text)
        self.mc.commands.chan_fail = {chunks[1]}
        run(messaging.send_channel_reply(self.mc, 0, text))
        self.assertEqual(self.tx_chunks("chan_tx"), [chunks[0], chunks[1]])
        self.assertEqual([entry[3] for entry in MESSAGE_LOG], [chunks[0]])

    def test_broadcast_does_not_interleave_with_a_dm_in_progress(self):
        text = self.multipart_text()

        async def both():
            await asyncio.gather(
                messaging.send_reply(self.mc, "AAAA", "alice", text),
                messaging.send_channel_reply(self.mc, 0, text),
            )

        run(both())
        kinds = [e[0] for e in self.mc.commands.events if e[0] in ("tx", "chan_tx")]
        # Whichever grabbed the lock first runs to completion before the
        # other starts: the kind sequence is one contiguous block of each.
        switches = sum(1 for a, b in zip(kinds, kinds[1:]) if a != b)
        self.assertEqual(switches, 1, f"interleaved transmissions: {kinds}")


if __name__ == "__main__":
    unittest.main()
