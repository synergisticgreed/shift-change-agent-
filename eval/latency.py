"""Latency benchmark: p50 / p95 / max per /chat message + max LLM calls (NFR-1, NFR-2).
Usage:  python -m eval.latency [n_messages]          # simulated LLM latency (no key needed)
        LLM_PROVIDER=gemini LLM_API_KEY=... python -m eval.latency 40  # real LLM (or groq)
Data Flow: script -> HTTP /chat (TestClient, full app) -> agent -> timed -> summary table."""
import os
import random
import sys
import tempfile
import time

workdir = tempfile.mkdtemp()  # throwaway DB, never touches agent.db
os.environ.update(DATABASE_URL=f"sqlite:///{workdir}/bench.db", CHECKPOINT_DB=f"{workdir}/cp.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.agent import service  # noqa: E402
from app.agent.graph import build_graph  # noqa: E402
from app.auth.jwt import issue_token  # noqa: E402
from app.config import settings  # noqa: E402
from app.llm.client import LLMError  # noqa: E402
from app.llm.fake import FakeLLM  # noqa: E402
from app.main import app  # noqa: E402

SCRIPT = ["Move my drop tomorrow to 19:00", "Move my drop tomorrow to 20:30", "yes",
          "Can I leave a bit late tomorrow?", "make it 7 instead", "any update?",
          "Move my drop tomorrow to 18:00", "Cancel my pickup tomorrow"]


class SimulatedLLM(FakeLLM):
    """FakeLLM answers + realistic latency: median ~0.35 s, long tail, 3% stalls."""
    rng = random.Random(42)

    def complete(self, system, user, json_mode=False, timeout=None):
        delay = 5.0 if self.rng.random() < 0.03 else self.rng.lognormvariate(-1.05, 0.6)
        time.sleep(min(delay, timeout))
        if delay > timeout:
            raise LLMError("timeout")
        return super().complete(system, user, json_mode)


def main(n: int) -> None:
    real = settings.llm_provider in ("groq", "gemini") and bool(settings.llm_api_key)
    if not real:
        service.graph = build_graph(SimulatedLLM(), f"{workdir}/cp.db")
    times, calls = [], []
    with TestClient(app) as c:
        for i in range(n):
            emp = f"E10{1 + (i // len(SCRIPT)) % 4}"            # rotate employees E101-E104
            hdr = {"Authorization": "Bearer " + issue_token("acme", emp)}
            t = time.perf_counter()
            r = c.post("/chat", json={"message": SCRIPT[i % len(SCRIPT)]}, headers=hdr).json()
            times.append(time.perf_counter() - t)
            calls.append(r["llm_calls"])
    times.sort()
    p = lambda q: times[min(len(times) - 1, int(q * len(times)))]  # noqa: E731
    print(f"LLM: {settings.llm_provider + ' ' + (settings.llm_model or 'default model') if real else 'simulated'}   messages: {n}")
    print(f"p50 {p(.50):.2f}s   p95 {p(.95):.2f}s   max {times[-1]:.2f}s   (target p95 < 5s)")
    print(f"LLM calls per message: max {max(calls)}   avg {sum(calls) / n:.1f}   (target <= 4)")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 40)
