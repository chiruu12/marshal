---
name: performance-optimization
description: |
  Systematic performance optimization workflow: measure, identify bottlenecks, fix,
  verify improvement. Use when user reports slowness, wants to optimize, mentions
  "performance", "slow", "latency", "bundle size", or "page load". Not for premature
  optimization — only when there's a measured problem.
---

# Performance Optimization

Measure before optimizing. Optimize the bottleneck, not what's easy to change. Verify the improvement with numbers, not vibes. Performance work without measurement is guessing.

## When to Use

- User reports observable slowness or latency
- Metrics show degradation (response time, page load, bundle size)
- Preparing for scale (known upcoming traffic increase)
- **NOT** when there's no measured problem — that's premature optimization
- **NOT** for readability refactors disguised as "perf improvements"

## Process

### 1. Establish baseline

Measure BEFORE changing anything:

```bash
# Web: Core Web Vitals
# API: Response time p50/p95/p99
# Build: Bundle size, build time
# Database: Query execution time, explain plans
```

Record specific numbers. "It's slow" is not a baseline. "P95 response time is 2.3s, target is 500ms" is.

### 2. Identify the bottleneck

Profile to find WHERE time is spent — don't guess:

- **Frontend**: Browser DevTools Performance tab, Lighthouse, bundle analyzer
- **Backend**: Profiler (cProfile, pprof, perf), APM traces, slow query logs
- **Database**: EXPLAIN ANALYZE on slow queries, connection pool stats
- **Network**: Waterfall chart, DNS/TLS/TTFB breakdown

**The bottleneck is the one thing that, if fixed, moves the metric.** Usually it's one of: N+1 queries, missing index, uncompressed assets, synchronous blocking, or memory allocation churn.

### 3. Fix the bottleneck

Apply the fix for the identified bottleneck. Common fixes:

| Bottleneck | Fix |
|-----------|-----|
| N+1 queries | Eager loading / batch queries |
| Missing index | Add index on filter/join columns |
| Large bundle | Code splitting, tree shaking, lazy loading |
| Synchronous blocking | Async/parallel execution |
| Uncompressed assets | gzip/brotli, image optimization |
| Excessive re-renders | Memoization, virtualization |
| Cold starts | Connection pooling, pre-warming |

### 4. Verify improvement

Re-run the same measurement from step 1. Compare numbers:

- Did the metric improve?
- By how much? (absolute and percentage)
- Any regressions in other metrics?

If improvement is less than 10%, the bottleneck was misidentified. Re-profile.

### 5. Document the threshold

Set a threshold that triggers future investigation:

> "API response time p95 must stay under 500ms. If it exceeds this, investigate."

## Common Rationalizations

| Shortcut | Why It Fails |
|----------|-------------|
| "I know what's slow, I don't need to profile" | Developers are wrong about bottlenecks ~80% of the time. Profile. The real bottleneck is almost never where you think it is. |
| "Let's optimize everything while we're at it" | Optimizing non-bottlenecks doesn't improve the metric. It adds complexity for zero user-facing benefit. Fix the ONE thing that moves the needle. |
| "It feels faster after my change" | Feelings aren't measurements. A/B test, before/after numbers, or it didn't happen. Confirmation bias makes everything "feel faster" after you touch it. |
| "We should optimize this before it becomes a problem" | Premature optimization is the root of all evil. Optimize when you have evidence of a real problem, not a hypothetical future one. |

## Red Flags

- Optimization work started without baseline numbers — can't verify improvement
- Multiple optimizations applied at once — can't attribute improvement to any specific change
- "It feels faster" as the only evidence — no objective measurement
- Optimizing code that runs once per request when the DB query takes 95% of the time — wrong bottleneck

## Verification Checklist

- [ ] Baseline measurement recorded before any changes
- [ ] Bottleneck identified through profiling (not guessing)
- [ ] Fix targets the identified bottleneck specifically
- [ ] Post-fix measurement shows quantifiable improvement (>10%)
- [ ] No regressions in other metrics
- [ ] Performance threshold documented for future monitoring

## Anti-patterns

- **DO NOT** optimize without measuring first — you're guessing, not engineering
- **DO NOT** apply multiple optimizations simultaneously — you can't attribute improvement
- **DO NOT** optimize non-bottleneck code — it adds complexity without moving the metric
- **DO NOT** accept "feels faster" as verification — use numbers
