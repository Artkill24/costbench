| Model | Concurrency | Achieved | Throughput (tok/s) | Power (W) | Tokens/J | TTFT p50 (ms) | EUR/1M tokens |
|:--|---:|---:|---:|---:|---:|---:|---:|
| Qwen2.5-Coder-14B Q4_K_M | 1 | 1 | 57.3 | 226.1 | 0.253 | 56 | 0.2741 |
| Qwen2.5-Coder-14B Q4_K_M | 4 | 4 | 116.6 | 236.9 | 0.492 | 170 | 0.1410 |
| Qwen2.5-Coder-14B Q4_K_M | 16 | 16 | 411.6 | 184.7 | 2.229 | 571 | 0.0312 |
| Qwen2.5-Coder-7B Q4_K_M | 1 | 1 | 103.1 | 216.5 | 0.476 | 25 | 0.1458 |
| Qwen2.5-Coder-7B Q4_K_M | 4 | 4 | 216.6 | 229.0 | 0.946 | 67 | 0.0734 |
| Qwen2.5-Coder-7B Q4_K_M | 16 | 16 | 696.3 | 168.1 | 4.142 | 196 | 0.0168 |

All values `measured`, not modeled. Closed-loop load with achieved concurrency verified per run. GPU board power only (sysfs `power1_average`, bus 0000:43:00.0); excludes CPU, RAM, cooling and datacenter PUE. Tariff 0.25 EUR/kWh.
