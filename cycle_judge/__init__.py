"""cycle-judge: a System-1 trust layer around a ReAct agent loop.

Three independent judges, each answering in one shot (System-1) from a
pre-written prose digest:

    CompletionJudge  verdict  -- was the task done, and does the claim say so?
    FlailDetector    signal   -- is the loop repeating/stalling itself?
    InjectionScreen  gate     -- is outside-authored text trying to steer us?

Two rules hold everywhere:

* FAIL CLOSED ON VERDICTS. If a model errors, times out or answers below
  threshold, the verdict is 'unverified' (or 'FLAGGED' only on real evidence).
  Never 'supported', never 'clean', never PASS.
* FAIL OPEN ON GATES. A gate that cannot run must return the permissive
  answer, so a judge outage degrades to a no-op rather than dropping work.
"""

from .client import UNAVAILABLE, decide, decide_detailed
from .completion import CompletionJudge
from .flail import FlailDetector
from .injection import InjectionScreen, annotate

__all__ = [
    "CompletionJudge",
    "FlailDetector",
    "InjectionScreen",
    "decide",
    "decide_detailed",
    "UNAVAILABLE",
    "annotate",
]

__version__ = "0.1.0"