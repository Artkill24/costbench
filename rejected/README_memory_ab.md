# Rejected: agent memory A/B run 20260802T210522Z

**Reported 69.2% fewer tokens with warm memory. The number is meaningless.**

The agent never answered the question. In both the cold and the warm round,
all three tasks returned variants of:

    "No information found about 'tokens_per_joule' in the provided files."

So the "saving" measured only this: with memory, the agent reached the same
failure in one step instead of two or three. It learned to give up faster.

## Why it failed

Three defects in the tools, all mine:

1. **`grep` did not handle glob paths.** The model passed `proofs/*.json`;
   `os.walk` on that literal path found nothing and returned zero hits every
   time. The system prompt told the model not to use wildcards. It used them
   anyway — which means the fix belonged in the code, not in the prompt.

2. **`grep` searched binary files**, including the agent's own memory
   database. It was reading back its own earlier answers as if they were
   source data.

3. **Multi-term regexes cannot match indented JSON.** The model generated
   `system_tok_s.*concurrency.*16`, which requires all three on one line.
   In a pretty-printed JSON file each field sits on its own line.

## What was done

All three fixed in `agent.py`: glob paths resolved, binaries excluded, and a
fallback that retries with the first term when a `.*` pattern finds nothing
and says so. A `read_json` tool was added afterwards so a value can be
reported with the record it belongs to.

The corrected run is `proofs/memory_ab_20260803T024708Z.json`: 28.5% fewer
tokens, with answers that are actually correct in two of three tasks.

Kept rather than deleted, for the same reason as the other rejected
measurement: the failure mode is the instructive part.
