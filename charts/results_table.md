| Concurrency | Achieved | Throughput (tok/s) | Power (W) | Tokens/J | TTFT p50 (ms) | EUR/1M tokens | Load pattern |
|---:|---:|---:|---:|---:|---:|---:|:--|
| 1 | 1 | 103.1 | 216.5 | 0.48 | 25 | 0.1458 | closed-loop |
| 4 | 4 | 216.6 | 229.0 | 0.95 | 67 | 0.0734 | closed-loop |
| 16 | 16 | 696.3 | 168.1 | 4.14 | 196 | 0.0168 | closed-loop |

All values `measured`, not modeled. GPU board power only (sysfs `power1_average`, bus 0000:43:00.0); excludes CPU, RAM, cooling and datacenter PUE. Idle baseline 13.02 W. Tariff 0.25 EUR/kWh.
