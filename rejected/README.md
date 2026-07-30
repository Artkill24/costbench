# Rejected measurements

## costbench_qwen14b-q4km-closedloop_20260730T110028Z.json — INVALID

Labelled 14B, actually measured the 7B.

The 14B server failed to bind port 8081 (see srv_14b log: "couldn't bind
HTTP server socket") because a previous llama-server instance was still
holding it. The benchmark's readiness check confirmed *a* server was
answering, but never verified *which model* it had loaded, so the run
silently re-measured Qwen2.5-Coder-7B.

Detected because throughput matched the 7B to one decimal place
(103.1 tok/s at concurrency 1) — a 14B has roughly twice the weights to
read per token and cannot match that.

Kept here rather than deleted: the failure mode is instructive, and the
fix (verify the loaded model, not just server liveness) is in demo.sh.
