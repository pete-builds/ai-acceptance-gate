# How this differs from Promptfoo

[Promptfoo](https://www.promptfoo.dev/) is a general evaluation and red-team platform: a matrix of prompts, providers and tests, dozens of assertion types including model-graded ones, a web viewer, and a generator of adversarial inputs. It is the better tool for comparing models, tuning prompts and red teaming.

This tool does one thing: decide whether a deployed chat endpoint may ship. It asks the real endpoint, through its real session, CSRF and streaming path, the questions the owner wrote down, and exits with a code CI can act on. Everything below follows from that narrower job.

Promptfoo facts here were checked against its docs and source on 2026-10-11 (main at `80142826`). Links are next to each claim.

## Where they differ

| | ai-acceptance-gate | Promptfoo |
|---|---|---|
| Unit under test | One deployed application endpoint. Nothing else. | Usually a prompt × provider matrix; a deployed endpoint through the [HTTP provider](https://www.promptfoo.dev/docs/providers/http/) or a custom provider. |
| What a case says | Four first-class expectations: an expected answer, prohibited claims, a refusal, a handoff to a person. | Generic assertions (`icontains-any`, `regex`, `javascript`, `llm-rubric` and more, any of them negatable with `not-`) that you compose into those meanings. [Assertions](https://www.promptfoo.dev/docs/configuration/expected-outputs/) |
| Repeated runs | `runs` per case, so a prompt-injection case can need 5 clean runs while a price question needs 3. `--min-runs` raises the floor and never lowers it. Runs go round-robin and a case that flips is named. | `--repeat N` or `evaluateOptions.repeat`, one count for the whole eval; there is no per-test repeat. Each repeat is its own result row, and with the default 100% pass threshold every repeat has to pass. A repeat-stability summary flags unstable groups but does not change the exit code. [Reference](https://www.promptfoo.dev/docs/configuration/reference/), [source](https://github.com/promptfoo/promptfoo/blob/801428268b5e421b2f6994332f6a06f86b27bb1f/src/util/eval/repeatStability.ts#L74) |
| Exit codes | `0` pass, `1` a wrong reply, `2` the suite is invalid, `3` some run got no reply to grade. A dead endpoint never looks like a regression, and never looks like a pass. | `100` when any test fails or the pass rate is under `PROMPTFOO_PASS_RATE_THRESHOLD`; `PROMPTFOO_FAILED_TEST_EXIT_CODE` changes the number. Provider errors count against the pass rate and exit with the same code, so "untested" and "wrong" are one signal. [CLI](https://www.promptfoo.dev/docs/usage/command-line/), [source](https://github.com/promptfoo/promptfoo/blob/801428268b5e421b2f6994332f6a06f86b27bb1f/src/node/doEval.ts#L1271-L1286) |
| Getting a reply | Each adapter owns the whole handshake: open a session, or fetch a page for a CSRF cookie and echo it, then parse the stream. A stream that ends without its finish event is an error, not a short answer. | The HTTP provider does not parse Server-Sent Events itself; you collect the `data:` chunks in `transformResponse`. Sessions come from `sessionParser` or `transformVars`, and tokens from an `auth` block. The docs show no general "fetch a cookie, then call" step, so a double-submit CSRF endpoint needs a custom provider. [HTTP provider](https://www.promptfoo.dev/docs/providers/http/) |
| Grading | Deterministic substring and regex checks only. The same reply always gets the same verdict, so a flip between runs is the model's and never the grader's. | Deterministic and model-graded assertions, with per-assertion `weight` and a test-level `threshold`. Model grading catches things string checks cannot, at the price of a second nondeterministic model in the verdict. |
| Retries | Transport failures only (429, 5xx, a dropped connection). A wrong answer is never retried. | Repeats are separate rows; retry behaviour for provider errors is the provider's. |
| Outputs | One JSON schema (`ai-acceptance-gate/result/v1`) and JUnit XML. | csv, txt, json, jsonl, yaml, html, xml and `junit.xml`, plus a web viewer. [CLI](https://www.promptfoo.dev/docs/usage/command-line/) |
| Adversarial inputs | None generated. You write the cases you will defend. | `promptfoo redteam` generates hundreds of attack inputs from plugins and strategies. [Red team](https://www.promptfoo.dev/docs/red-team/quickstart/) |

## What this tool deliberately does not do

It does not compare models, score prompts, grade with a model, generate attacks, or host a viewer. A pass is a floor, not proof: the checks catch a missing fact and the wrong claims someone thought to name, and cannot catch an invented claim nobody listed. Read the replies in the JSON result before a first release.

## Using Promptfoo as the underlying engine

Nothing in the suite format is tied to this runner. If you already run Promptfoo, or want its model-graded checks or viewer, it can do the asking and grading while this suite stays the source of truth. Three pieces would be needed. None of them ship yet.

**1. A provider that reuses these adapters.** Promptfoo calls a Python provider's `call_api(prompt, options, context)` and expects `{"output": ...}` or `{"error": ...}` back. [Python provider](https://www.promptfoo.dev/docs/providers/python/) A sketch, untested:

```python
# promptfoo_provider.py, used as  id: file://promptfoo_provider.py
from acceptance_gate.adapters import TransportError, build
from acceptance_gate.spec import Target

def call_api(prompt, options, context):
    target = Target.model_validate(options["config"]["target"])
    adapter = build(target)
    try:
        return {"output": adapter.ask([], prompt).text}
    except TransportError as exc:
        return {"error": str(exc)}
    finally:
        adapter.close()
```

Scripted history would travel in the test's vars, or through Promptfoo's own `_conversation`. [Chat](https://www.promptfoo.dev/docs/configuration/chat/)

**2. A compiler from the suite to `promptfooconfig.yaml`.**

| Suite field | Promptfoo |
|---|---|
| `ask` | `vars.prompt` |
| `must_include_any` | `icontains-any` |
| `must_include_all` | `icontains-all` |
| `prohibited_claims` | one `not-icontains` per claim; suite-wide ones in `defaultTest` |
| `prohibited_patterns` | `not-regex` (Promptfoo's regex is JavaScript, so case-insensitivity has to be translated, not copied) |
| `expect: handoff` | `icontains-any` over the handoff markers |
| `expect: refuse` | a `javascript` assertion: a decline pattern or a handoff marker, and the word cap |
| `max_words`, cut-off replies | `javascript` |
| `runs` | no per-test equivalent: run one eval per distinct `runs` value with `--repeat`, or repeat everything at the highest count and pay for it |

**3. A wrapper for the exit code.** Run `promptfoo eval -o result.json`. If any test failed, exit 1; if none failed but any errored, exit 3. Promptfoo's own exit code cannot tell those apart.

What you would give up: per-case run counts, the distinct "untested" exit code, and refusals and handoffs as first-class kinds instead of JavaScript. What you would gain: model-graded checks, the viewer, red-team generation, and Promptfoo's provider catalogue.
