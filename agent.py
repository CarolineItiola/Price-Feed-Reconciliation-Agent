"""
The decision loop.

The agent is handed tools and the current state, and then pick its
own next action at every step. There is no fixed sequence. It 
decides source to query, whether to seek corroboration, when 
to commit and when to escalate are all decisions it makes from
what it has observed so far.

The loop itself enforces only structural rules:
  - it stops after MAX_AGENT_STEPS
  - if it runs out of steps without settling, the state is
    escalated rather than left half written

Everything else is the agent's judgement, and every judgement it
makes is recorded with the reason it gave at the time.
"""

import json
import os

from anthropic import Anthropic
from dotenv import load_dotenv

import config
import sources
import state as state_module
import tools
import trust

load_dotenv()


SYSTEM_PROMPT = """You maintain the canonical price of a single \
asset by querying independent sources and deciding which of them \
to believe.

Your sources are not equal, and their failures are not equal:

  finnhub        cheap, 60 calls a minute, timestamps to the
                 second. Your workhorse.
  alphavantage   scarce, 25 calls a day and one per second.
                 Coarse timestamps. When rate limited it returns
                 a success code carrying no price at all.
  warehouse_csv  a local snapshot. Free and instant, but only as
                 current as whatever last wrote to it.

You decide your own next action at every step. There is no
required order, no required number of queries, and no obligation
to query every source.

HOW TO WEIGH WHAT YOU SEE

Recency. A price is only as good as the moment it was true. A
quote can arrive in milliseconds and still carry a price from two
days ago. Every quote comes back with a freshness verdict that
already accounts for whether the venue was trading: an old equity
price on a closed market is correct, an old crypto price never
is. Read that verdict, do not recompute it.

Consistency. Two sources inside tolerance are corroborating each
other. Two sources outside it are in genuine conflict, and one of
them is wrong. Working out which one is your job, and the answer
is not always the majority.

Reliability. Trust scores move as sources behave. A source that
just went stale or returned nothing has earned less of your
belief than one that has been corroborated. But a source being
cheap to query is not a reason to believe it more.

WHAT YOU MUST NEVER DO

Never average conflicting prices. The state store will reject it
outright. There is no midpoint between a right answer and a wrong
one, and a blended number is traceable to nothing.

Never fall back to the last known value because you could not get
a fresh one. If you cannot verify a price now, say so and
escalate. Silence about staleness is worse than admitting it.

Never accept a price you cannot attribute to a source you
actually queried this session.

ESCALATION IS A CORRECT OUTCOME

Flagging a conflict you cannot resolve is a better result than
committing to a price you cannot defend. You are not judged on
committing quickly, or on committing at all. You are judged on
whether the state you leave behind is one a person can trust.

Before each action, reason briefly: what you now hold, what is
still missing, and what the next action is meant to settle."""


def line(text=""):
    print(text)


def summarise(name, args, result):
    """One compact line describing what a tool call did."""
    if name == "query_source":
        who = args.get("source_name", "?")
        if result.get("error"):
            return "     " + who + ": " + result["error"]
        if result.get("status") != "OK":
            return (
                "     " + who + ": " + str(result.get("status"))
                + " - " + str(result.get("detail", ""))[:70]
                + "  trust now " + str(result.get("trust_now"))
            )
        bits = [
            "     " + who + ": " + str(result.get("price")),
            str(result.get("freshness")),
            "trust " + str(result.get("trust_now")),
        ]
        out = "  ".join(bits)
        for comp in result.get("comparisons", []):
            verdict = "agrees" if comp["agree"] else "CONFLICTS"
            out += (
                "\n       vs " + comp["against"] + ": "
                + str(comp["spread_pct"]) + "% " + verdict
            )
        return out

    if name == "accept_canonical":
        if result.get("rejected"):
            return "     REJECTED: " + str(result.get("error"))[:120]
        snap = result.get("state", {})
        return (
            "     canonical set to " + str(snap.get("canonical_price"))
            + " (" + str(snap.get("status")) + ", confidence "
            + str(snap.get("confidence")) + ")"
        )

    if name == "mark_source_unreliable":
        if result.get("error"):
            return "     " + result["error"]
        return (
            "     " + str(result.get("source")) + " trust "
            + str(result.get("trust_before")) + " -> "
            + str(result.get("trust_now"))
            + ("  UNRELIABLE" if result.get("unreliable") else "")
        )

    if name == "flag_for_review":
        return "     escalated to human review"

    if name == "finish":
        return "     session closed"

    return "     " + json.dumps(result, default=str)[:120]


def run_with(kit, opening_note="", verbose=True):
    """
    Run the loop over an already built toolkit.

    scenarios.py uses this entry point so it can inject faults
    into the sources before the agent starts.
    """
    client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    profile_lines = []
    for name in sorted(kit.sources):
        p = config.SOURCE_PROFILES.get(name, {})
        profile_lines.append(
            "  " + name + ": " + p.get("cost", "?")
            + ", " + p.get("limit", "?")
        )

    opening = (
        "Establish the canonical price for " + kit.asset + ".\n\n"
        "Sources available:\n" + "\n".join(profile_lines) + "\n\n"
        "Current state:\n"
        + json.dumps(kit.state.snapshot(), indent=2) + "\n\n"
        "Trust so far:\n"
        + json.dumps(kit.ledger.snapshot(), indent=2)
    )
    if opening_note:
        opening += "\n\n" + opening_note

    messages = [{"role": "user", "content": opening}]

    for step in range(1, config.MAX_AGENT_STEPS + 1):
        kit.step = step

        response = client.messages.create(
            model=config.MODEL,
            max_tokens=config.MAX_TOKENS,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=tools.TOOL_SCHEMAS,
            messages=messages,
        )

        thoughts = []
        calls = []
        for block in response.content:
            if block.type == "text":
                if block.text.strip():
                    thoughts.append(block.text.strip())
            elif block.type == "tool_use":
                calls.append(block)

        if verbose:
            line()
            line("  step " + str(step))
            for t in thoughts:
                for para in t.split("\n"):
                    if para.strip():
                        line("     " + para.strip())

        messages.append(
            {"role": "assistant", "content": response.content}
        )

        if not calls:
            # The agent replied without acting. Nothing more
            # will happen, so stop rather than loop on silence.
            if verbose:
                line("     no action taken, ending loop")
            break

        results = []
        for call in calls:
            args = dict(call.input)
            result = kit.call(call.name, args)

            if verbose:
                line("   -> " + call.name + "(" + ", ".join(
                    str(k) + "=" + str(v)[:40]
                    for k, v in args.items()
                ) + ")")
                line(summarise(call.name, args, result))

            results.append({
                "type": "tool_result",
                "tool_use_id": call.id,
                "content": json.dumps(result, default=str),
            })

        messages.append({"role": "user", "content": results})

        if kit.finished:
            break

    # Structural guarantee: never leave the state half written.
    if not kit.finished and kit.state.status != state_module.QUARANTINED:
        reason = (
            "step limit of " + str(config.MAX_AGENT_STEPS)
            + " reached without settling on a canonical price. "
            "Escalating rather than leaving the state unresolved."
        )
        kit.flag_for_review(reason)
        if verbose:
            line()
            line("   -> step limit reached, forced escalation")

    return kit


def run(asset=None, opening_note="", verbose=True):
    """Build everything fresh and run one session."""
    asset = asset or config.DEFAULT_ASSET
    registry = sources.build_sources()
    ledger = trust.TrustLedger(list(registry))
    canonical = state_module.CanonicalState(asset)
    kit = tools.AgentTools(asset, registry, canonical, ledger)
    return run_with(kit, opening_note, verbose)


if __name__ == "__main__":
    kit = run()
    print("")
    print("=" * 60)
    print("  FINAL STATE")
    print("=" * 60)
    print(json.dumps(kit.state.snapshot(), indent=2))
    print("")
    print("  TRUST")
    print(kit.ledger.report())
    print("")
    print("  AUDIT TRAIL")
    print(kit.state.audit())
    print("")