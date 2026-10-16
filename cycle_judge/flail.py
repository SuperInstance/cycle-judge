"""Flail detection over an agent's step log.

Two independent signals; either one flags.

(a) DETERMINISTIC, no network. Two rules:
    A1 "repeat": the same action (arguments stripped) with a similar result
       digest occurs >= ``window`` times.
    A2 "stall": a run of >= ``window`` consecutive non-advancing actions
       (reading state, deferring, asking questions, declaring done) that never
       produced real work. A1 alone misses the incident this module was
       written for -- an agent read the board, deferred a check, asked for
       clarification twice, and declared itself done: no single action
       repeats three times, but nothing advanced either.

(b) OPTIONAL decision-model check against Ollaya (model "laya"). Asks
    "is this making progress?" once per step and requires TWO CONSECUTIVE
    'repeating' verdicts, so a single ambiguous step cannot flag a run.

Fails open: if the model check is unavailable or times out, the deterministic
signal alone still decides. A flail detector that cannot run is not evidence
of flailing.
"""

from __future__ import annotations

import difflib
import re

from .client import DEFAULT_TIMEOUT, UNAVAILABLE, decide_detailed

PROGRESS_QUESTION = {
    "is this making progress?": {
        "type": "choice",
        "instructions": (
            "Is this step making progress toward the agent's assigned task? "
            "Judge whether the step changes the state of the work."
        ),
        "criteria": {
            "progress": (
                "This step does real work toward the task or resolves a real "
                "blocker."
            ),
            "repeating": (
                "This step repeats earlier work or stalls without moving the "
                "task forward."
            ),
            "going_backwards": (
                "This step undoes, re-opens, or re-litigates settled work."
            ),
        },
    }
}

#: Verbs that mean "did not advance the work".
_NO_ADVANCE = re.compile(
    r"(clarif|ask|question|query|defer|hold|wait|pause|confirm|"
    r"noop|no_op|idle|poll|status|read|list|board|inspect|survey|"
    r"declare|report|summary|checkin)",
    re.IGNORECASE,
)

_NON_WORD = re.compile(r"[^a-z0-9]+")


def action_family(action):
    """Strip arguments so 'read_file TASKS.md' -> 'read_file'."""
    text = str(action or "").strip().lower()
    text = re.split(r"[\s:(,\[]", text, maxsplit=1)[0]
    return text or "unknown"


def norm_digest(digest):
    """Collapse a result digest to comparable tokens.

    Digests differ by ids, counts and timestamps far more often than by
    meaning, so numbers are dropped before comparison.
    """
    text = _NON_WORD.sub(" ", str(digest or "").lower())
    return " ".join(w for w in text.split() if not w.isdigit())


def digests_similar(a, b, cutoff=0.7):
    na, nb = norm_digest(a), norm_digest(b)
    if not na or not nb:
        return na == nb
    if na == nb:
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= cutoff


def advances_work(action, result_ok):
    """False when the step could not have moved the task forward.

    A failed step is still progress if it was genuine work; a successful step
    is not progress if it only re-read, deferred or asked.
    """
    if action_family(action) in ("unknown",):
        return False
    return not _NO_ADVANCE.search(str(action or ""))


def _repeat_rule(steps, window):
    hits = {}
    for step in steps:
        key = action_family(step.get("action"))
        hits.setdefault(key, []).append(step)

    worst = None
    for family, group in hits.items():
        if len(group) < window:
            continue
        # Every member must have a digest similar to the first one.
        base = group[0].get("result_digest")
        if all(digests_similar(g.get("result_digest"), base) for g in group[1:]):
            if worst is None or len(group) > len(worst[1]):
                worst = (family, group)
    if worst:
        family, group = worst
        return {
            "flailing": True,
            "reason": (
                "repeat: action '%s' ran %d times with similar results "
                "(window=%d)" % (family, len(group), window)
            ),
            "signal": "deterministic-repeat",
            "action": family,
        }
    return None


def _stall_rule(steps, window):
    run = 0
    best = 0
    best_span = None
    span_start = 0
    for idx, step in enumerate(steps):
        if advances_work(step.get("action"), step.get("result_ok")):
            run = 0
            span_start = idx + 1
            continue
        if run == 0:
            span_start = idx
        run += 1
        if run > best:
            best = run
            best_span = (span_start, idx)
    if best >= window:
        actions = [
            str(steps[i].get("action")) for i in range(best_span[0], best_span[1] + 1)
        ]
        return {
            "flailing": True,
            "reason": (
                "stall: %d consecutive non-advancing steps (window=%d): %s"
                % (best, window, " -> ".join(actions))
            ),
            "signal": "deterministic-stall",
            "actions": actions,
        }
    return None


class FlailDetector:
    """Flags runs that repeat or stall without advancing the task."""

    def __init__(
        self,
        window=3,
        url=None,
        model="laya",
        model_threshold=0.5,
        timeout=DEFAULT_TIMEOUT,
    ):
        self.window = window
        self.url = url
        self.model = model
        self.model_threshold = model_threshold
        self.timeout = timeout

    # -- model signal ---------------------------------------------------

    def _model_signal(self, steps):
        """Per-step progress question; two consecutive 'repeating' flags."""
        per_step = []
        consecutive = 0
        flagged_at = None
        for idx, step in enumerate(steps):
            state = (
                "STEP {n} of {total}\n"
                "AGENT'S ASSIGNED TASK: (see surrounding run context)\n"
                "ACTION: {action}\n"
                "RESULT OK: {ok}\n"
                "RESULT: {digest}"
            ).format(
                n=idx + 1,
                total=len(steps),
                action=step.get("action"),
                ok=bool(step.get("result_ok")),
                digest=step.get("result_digest"),
            )
            answers, _errors = decide_detailed(
                self.url,
                self.model,
                state,
                PROGRESS_QUESTION,
                timeout=self.timeout,
            )
            choice, conf = answers.get("is this making progress?", (UNAVAILABLE, 0.0))
            repeating = (
                choice == "repeating" and conf >= self.model_threshold
            )
            consecutive = consecutive + 1 if repeating else 0
            if consecutive >= 2 and flagged_at is None:
                flagged_at = idx
            per_step.append(
                {
                    "step": idx,
                    "action": step.get("action"),
                    "choice": choice,
                    "confidence": round(conf, 4),
                    "repeating": repeating,
                }
            )
        return per_step, flagged_at

    # -- public ---------------------------------------------------------

    def check(self, steps, use_model=None):
        """Return {'flailing': bool, 'reason': str, ...}. Never raises."""
        steps = list(steps or [])
        result = {
            "flailing": False,
            "reason": "no flailing pattern detected",
            "signal": None,
            "steps": len(steps),
            "per_model": None,
        }
        if not steps:
            result["reason"] = "no steps to check"
            return result

        deterministic = _repeat_rule(steps, self.window) or _stall_rule(
            steps, self.window
        )
        if deterministic:
            result.update(deterministic)
        else:
            result["reason"] = "no repeat and no stall detected"

        if use_model is None:
            use_model = bool(self.url)
        if not use_model:
            result["model_check"] = "skipped (no url configured)"
            return result

        try:
            per_step, flagged_at = self._model_signal(steps)
        except Exception as exc:  # fails open: never let the judge break the run
            result["model_check"] = "unavailable (%s: %s)" % (type(exc).__name__, exc)
            return result

        result["per_model"] = per_step
        if flagged_at is not None:
            result["model_check"] = "two consecutive 'repeating' verdicts"
            result["flailing"] = True
            result["signal"] = (
                result["signal"] or "model-progress"
            )
            result["reason"] = (
                "progress-model: two consecutive 'repeating' verdicts "
                "(steps %d and %d)"
                % (flagged_at - 1, flagged_at)
            )
        else:
            result["model_check"] = "no two consecutive 'repeating' verdicts"
        return result