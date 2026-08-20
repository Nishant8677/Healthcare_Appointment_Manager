# ADR 0007 — LLM summaries and medication reminders

**Status:** Accepted · **Phase:** 6

Two generated summaries — a triage brief for the doctor before the visit, a plain-language
write-up for the patient after it — plus the medication reminders that follow a prescription.
The design question throughout is not "how do we call a model" but "what happens when the
model is slow, offline, or wrong".

---

## 1. A summary is requested inside the transaction and generated outside it

**Decision.** Confirming a booking creates a `pending` `ai_summaries` row in the same
transaction as the appointment. Recording a visit does the same for the post-visit summary. A
background worker fills them in afterwards.

**Why.** This is the requirement stated plainly: *a booking must never fail because the model
did*. Calling the model inline would put an external service with multi-second latency and its
own outages directly on the path of a patient booking an appointment — the model becomes a
dependency of the thing that matters most, in exchange for a convenience that matters least.

It is the same shape as the notification outbox (ADR 0005), for the same reason: the *intent*
is durable and transactional, the *fulfilment* is allowed to fail and retry. Verified against
a running server with a deliberately invalid API key — booking returned `200`, the visit
recorded `201`, emails still went out, and only the summary was delayed.

## 2. The response is schema-constrained, not parsed hopefully

**Decision.** Requests go through the official SDK's structured-output support with a pydantic
model as the output contract. There is no JSON parsing step and no salvage of malformed text.

**Why.** The obvious implementation — ask for JSON in the prompt, `json.loads` the reply, retry
on failure — treats a solved problem as an open one. Constraining the output at the API level
means "the model returned something unparseable" is not a failure mode the application has to
carry code for. What remains is the failure modes that are genuinely irreducible: the provider
being unreachable, and the model declining.

A validator still narrows `urgency` to the three permitted values, because the prompt asks for
"Low / Medium / High" and the database stores lower case — a small normalisation that belongs
next to the schema rather than at the call site.

## 3. Patient symptom text is treated as untrusted input

**Decision.** The system prompt states that the symptom block is patient-written data to
summarise, never instructions to follow, and the text is delimited. A test asserts the framing
is actually sent, not merely written down.

**Why.** Symptoms are free text typed by a stranger and fed to a model whose output a doctor
reads. That is a prompt-injection surface, and the consequence of ignoring it is not a broken
page but a fabricated clinical brief. The defence is cheap and the failure is expensive.

## 4. Medication reminders come from structured fields, never from generated text

**Decision.** `times_per_day` and `duration_days` are typed by the doctor, constrained by
database check constraints, and drive the reminder schedule directly. The LLM writes the
patient's *explanation* of the schedule; it never determines the schedule.

**Why.** This is the single most consequential separation in the phase. A dosing schedule
parsed out of a model's prose is a medication error waiting to happen — and it would be an
error nobody notices, because the prose would read perfectly well. The model is used where
being wrong is survivable (wording) and kept away from where it is not (timing and dose).

**A defect this surfaced.** The first implementation skipped dose slots that had already
passed today, so a course prescribed at 4pm produced fewer reminders than the patient owed —
and *how many fewer depended on the time of day the code ran*. Tests caught it as a flaky
count. The rule now counts doses rather than days: five days at three times a day is fifteen
reminders whenever it is prescribed, running a little further into the calendar if the first
day is half gone.

Long courses are capped at `MEDICATION_REMINDER_MAX_DAYS`, and the cap is logged rather than
applied silently — a year-long prescription would otherwise queue thousands of messages.

Dose times are spread across a waking window (three a day becomes 08:00 / 14:00 / 20:00), on
the grounds that a reminder a patient can predict is a reminder they act on.

## 5. A missing summary is reported as missing, not as empty

**Decision.** The endpoint returns the summary's `status` and an `unavailable_reason`. Pending
says "still being prepared"; failed says "could not be generated. The clinical record is
unaffected."

**Why.** `null` collapses three different situations — not ready yet, permanently failed, and
nothing to say — into one. A doctor deciding whether to wait ten seconds or proceed without it
needs to know which. The explicit note that the clinical record is unaffected matters too: the
notes and prescription are the record, and the generated text is commentary on it.

## 6. A refusal is terminal; an outage is retried

**Decision.** `stop_reason: "refusal"` marks the summary failed immediately. Connection
errors, timeouts and rate limits retry with growing backoff.

**Why.** They are different events wearing the same shape. A refusal is a decision about *this
request*, and it will be reached again identically — retrying only delays the same outcome
while spending money. An outage is about the moment, not the request. Note that a refusal
arrives as a normal `200` response, so it has to be checked for explicitly rather than caught.

## 7. The default provider is a stub, and it announces itself

**Decision.** `LLM_PROVIDER` defaults to `stub`, which returns structurally valid text with
every field prefixed `[stub]`. Selecting `anthropic` without an API key fails at startup.

**Why.** Development and tests need the machinery exercised without a key, a bill, or a
network. But a stub that produced plausible-looking clinical text would be genuinely dangerous
if it ever reached a screen, so it is unmistakable on sight. Failing loudly on a missing key
is the other half: silently serving canned text as though it were a clinical summary is the
worst outcome available here.

## 8. Prompts are versioned constants stored with their output

**Decision.** `PRE_VISIT_PROMPT_VERSION` / `POST_VISIT_PROMPT_VERSION` are constants, recorded
on every summary row along with the model id.

**Why.** The generated text becomes part of a clinical record. Six months on, "why does this
brief say that" has to be answerable, and it only is if the exact prompt and model behind it
are known. It also makes prompt changes measurable rather than a matter of impression.

## 9. One polling loop, used twice

**Decision.** The notification worker's loop was extracted into `PollingWorker` and the summary
worker reuses it.

**Why.** Both jobs have the same shape — open a session, do one bounded pass, close, wait — and
the loop is where the subtle requirements live: survive a failing pass, shut down cleanly,
never let one error end the process. Writing that twice would mean maintaining two versions of
the same careful code, and the second copy is always the one that quietly lacks a fix.
