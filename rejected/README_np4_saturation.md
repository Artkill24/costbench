# Rejected: costbench_qwen7b-q4km_20260730T184015Z.json

**Five concurrency levels, all closed-loop, achieved concurrency verified —
and still not comparable to the published runs.**

    conc   tok/s      W
       1   103.2  216.6
       2   166.4  227.7
       4   216.7  229.7
       8   261.8  229.2
      16   261.6  228.8

Throughput saturates at 262 tok/s from concurrency 8 onward, and power stays
flat at 229 W. The published run reports 696 tok/s and 168 W at concurrency
16.

## Why

The server was started with `-np 4`. Beyond four in-flight requests the rest
queue: the *offered* concurrency was 16, but the engine was serving 4. So
concurrency 8 and 16 are really concurrency 4 measured twice.

This is why the power drop at high batch does not appear here. It is a
property of a genuinely full batch, not of the request rate.

## What it exposes

`achieved_concurrency` did not catch this. It counts requests in flight on
the client side, which was correctly 16 — it cannot see how many slots the
server actually had. A complete check would also read the server's parallel
slot count and compare.

Kept because the failure is a subtle one: every validity marker in the file
says the run is sound, and it is — it just measures a different thing than
its label claims.
