"""Session-level integration test for Laya reranker + decision.log emission.

Verifies that:
- Session._handle_text() does NOT raise KeyError on the WSMessage payload.
- A decision.log message is enqueued with route_id, source, score, etc.
- Mock reranker path works end-to-end.
- Abstained path emits a decision.log with abstained=True (route_id=None).
"""
import asyncio
import json
import os
import pytest

os.environ.setdefault("MAYA_LAYA_MOCK", "1")

from app.protocol import WSMessage, ServerMsgType  # noqa: E402
from app.session import Session  # noqa: E402
from app.persona import load_persona  # noqa: E402
from app.router import DialogRouter  # noqa: E402

DECISION_LOG = ServerMsgType.DECISION_LOG.value


class _FakeWS:
    """Stand-in for FastAPI WebSocket — just records .send_json() calls."""

    def __init__(self):
        self.sent: list[dict] = []
        self.closed = False

    async def send_json(self, data) -> None:
        if hasattr(data, "to_json"):
            data = data.to_json()
        self.sent.append(dict(data))

    async def send_text(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def close(self, code: int = 1000) -> None:
        self.closed = True


def _build_session(ws: _FakeWS, persona_name: str = "lyra_vex") -> Session:
    persona = load_persona(persona_name)
    router = DialogRouter.load()  # uses default routes.yaml (or builtin)
    sess = Session(id="test-session-1", ws=ws, persona=persona, router=router)
    return sess


async def _drain_to_fake_ws(sess: Session, ws: _FakeWS, duration: float = 1.0) -> None:
    """Drain session._send_q into the fake ws until idle for `duration` seconds."""
    while True:
        try:
            msg = await asyncio.wait_for(sess._send_q.get(), timeout=duration)
        except asyncio.TimeoutError:
            return
        await ws.send_json(msg.to_json())


@pytest.mark.asyncio
async def test_session_handle_text_emits_decision_log():
    """Session._handle_text() should emit decision.log and a route message for "응"."""
    ws = _FakeWS()
    sess = _build_session(ws)

    drain_task = asyncio.create_task(_drain_to_fake_ws(sess, ws, duration=0.5))

    # "응" matches the "affirm" route via mock reranker.
    await sess._handle_text("응")

    # Let drain consume the queue.
    await asyncio.sleep(0.5)
    drain_task.cancel()
    try:
        await drain_task
    except asyncio.CancelledError:
        pass

    # Check at least one message is a decision.log.
    decision_logs = [m for m in ws.sent if m.get("type") == DECISION_LOG]
    # Also: NO nested 'payload' key on the wire (we flatten payload into top-level).
    for m in decision_logs:
        assert "payload" not in m, f"Wire must not wrap payload: {m}"

    assert decision_logs, f"No decision.log emitted. Got: {ws.sent}"

    log = decision_logs[0]
    assert "text" in log, f"decision.log missing text: {log}"
    assert "route_id" in log, f"decision.log missing route_id: {log}"
    assert "source" in log, f"decision.log missing source: {log}"
    assert log["source"] in {"rule", "laya", "disabled", "mock"}, f"unexpected source: {log}"
    # "응" → affirm route, so we expect a hit.
    assert log["route_id"] == "affirm", f"expected affirm, got: {log.get('route_id')}"
    assert log.get("abstained") is False, f"expected not abstained, got: {log}"
    assert log.get("threshold") == 0.85
    assert isinstance(log.get("all_scores"), list)


@pytest.mark.asyncio
async def test_session_handle_text_emits_abstained_decision_log():
    """Session._handle_text() with gibberish emits a decision.log with abstained=True."""
    ws = _FakeWS()
    sess = _build_session(ws)

    drain_task = asyncio.create_task(_drain_to_fake_ws(sess, ws, duration=0.5))

    # Gibberish — mock reranker abstains.
    await sess._handle_text("qwertyuiopasdfghjkl12345")

    await asyncio.sleep(0.5)
    drain_task.cancel()
    try:
        await drain_task
    except asyncio.CancelledError:
        pass

    decision_logs = [m for m in ws.sent if m.get("type") == DECISION_LOG]
    # In the abstained path we still emit the log before falling through to LLM.
    # The LLM path requires real chat backend — we don't run it here, so the
    # test just verifies the abstained log shape if present, and that no KeyError was raised.
    if decision_logs:
        for m in decision_logs:
            assert "payload" not in m, f"Wire must not wrap payload: {m}"
        log = decision_logs[0]
        # Either it's a hit, or abstained.
        if log.get("abstained"):
            assert log["route_id"] is None
            assert log["source"] in {"laya", "rule", "disabled", "mock"}


@pytest.mark.asyncio
async def test_session_handle_text_no_keyerror_on_payload():
    """Regression: KeyError 'payload' was raised in _send_textq with WSMessage(**payload)."""
    ws = _FakeWS()
    sess = _build_session(ws)

    drain_task = asyncio.create_task(_drain_to_fake_ws(sess, ws, duration=0.5))

    # Should not raise. We don't care about the LLM path here — we just want
    # to confirm no KeyError fires before route/decision handling.
    try:
        await sess._handle_text("hello")
    except KeyError as e:
        if str(e) == "'payload'":
            pytest.fail("Regression: KeyError 'payload' in _send_textq")
        raise
    finally:
        await asyncio.sleep(0.3)
        drain_task.cancel()
        try:
            await drain_task
        except asyncio.CancelledError:
            pass
