---
title: Token usage
description: How Citadel Agent limits the input tokens it sends to the model, and how to measure usage per request
---

## Why it matters

A model request carries the system instructions, the schema of every available tool, the conversation so far and the user's message. A turn with tool calls makes several requests, and each one repeats everything. Left alone, the tool schemas alone cost about 5,000 input tokens on every request for an operator.

Three mechanisms keep requests small.

## Per-turn tool selection

The agent starts every turn with a small core of tools and adds the groups the message needs.

| Group         | Tools added                                                                                                       | Approximate size |
|---------------|-------------------------------------------------------------------------------------------------------------------|------------------|
| Core, always  | Documentation search, gateway overview, models, contract listing and description, prepare, execute, change status | 1,200 tokens     |
| `access`      | Draft access contract                                                                                             | 900 tokens       |
| `calling`     | Client code generation, smoke test                                                                                | 500 tokens       |
| `diagnostics` | Error explanation, gateway logs, smoke test, preflight                                                            | 900 tokens       |
| `usage`       | Usage report, backend summary                                                                                     | 400 tokens       |
| `rotation`    | Start and advance key rotation                                                                                    | 400 tokens       |
| `backend`     | Draft backend change, backend summary                                                                             | 800 tokens       |
| `alerts`      | Draft alert rules                                                                                                 | 450 tokens       |

A group loads when the message contains words for the task, such as "onboard", "429", "usage last week", "rotate" or "publish". The groups used in the last two turns stay loaded, and a short reply such as "yes, go ahead" keeps the topic. A message that names no task gets the core only, which is enough to answer questions and to approve a prepared change.

Rules live in `src/citadel_agent/tool_routing.py`, and tests pin them. If the model lacks a tool, ask for the task in plain words. The agent's instructions tell it to say so.

## History compaction

Agent Framework compaction keeps the conversation inside a budget of about 16,000 tokens. Old tool results collapse into short summaries first, and the oldest turns drop only if that is not enough. Prepared changes put the change ID and approval hash at the top of the tool output, so a summary never loses them.

A single tool result is capped at 8,000 characters.

## Measure it

The chat prints a line after every answer:

```text
tokens: 6,981 in, 144 out (1 model call, largest request 6,981 in)
```

Every request is also appended to `.citadel-agent/usage.jsonl` with counts only, never prompts or answers:

```json
{"ts":"2026-10-13T12:00:00+00:00","session":"...","call":1,"input":3235,"output":60,"cached":0,"tools":9,"domains":[],"history":null}
```

Summarize the log:

```bash
uv run citadel-agent usage
```

Look at the largest request and the 90th percentile, not the total. The total grows with the number of tool calls in a turn, while the largest request shows what each model call costs.

## Reference measurements

On the development hub, with the routing in place:

| Turn                                        | Tools sent | Largest request |
|---------------------------------------------|------------|-----------------|
| "What is Citadel?" (documentation question) | 9          | 3,235 tokens    |
| "Why do I get a 429?" (diagnostics)         | 13         | 6,602 tokens    |
| Follow-up about a 403 (same topic)          | 13         | 6,981 tokens    |

Before routing, every request carried at least 5,150 tokens of tool schemas, so the first request of the same turns was roughly twice as large. Skill instructions and resources loaded during a turn account for much of the rest.

## Tuning

* Lower `COMPACTION_WINDOW_TOKENS` in `src/citadel_agent/agent.py` to compact earlier.
* Add keywords to `tool_routing.py` when a task is routed to the wrong group, and add a case to `tests/test_tool_routing.py`.
* Keep tool descriptions short. They are part of every request that includes the tool.
