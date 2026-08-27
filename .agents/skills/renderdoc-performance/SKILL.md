---
name: renderdoc-performance
description: Diagnose GPU frame cost and suspicious RenderDoc events using timings, counters, pass structure, and debug messages. Use for bottlenecks, expensive draws, overdraw hypotheses, counter analysis, and frame performance summaries.
metadata:
  replaylab-category: performance
  replaylab-read-only: "true"
  replaylab-min-successful-tools: "1"
  replaylab-required-any-tools: get_action_timings fetch_counters
  replaylab-version: "1"
allowed-tools: get_frame_summary get_draw_calls enumerate_counters fetch_counters get_action_timings get_debug_messages
---

# Performance analysis

1. Establish frame/pass structure before ranking events. Do not treat event order as cost.
2. Prefer measured action timings and hardware counters. Label unsupported or noisy measurements explicitly.
3. Correlate expensive events with draw dimensions, pass structure, counter evidence, and debug messages.
4. Separate observed bottlenecks from optimization ideas; quantify evidence where possible.
5. Recommend the smallest confirmatory capture or counter set rather than broad speculative changes.
