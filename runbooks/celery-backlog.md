---
title: Celery queue backlog and unavailable consumers
service: transcoder
symptom: queue_backlog
error_code: CELERY_QUEUE_BACKLOG
updated_at: 2026-10-20
version: 1.2.0
---
# Celery queue backlog and unavailable consumers

## Symptoms

Pending jobs increase, video_queue_depth rises and enqueue-to-start delay grows. Queue depth reports waiting tasks only, excluding reserved, executing and priority queues.

## Investigation checklist

- Query the single trusted exporter for celery queue depth in a bounded window.
- Compare task arrival/completion rates, worker readiness, Celery consumer queue and active concurrency.
- Read bounded worker logs for broker reconnects, time limits and failed deliveries.
- Check CPU/memory and task duration; correlate with a recent deployment or unusually large media.

## Distinguishing evidence

A growing waiting queue with ready consumers and long successful transcodes suggests capacity pressure. Zero available consumers suggests scheduling/configuration failure. Redis connection errors indicate broker connectivity rather than simply insufficient concurrency.

## Likely causes

Tasks arrive faster than processing capacity, consumers use the wrong queue, a worker is unavailable, or slow FFmpeg tasks occupy all slots. Queue depth alone cannot select the cause.

## Safe manual remediation

- Operator reviews consumer configuration and workload rate before changing the video-platform worker's concurrency.
- Reduce only reviewed incoming workload; never purge Redis, discard jobs or scale unrelated projects.
- Any replica/concurrency adjustment requires a separately reviewed change and memory capacity checks.

## Verification

After the reviewed change, require queue depth to decrease, waiting jobs to start and a fresh sample to complete without increased failures or OOM. Verify over a bounded interval, not one snapshot.

## Escalation

Escalate if exporter data is stale, broker state is unknown, queue does not drain or duplicate delivery is observed. Reserved tasks and priority queues require additional evidence.

## Current policy scope

There is no executable queue scaling/concurrency repair action in the current allowlist. These recommendations require separate operator review. Existing worker HPA may manage replicas; never invent a repair action from this document.
