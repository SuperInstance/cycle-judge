"""HTTP client for the Ollaya decision server.

Contract for every caller in this package:

    decide(...) NEVER RAISES. It always returns a mapping of
    question-id -> (choice, confidence). Any question that could not be
    resolved comes back as (UNAVAILABLE, 0.0).

That is the whole fail-safe story. Judges interpret UNAVAILABLE as
"no evidence" and apply their own fail-closed / fail-open policy on top.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

#: Default per-request budget. The first call after a model reload can be
#: slow (measured ~8s cold on this box), so keep real headroom under 30.
DEFAULT_TIMEOUT = 30.0

#: Sentinel choice used whenever a question could not be answered.
#: Judges MUST NOT treat it as a substantive answer in either direction.
UNAVAILABLE = "__unavailable__"


def _unavailable_all(question_ids):
    return {qid: (UNAVAILABLE, 0.0) for qid in question_ids}


def _clamp_conf(value):
    try:
        conf = float(value)
    except (TypeError, ValueError):
        return 0.0
    if conf != conf:  # NaN
        return 0.0
    return max(0.0, min(1.0, conf))


def decide_detailed(url, model, state, questions, timeout=DEFAULT_TIMEOUT):
    """Call the decision server and return ``(answers, errors)``.

    ``answers`` maps question-id -> (choice, confidence) and always contains
    an entry for every id in ``questions``. ``errors`` maps question-id ->
    human-readable reason, for ids that could not be answered.

    Never raises.
    """
    question_ids = list(questions.keys())
    errors = {}

    if not url:
        return _unavailable_all(question_ids), {
            qid: "no decision-server url configured" for qid in question_ids
        }
    if not question_ids:
        return {}, {}

    endpoint = url.rstrip("/") + "/v1/decisions"
    payload = {
        "model": model,
        "state": state,
        "questions": questions,
    }

    try:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            endpoint, data=body, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
        parsed = json.loads(raw)
    except urllib.error.HTTPError as exc:
        return _unavailable_all(question_ids), {
            qid: "http %s from %s" % (exc.code, endpoint) for qid in question_ids
        }
    except Exception as exc:  # timeout, connection refused, bad JSON, ...
        return _unavailable_all(question_ids), {
            qid: "%s: %s" % (type(exc).__name__, exc) for qid in question_ids
        }

    if not isinstance(parsed, dict):
        return _unavailable_all(question_ids), {
            qid: "unexpected response shape: %r" % (type(parsed).__name__,)
            for qid in question_ids
        }

    returned = parsed.get("answers")
    if not isinstance(returned, dict):
        return _unavailable_all(question_ids), {
            qid: "response contained no answers object" for qid in question_ids
        }

    answers = {}
    for qid in question_ids:
        entry = returned.get(qid)
        if not isinstance(entry, dict) or "choice" not in entry:
            answers[qid] = (UNAVAILABLE, 0.0)
            errors[qid] = "question absent or malformed in response"
            continue
        answers[qid] = (str(entry["choice"]), _clamp_conf(entry.get("confidence")))
    return answers, errors


def decide(url, model, state, questions, timeout=DEFAULT_TIMEOUT):
    """Return ``{question_id: (choice, confidence)}``; never raises.

    Unanswerable questions map to ``(UNAVAILABLE, 0.0)``.
    """
    answers, _errors = decide_detailed(
        url, model, state, questions, timeout=timeout
    )
    return answers