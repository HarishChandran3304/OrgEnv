"""Colleagues talk in words (the ``chat`` switch).

The seeded engine still decides everything: what a colleague does, when, and which facts they
give out. This module only changes the channel between Alex and the people around him.

* **Alex writes plain messages.** A *reader* turns each message into the requests the
  recipient can act on (the same requests the engine has always understood). The reader never
  decides an outcome: it can only produce a request Alex could have made, which the engine
  then judges exactly as before.
* **Colleagues answer in their own voice.** A *writer* rewrites the engine's reply in the
  sender's persona. Every rendering is checked to carry every fact of the engine's reply and
  to add none; a rendering that fails the check is retried and then replaced by the engine's
  own wording, so the agent is never told something the engine did not decide.

The world state, the event log and the verifier keep the engine's wording and its requests,
so a run replays without any model. What the agent actually read is recorded next to it.

Two backends: ``ScriptedBackend`` (no model: keyword reader, engine wording) for tests and
reference sweeps, and ``ModelBackend`` (a language model behind ``complete``).
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
import re
import urllib.error
import urllib.request

from .day import TOPICS as DAY_TOPICS, scripted_asks
from .model import stream


class ChatUnavailable(Exception):
    """The language model behind the chat layer could not be reached."""


# --------------------------------------------------------------------------- personas
PERSONAS = {
    "daniel": {"name": "Daniel", "role": "payments engineer",
               "voice": "Precise and a little dry. Short sentences. States what he did, then where to find it."},
    "maya": {"name": "Maya", "role": "billing platform lead",
             "voice": "Direct and protective of her merchants. Says yes or no first, then why. No pleasantries."},
    "priya": {"name": "Priya", "role": "site reliability engineer",
              "voice": "Calm and procedural. Refers to what she checked. Never vague about whether something is approved."},
    "nina": {"name": "Nina", "role": "database engineer",
             "voice": "Careful and exact. Lists problems one by one. Does not soften a rejection."},
    "jordan": {"name": "Jordan", "role": "incident commander",
               "voice": "Brisk, in command, used to incident channels. Leads with the decision."},
    "sam": {"name": "Sam", "role": "customer support lead",
            "voice": "Warm and customer-minded. Friendly in tone, but says only what he has to report."},
}
# One mood per person per incident, drawn from the seed: the same person does not write the
# same way every day.
MOODS = (
    ("unhurried", "Has a quiet moment: writes in full sentences."),
    ("rushed", "Is in the middle of something else: writes tersely, drops greetings, may use fragments."),
    ("thorough", "Likes to be understood: restates the request in a few words before answering."),
    ("informal", "Writes the way people do in a team chat: lower-case starts, contractions, no sign-off."),
)


def persona(seed, actor):
    mood = MOODS[stream(seed, "persona:" + actor).randrange(len(MOODS))]
    return {**PERSONAS[actor], "mood": mood[0], "mood_note": mood[1]}


# --------------------------------------------------------------------------- facts
_ID = r"\b(?:inv|sub|m|ask|dp|ul|lx|rq|chg|gr|host|py|st|le|pl|er|cr)-[A-Za-z0-9]+\b|\bch_[0-9]+\b|\b[0-9a-f]{8,64}\b"
_NUMBER = r"(?<![A-Za-z0-9_\-.])\d+(?:\.\d+)?(?![A-Za-z0-9_\-]|\.\d)"
_TOOL = r"\b[a-z]+(?:_[a-z]+)+\b"
_TERMS = ("primary", "secondary", "paused", "quarantined", "running", "rebill", "release", "legitimate", "duplicate")


def hard_tokens(text):
    """Identifiers, numbers and tool names: what a rendering must carry exactly."""
    lowered = text.lower()
    tokens = set(re.findall(_ID, lowered)) | set(re.findall(_TOOL, lowered))
    tokens |= {t.rstrip("0").rstrip(".") if "." in t else t for t in re.findall(_NUMBER, lowered)}
    return tokens


def _strict_tokens(text):
    lowered = text.lower()
    return set(re.findall(_ID, lowered)) | {t.rstrip("0").rstrip(".") if "." in t else t for t in re.findall(_NUMBER, lowered)}


def token_problems(source, rendering):
    """Why ``rendering`` cannot stand for ``source``, judged on exact tokens only."""
    need, got = hard_tokens(source), hard_tokens(rendering)
    missing = sorted(need - got)
    missing += [w for w in _TERMS if re.search(rf"\b{w}\b", source.lower()) and not re.search(rf"\b{w}\b", rendering.lower())]
    added = sorted(_strict_tokens(rendering) - _strict_tokens(source))
    problems = []
    if missing:
        problems.append("missing: " + ", ".join(missing))
    if added:
        problems.append("not in the original: " + ", ".join(added))
    return problems


# --------------------------------------------------------------------------- what a recipient can be asked
HANDLES = {
    "maya": ("request_pool_change", "request_contract_terms", "ask_colleague"),
    "jordan": ("request_emergency", "request_contract_terms", "ask_colleague"),
    "sam": ("request_support_hold", "advise_support", "request_rebill_agreement", "request_contract_terms", "ask_colleague"),
    "daniel": ("request_contract_terms", "ask_colleague"),
    "nina": ("ask_colleague",), "priya": ("ask_colleague",),
}
# What a person does whenever a message reaches them: it needs no request to be read out.
ALWAYS = {
    "daniel": "Whenever a message reaches him he publishes a provider ledger export.",
    "maya": "Whenever a message reaches her she publishes the worker journals of every pool.",
    "nina": "Whenever a message reaches her she reviews the current repair plan.",
    "priya": "Whenever a message reaches her she reviews the prepared release for approval.",
    "jordan": "", "sam": "",
}
READS = {
    "request_pool_change": "set a worker pool's mode. args: {\"pool\": \"primary\"|\"secondary\", \"mode\": \"paused\"|\"quarantined\"|\"running\"}. "
                           "One entry per pool. Maya's own pool is the secondary pool.",
    "request_emergency": "an emergency measure. args: {\"kind\": \"stop_billing\"} to stop every worker pool, or "
                         "{\"kind\": \"provider_audit\"} for the one-time export of settled provider charges.",
    "request_support_hold": "hold goodwill refunds to complaining customers for now. args: {}",
    "advise_support": "how to answer double-charge complaints. args: {\"subscription_ids\": [\"sub-...\"], \"verdict\": "
                      "\"legitimate\" (the charges belong to different invoices: close without a refund) | \"duplicate\" "
                      "(a real duplicate: the incident will refund it)}. One entry per verdict.",
    "request_rebill_agreement": "contact customers and ask whether they agree to be billed again. args: {\"subscription_ids\": [\"sub-...\"]}",
    "request_contract_terms": "the contract refund term of a merchant account. args: {\"merchant_id\": \"m-...\"}. One entry per merchant.",
    "ask_colleague": "something this colleague holds or does for one of the day's other cards. args: {\"topic\": <one of the topics below>, "
                     "\"items\": [<what it is about, as the topic says; identifiers copied exactly>]}. One entry per topic. Topics: "
                     + " | ".join(f"{topic}: {spec['about']}" for topic, (_, spec) in sorted(DAY_TOPICS.items())),
    "answer_ask": "Alex is answering a question this person asked him. args: {\"ask_id\": \"ask-...\", \"value\": <whole number>}",
}

_SENTENCES = re.compile(r"[.!?]+(?=\s|$)|[\n;]+")


def scripted_read(recipient, text, open_asks):
    """Keyword reader used without a model. It understands plain, explicit requests."""
    handles = HANDLES.get(recipient, ())
    intents = []
    for raw in _SENTENCES.split(text):
        s = raw.lower().strip()
        if not s:
            continue
        if "request_pool_change" in handles:
            pools = re.findall(r"\b(primary|secondary)\b", s)
            if re.search(r"\b(both|every|all|each)\b[^.]*\bpools?\b", s):
                pools = ["primary", "secondary"]
            mode = ("quarantined" if "quarantin" in s else "paused" if re.search(r"\b(pause|paused|stop|halt)\b", s)
                    else "running" if re.search(r"\b(resume|running|restart|start)\b", s) else None)
            if mode and "pool" in s:
                intents += [{"op": "request_pool_change", "args": {"pool": p, "mode": mode}} for p in dict.fromkeys(pools)]
        if "request_emergency" in handles:
            if re.search(r"emergency stop|stop (all|every)|stop billing", s):
                intents.append({"op": "request_emergency", "args": {"kind": "stop_billing"}})
            if re.search(r"\b(export|audit)\b", s):
                intents.append({"op": "request_emergency", "args": {"kind": "provider_audit"}})
        if "request_contract_terms" in handles and ("contract" in s or "refund term" in s) and not re.search(r"amendment|price|\bfee\b|\bbank\b", s):
            intents += [{"op": "request_contract_terms", "args": {"merchant_id": m}} for m in dict.fromkeys(re.findall(r"\bm-[a-z0-9]+\b", s))]
        if "request_support_hold" in handles:
            subs = list(dict.fromkeys(re.findall(r"\bsub-[0-9]+\b", s)))
            if "hold" in s and "refund" in s:
                intents.append({"op": "request_support_hold", "args": {}})
            elif subs and "legitimate" in s:
                intents.append({"op": "advise_support", "args": {"subscription_ids": subs, "verdict": "legitimate"}})
            elif subs and "duplicate" in s:
                intents.append({"op": "advise_support", "args": {"subscription_ids": subs, "verdict": "duplicate"}})
            elif subs and re.search(r"agree|billed again|bill them again", s):
                intents.append({"op": "request_rebill_agreement", "args": {"subscription_ids": subs}})
        if open_asks and "answer" in s:
            number = re.search(_NUMBER, s)
            if number and "." not in number.group():
                intents.append({"op": "answer_ask", "args": {"ask_id": open_asks[0]["ask_id"], "value": int(number.group())}})
    if "ask_colleague" in handles:
        intents += scripted_asks(text)
    return {"intents": intents, "unclear": ""}


class ScriptedBackend:
    """No model: explicit requests are read by keyword, colleagues use the engine's wording."""
    name = "scripted"

    def read(self, recipient, text, context):
        return scripted_read(recipient, text, context["open_asks"])

    def write(self, sender, source, context):
        return {"text": source, "attempts": 0, "fallback": False}


# --------------------------------------------------------------------------- model backend
def _first_json(text):
    start = text.find("{")
    while start != -1:
        depth, quoted, escaped = 0, False, False
        for i in range(start, len(text)):
            c = text[i]
            if quoted:
                escaped = (c == "\\") and not escaped
                if c == '"' and not escaped:
                    quoted = False
                if c != "\\":
                    escaped = False
            elif c == '"':
                quoted = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except ValueError:
                        break
        start = text.find("{", start + 1)
    raise ValueError("no JSON object in the model's answer")


READER_SYSTEM = """You read one chat message that Alex, an on-call engineer at RelayPay, sent to a colleague during a payment incident, and you list the requests in it that this colleague can act on.

You do not decide whether a request is granted. You only report what Alex asked for.

Rules:
- Report a request only if the message asks this colleague for it. A status update, a thank-you or a question about something else is not a request.
- Every detail in "args" must be stated in the message, or follow without doubt from the earlier messages shown or from the notes about the colleague. Do not guess. Copy identifiers exactly.
- Use only the request names listed for this colleague. Anything else Alex asks for (a review, an approval, an export, journals, a status) needs no entry: the colleague reads the message themselves. Never mention such things in "unclear".
- Report what the new message says, completely, even where it repeats or contradicts an earlier message: Alex may have changed his mind, and that is his call. Earlier messages are shown only to resolve references such as "the same for the other pool".
- Only if Alex clearly wants one of the listed things but a required detail cannot be told (for example which pool, or which customers), leave it out of "intents" and say in "unclear", in one short sentence, what is missing. Otherwise "unclear" is empty.
- A message may hold several requests, or none.

Answer with one JSON object and nothing else:
{"intents": [{"op": "<name>", "args": {...}}], "unclear": "<empty, or what is missing>"}"""

WRITER_SYSTEM = """You write one chat message for a person at RelayPay, a payments company, during a billing incident. You are given who they are, how they write, and the exact content of what they are telling Alex, the on-call engineer.

Write it the way this person would type it in the incident channel.

Rules:
- Say everything the content says and nothing it does not say. Do not add facts, advice, offers, promises, times, counts or opinions about what Alex should do.
- Keep every identifier, number and tool or record name exactly as written (for example inv-1234, sub-5678, m-kite, ch_1001, release hashes, provider_audit, customer_reports). Keep the words primary, secondary, paused, quarantined, running, rebill, release, legitimate and duplicate where the content uses them.
- Do not turn a yes into a maybe or a no into a maybe. A refusal stays a refusal, with its reason.
- Times are written as in the content (for example t=5). Do not use digits for anything that is not in the content.
- If you are shown the message from Alex they are replying to, it is there for tone only. Take no identifier, number or fact from it.
- When the content is a question this person is putting to Alex, write it as their question to Alex, with the time they need the answer by. They are asking, not answering.
- Plain text only: no markdown, no lists unless the content lists many items, no signature.

Answer with the message text only."""

JUDGE_SYSTEM = """You compare two versions of one chat message. A is the original. B is a rewrite in a different voice.

B is acceptable only if a careful reader would learn exactly the same facts and decisions from B as from A: every decision (granted, refused, applied, not applied, approved, rejected), every reason, every identifier, number and name, and nothing that A does not say. Who is speaking and what kind of message it is must not change: a question stays the same question put to the same person, a statement stays a statement, a refusal stays a refusal. B must not add an offer, a promise, advice or an instruction. Tone, order and wording may differ. A greeting or a word of filler is fine.

Answer with one JSON object and nothing else:
{"same": true or false, "problem": "<empty, or the first difference>"}"""


class ModelBackend:
    """A language model reads Alex's messages and words the colleagues' replies.

    ``complete(system, user, max_tokens)`` returns the model's text. Every reply is checked
    against the engine's wording; ``max_attempts`` failed renderings fall back to it.
    """
    name = "model"

    def __init__(self, complete, *, max_attempts=3, label="model"):
        self.complete, self.max_attempts, self.label = complete, max_attempts, label
        self.calls = 0

    def _ask(self, system, user, max_tokens):
        self.calls += 1
        return self.complete(system, user, max_tokens)

    def read(self, recipient, text, context):
        handles = list(HANDLES.get(recipient, ())) + (["answer_ask"] if context["open_asks"] else [])
        person = PERSONAS[recipient]
        lines = [f"The colleague: {person['name']}, {person['role']}.", f"Notes: {context['recipient_note']}. {ALWAYS[recipient]}", "",
                 "What this colleague can be asked for:"]
        lines += [f"- {op}: {READS[op]}" for op in handles] or ["- nothing from the list; any message simply reaches them."]
        if context["open_asks"]:
            lines += ["", "Questions this colleague has asked Alex and is waiting on:"]
            lines += [f"- {a['ask_id']}: {a['question']}" for a in context["open_asks"]]
        if context["thread"]:
            lines += ["", "Earlier messages between them, oldest first:"]
            lines += [f"{who}: {said}" for who, said in context["thread"][-6:]]
        lines += ["", "Alex's new message:", text]
        last = None
        for _ in range(2):
            try:
                result = _first_json(self._ask(READER_SYSTEM, "\n".join(lines), 1500))
                intents = result.get("intents")
                if isinstance(intents, list) and all(isinstance(i, dict) and isinstance(i.get("op"), str)
                                                     and isinstance(i.get("args", {}), dict) for i in intents):
                    return {"intents": [{"op": i["op"], "args": i.get("args", {})} for i in intents],
                            "unclear": str(result.get("unclear") or "")[:300]}
                last = "malformed intents"
            except ValueError as exc:
                last = str(exc)
        raise ChatUnavailable("the reader gave no usable answer: " + str(last))

    def write(self, sender, source, context):
        who = context["persona"]
        prompt = [f"Who is writing: {who['name']}, {who['role']}.", f"How they write: {who['voice']}",
                  f"Today: {who['mood_note']}"]
        if context.get("replying_to"):
            prompt += ["", "They are replying to this message from Alex (for tone only):", context["replying_to"]]
        if context.get("kind") == "ask":
            prompt += ["", "The content is a question they are putting to Alex. They need Alex to give them the number."]
        if context.get("kind") == "note":
            prompt += ["", "They are writing to Alex unprompted: this is something they want from him or want him to know."]
        prompt += ["", "The content to convey (complete and exact):", source]
        problem = None
        for attempt in range(1, self.max_attempts + 1):
            user = "\n".join(prompt + (["", f"Your previous attempt was rejected ({problem}). Write it again."] if problem else []))
            try:
                text = self._ask(WRITER_SYSTEM, user, 1200).strip()
            except ChatUnavailable:
                problem = "model unavailable"
                continue
            issues = token_problems(source, text) if text else ["empty"]
            if not issues:
                try:
                    verdict = _first_json(self._ask(JUDGE_SYSTEM, f"A:\n{source}\n\nB:\n{text}", 300))
                    if verdict.get("same") is True:
                        return {"text": text, "attempts": attempt, "fallback": False}
                    issues = [str(verdict.get("problem") or "judged different")[:200]]
                except (ValueError, ChatUnavailable):
                    issues = ["no verdict"]
            problem = "; ".join(issues)
        return {"text": source, "attempts": self.max_attempts, "fallback": True, "problem": problem}


def anthropic_complete(model, *, api_key, base_url=None, effort="low"):
    """``complete`` over the Anthropic Messages API (first-party, or a relay given by ``base_url``)."""
    import anthropic
    base_url = base_url.rstrip("/").removesuffix("/v1") if base_url else None  # a relay address may end in /v1
    client = anthropic.Anthropic(api_key=api_key, base_url=base_url, timeout=60.0, max_retries=2)

    def complete(system, user, max_tokens):
        try:
            response = client.messages.create(model=model, max_tokens=max_tokens + 2000, system=system,
                                              output_config={"effort": effort},
                                              messages=[{"role": "user", "content": user}])
        except anthropic.APIError as exc:
            raise ChatUnavailable(type(exc).__name__) from None  # provider text can carry request data
        if response.stop_reason == "refusal":
            raise ChatUnavailable("refusal")
        return "".join(block.text for block in response.content if block.type == "text")
    return complete


def gateway_complete(url, token):
    """``complete`` over the chat gateway that sits next to the runtime (harbor/environment/gateway)."""
    def complete(system, user, max_tokens):
        body = json.dumps({"system": system, "user": user, "max_tokens": max_tokens}).encode()
        request = urllib.request.Request(url.rstrip("/") + "/complete", data=body, method="POST",
                                         headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
        try:
            with urllib.request.urlopen(request, timeout=150) as response:
                return json.loads(response.read())["text"]
        except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
            raise ChatUnavailable(type(exc).__name__) from None
    return complete


def backend_from_environment():
    """ORGENV_CHAT_BACKEND: ``scripted`` (default), ``gateway`` (ORGENV_CHAT_URL, ORGENV_CHAT_TOKEN)
    or ``anthropic`` (ORGENV_CHAT_MODEL, ORGENV_CHAT_API_KEY, optional ORGENV_CHAT_BASE_URL)."""
    kind = os.environ.get("ORGENV_CHAT_BACKEND", "scripted")
    if kind == "scripted":
        return ScriptedBackend()
    if kind == "gateway":
        return ModelBackend(gateway_complete(os.environ["ORGENV_CHAT_URL"], os.environ.get("ORGENV_CHAT_TOKEN", "")),
                            label="gateway")
    if kind == "anthropic":
        model = os.environ.get("ORGENV_CHAT_MODEL", "claude-haiku-5-5")
        return ModelBackend(anthropic_complete(model, api_key=os.environ["ORGENV_CHAT_API_KEY"],
                                               base_url=os.environ.get("ORGENV_CHAT_BASE_URL") or None), label=model)
    raise ValueError("unknown ORGENV_CHAT_BACKEND")


# --------------------------------------------------------------------------- the conversation as Alex sees it
class Conversation:
    """What Alex has read and written, kept beside the engine's own record."""

    def __init__(self, seed, backend):
        self.seed, self.backend = seed, backend
        self.shown = {}        # message id -> the text Alex was shown
        self.order = []        # message ids in the order Alex received them
        self.extra = {}        # message id -> a message the layer delivers itself (colleagues' asks)
        self.notes = {}        # colleague -> remarks owed with their next reply
        self.last_from_alex = {}
        self.thread = {}       # colleague -> [(who, text)]
        self.log = []          # everything the layer did, in order

    def persona(self, actor):
        return persona(self.seed, actor)

    def said(self, actor, who, text):
        self.thread.setdefault(actor, []).append((who, text))

    def render(self, message_id, sender, source, *, kind="reply"):
        notes = self.notes.pop(sender, []) if kind == "reply" else []
        full = source + "".join(" " + n for n in notes)
        result = self.backend.write(sender, full, {"persona": self.persona(sender), "kind": kind,
                                                   "replying_to": self.last_from_alex.get(sender) if kind == "reply" else None})
        self.shown[message_id] = result["text"]
        self.order.append(message_id)
        self.said(sender, PERSONAS[sender]["name"], result["text"])
        self.log.append({"kind": "write", "message_id": message_id, "sender": sender, "source": full,
                         "shown": result["text"], "attempts": result["attempts"], "fallback": result["fallback"],
                         **({"problem": result["problem"]} if result.get("problem") else {})})

    def record(self):
        writes = [e for e in self.log if e["kind"] == "write"]
        return {"backend": getattr(self.backend, "label", self.backend.name), "model_calls": getattr(self.backend, "calls", 0),
                "messages_written": len(writes), "fallbacks": sum(e["fallback"] for e in writes),
                "personas": {a: self.persona(a)["mood"] for a in PERSONAS}, "log": deepcopy(self.log)}
