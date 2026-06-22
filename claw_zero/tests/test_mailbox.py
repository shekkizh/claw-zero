"""Phase 1.1 acceptance — send 3 / receive 3 FIFO; poll() is None when empty."""

import asyncio

from claw_zero.messaging.mailbox import Mailbox, Message


def test_send_three_receive_three_fifo():
    async def run():
        mb = Mailbox()
        for i in range(3):
            await mb.send(Message(sender="human", recipient="claw-zero", content=f"m{i}"))
        got = [(await mb.receive()).content for _ in range(3)]
        return got

    assert asyncio.run(run()) == ["m0", "m1", "m2"]


def test_poll_returns_none_when_empty():
    async def run():
        mb = Mailbox()
        assert mb.poll() is None
        await mb.send(Message(sender="self", recipient="claw-zero", content="x", kind="tick"))
        polled = mb.poll()
        assert polled is not None and polled.content == "x"
        assert mb.poll() is None  # drained again
        return True

    assert asyncio.run(run()) is True


def test_message_auto_id_and_defaults():
    m = Message(sender="agent-b", recipient="claw-zero", content="hi")
    assert m.kind == "message"
    assert m.id.startswith("msg-")
    assert m.ts == ""  # producer fills ts; hot paths don't call datetime.now()
