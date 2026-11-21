# LEDGER: waste and selector CPU per sample commit (314df58)

Selector under test: lane-c at 314df58 (public main v8.3), image built from it, run on the kit's 30 sample commits.
Costs come from the kit's scorer rules (busiest worker + 2 s per worker, ms arithmetic); oracle = the exact failing
set packed with our own packer (0 for an empty failing set); waste = ours - oracle. kind is from answers.json.

Totals: simulated 113.060 s (sum of commit costs 113.060 s + 0 misses x 300 s),
oracle 94.351 s, waste 18.709 s. Tests picked 416 vs failing 73.

CPU columns are ms of selector CPU per phase (in-process timers, `time.thread_time()` around each phase, innermost phase
charged, median of 3 container runs pinned to cpus 16,17 under the board flags, via tools/phase_profile.py; the
selector's stdout is byte-identical to a plain run). ld = map load/decode, pp = patch parse and apply, ast = ast.parse +
ast.dump, cmp = compile() (tree and source), walk = pyscope AST walks (ast_diff, bind_sig, scope table walks, default_only),
wm = weakmut + attrrule + tpcds_scope, rep = replay (main side + children CPU; no replay ran on the sample), pk = packing,
oth = rest of rules and main. The (shared) row is the one-off cost (map decode, module imports, interpreter start).
Total CPU is the sum of per-cell medians; median whole-run main-process CPU was 8592 ms. Host contention (another lane
keeps cpus 0-15 busy) inflates absolute CPU on 16,17: a quiet single run measured about 3.2 s for the same selector, so read the
phase shares and the per-commit ranking, not the absolute ms.

| commit | kind | picked | failing | ours s | oracle s | waste s | ld | pp | ast | cmp | walk | wm | rep | pk | oth | cpu ms |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 2haz | code | 0 | 0 | 0.000 | 0.000 | 0.000 | 67 | 3 | 665 | 341 | 355 | 401 | 0 | 0 | 38 | 1869 |
| 2jrq | code | 29 | 8 | 25.454 | 25.454 | 0.000 | 0 | 1 | 87 | 67 | 69 | 58 | 0 | 0 | 28 | 310 |
| 2qn5 | code | 0 | 0 | 0.000 | 0.000 | 0.000 | 0 | 1 | 351 | 98 | 138 | 154 | 0 | 0 | 7 | 748 |
| 2vdj | fixture | 1 | 1 | 2.083 | 2.083 | 0.000 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 6 | 6 |
| 3utz | code | 37 | 37 | 25.454 | 25.454 | 0.000 | 0 | 0 | 66 | 21 | 47 | 40 | 0 | 0 | 16 | 190 |
| 4d63 | code | 273 | 0 | 9.078 | 0.000 | 9.078 | 113 | 0 | 49 | 16 | 16 | 5 | 0 | 0 | 6 | 206 |
| 535z | code | 3 | 1 | 2.671 | 2.596 | 0.075 | 52 | 0 | 35 | 15 | 14 | 15 | 0 | 0 | 11 | 142 |
| 5y72 | code | 3 | 0 | 2.466 | 0.000 | 2.466 | 0 | 0 | 43 | 17 | 34 | 16 | 0 | 0 | 4 | 115 |
| 66ks | code | 0 | 0 | 0.000 | 0.000 | 0.000 | 0 | 0 | 38 | 17 | 18 | 20 | 0 | 0 | 5 | 97 |
| 6vvp | fixture | 1 | 1 | 2.037 | 2.037 | 0.000 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 3 | 3 |
| 6ze8 | code | 2 | 1 | 2.017 | 2.001 | 0.016 | 1 | 0 | 25 | 17 | 18 | 24 | 0 | 0 | 4 | 89 |
| 8y7j | code | 0 | 0 | 0.000 | 0.000 | 0.000 | 0 | 1 | 165 | 112 | 123 | 144 | 0 | 0 | 5 | 551 |
| 986c | multi | 2 | 1 | 2.340 | 2.329 | 0.011 | 0 | 0 | 22 | 0 | 0 | 0 | 0 | 0 | 4 | 27 |
| 9u5k | comment | 0 | 0 | 0.000 | 0.000 | 0.000 | 0 | 0 | 83 | 0 | 0 | 28 | 0 | 0 | 18 | 129 |
| ary6 | code | 10 | 10 | 4.382 | 4.382 | 0.000 | 0 | 1 | 233 | 105 | 87 | 126 | 0 | 0 | 7 | 559 |
| bap8 | comment | 0 | 0 | 0.000 | 0.000 | 0.000 | 0 | 1 | 158 | 0 | 0 | 33 | 0 | 0 | 7 | 198 |
| bj7g | code | 2 | 1 | 2.241 | 2.011 | 0.230 | 0 | 1 | 305 | 110 | 126 | 81 | 0 | 0 | 26 | 648 |
| bvw2 | multi | 1 | 1 | 2.061 | 2.061 | 0.000 | 0 | 0 | 1 | 0 | 0 | 0 | 0 | 0 | 7 | 8 |
| egys | fixture | 1 | 1 | 2.596 | 2.596 | 0.000 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 3 | 3 |
| f4bn | fixture | 1 | 1 | 2.007 | 2.007 | 0.000 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 3 | 3 |
| f4xa | comment | 0 | 0 | 0.000 | 0.000 | 0.000 | 0 | 0 | 24 | 0 | 0 | 2 | 0 | 0 | 9 | 35 |
| fryt | code | 3 | 0 | 2.157 | 0.000 | 2.157 | 0 | 0 | 14 | 16 | 16 | 17 | 0 | 0 | 4 | 67 |
| gbav | code | 33 | 0 | 2.219 | 0.000 | 2.219 | 0 | 0 | 108 | 33 | 29 | 19 | 0 | 0 | 7 | 196 |
| gbhk | fixture | 1 | 1 | 2.061 | 2.061 | 0.000 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 3 | 3 |
| j6uz | code | 2 | 2 | 4.399 | 4.399 | 0.000 | 0 | 1 | 291 | 113 | 123 | 145 | 0 | 0 | 6 | 679 |
| ktz3 | code | 4 | 0 | 2.446 | 0.000 | 2.446 | 53 | 0 | 50 | 34 | 21 | 16 | 0 | 0 | 7 | 181 |
| mthh | fixture | 1 | 1 | 3.016 | 3.016 | 0.000 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 3 | 3 |
| nwnx | fixture | 1 | 1 | 2.092 | 2.092 | 0.000 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 3 | 3 |
| rszg | multi | 4 | 3 | 5.775 | 5.764 | 0.011 | 0 | 1 | 101 | 47 | 36 | 0 | 0 | 0 | 18 | 203 |
| st8f | fixture | 1 | 1 | 2.008 | 2.008 | 0.000 | 0 | 0 | 0 | 112 | 0 | 0 | 0 | 1 | 88 | 200 |
| (shared: maps, imports, start) | | | | | | | 337 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 829 | 1167 |
| **TOTAL** | | **416** | **73** | **113.060** | **94.351** | **18.709** | **623** | **12** | **2913** | **1292** | **1270** | **1344** | **0** | **2** | **1182** | **8639** |

Top waste commits:

- 4d63 (code): +9.1 s over the oracle, 273 picked vs 0 failing
- 5y72 (code): +2.5 s over the oracle, 3 picked vs 0 failing
- ktz3 (code): +2.4 s over the oracle, 4 picked vs 0 failing
- gbav (code): +2.2 s over the oracle, 33 picked vs 0 failing
- fryt (code): +2.2 s over the oracle, 3 picked vs 0 failing
- bj7g (code): +0.2 s over the oracle, 2 picked vs 1 failing

Misses: 0. Simulated total from the kit scorer: 113.060 s.


## CPU diet, before and after (phase totals on the 30 sample commits)

Median of 3 interleaved container runs per side on cpus 16,17 under the board flags, via tools/phase_profile.py (same
timers as above). Before = 314df58, after = lane-c-cpu. The machine was loaded by other lanes, so read the ratios.

| phase | before ms | after ms |
|---|---:|---:|
| map load and decode | 346 | 342 |
| patch parse and apply | 14 | 10 |
| ast.parse | 1035 | 536 |
| ast.dump | 546 | 58 |
| compile() | 700 | 177 |
| pyscope walks | 714 | 184 |
| weakmut | 610 | 47 |
| attrrule + tpcds_scope | 55 | 54 |
| packing | 3 | 4 |
| rest of rules and main | 534 | 382 |
| module imports | 295 | 33 |
| **main process, whole run** | 5170 | 2362 |
