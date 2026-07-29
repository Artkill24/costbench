| Concurrency | Achieved | Throughput (tok/s) | Power (W) | Tokens/J | TTFT p50 (ms) | EUR/1M tokens | Load pattern |
|---:|---:|---:|---:|---:|---:|---:|:--|
| 1 | — | 103.2 | 216.4 | 0.48 | 25 | 0.1456 | wave-barrier |
| 4 | — | 216.6 | 228.8 | 0.95 | 64 | 0.0733 | wave-barrier |
| 16 | — | 694.0 | 167.8 | 4.14 | 184 | 0.0168 | wave-barrier |

All values `measured`, not modeled. GPU board power only (sysfs `power1_average`, bus 0000:43:00.0); excludes CPU, RAM, cooling and datacenter PUE. Idle baseline 13.02 W. Tariff 0.25 EUR/kWh.
