---
title: Redis DNS and broker connectivity failures
service: transcoder
symptom: transcode_stalled
error_code: REDIS_CONNECTIVITY
updated_at: 2026-10-20
version: 1.2.0
---
# Redis DNS and broker connectivity failures

## Symptoms

Celery reports Redis connection refused, name resolution failure or broker timeout. Jobs may remain pending while consumers reconnect.

## Investigation checklist

- Read bounded worker broker errors and the affected timestamp; redact broker passwords and URLs.
- Operator compares the configured Redis host, port, database and consumer queue with the intended namespace/service.
- Check Redis readiness, endpoints and recent restarts using authorized read-only access.
- Correlate queue backlog and whether another worker uses the same broker successfully.

## Distinguishing evidence

DNS name resolution errors support a hostname/service discovery problem. Connection refused suggests a reachable host without a listening service. Timeout may be network policy or overload. Current Redis health cannot prove its health during the incident.

## Likely causes

Wrong broker DNS/port, Redis restart, unavailable endpoints, network restrictions or transient overload. Evidence should discriminate rather than treating every backlog as Redis failure.

## Safe manual remediation

- Operator reviews the bounded original-video broker repair below. Failed verification is uncertain and requires investigation, with no automatic rollback or repeated patch.
- Preserve queued tasks; never flush Redis, purge queues or replay jobs automatically.
- Changes require separate bound authorization and must target the namespace allowed by the matching policy.

## Verification

Require a fresh broker connection, the correct queue consumer and a new completed task for the original Job with fresh valid output; monitor reconnect errors and backlog drain. Keep before/after timestamps and identities.

## Escalation

Escalate if service/endpoints cannot be read with current RBAC, credentials may be wrong, or recovery would affect other applications sharing Redis.


## Original uploaded video integration

A committed video_task_api record with an actual Redis name resolution or connection-refused exception and the matching failed Job/task supports REDIS_CONNECTIVITY. Queue backlog alone is insufficient.

In video-platform, restore_video_redis_and_retry permits only the fixed worker and backend Deployments to use redis://redis:6379/0, then retries the exact original failed uploaded video. Bind the worker Deployment UID and v2 spec hash excluding replicas, backend Deployment UID/resourceVersion, failed task, codec and authenticated human approval. Multiple worker replicas and HPA scaling are supported. Original failed Pod identity is historical evidence, distinct from Deployment approval binding; a replacement Pod need not retain the old UID. Historical approvals keep their original contract. Do not edit unrelated services or delete queues. Recovery requires every desired replica in both rollouts updated and available, both fixed URLs restored, and a fresh completed outcome plus valid output for the original Job. If Redis itself remains unavailable, report uncertain; do not automatically repatch.
