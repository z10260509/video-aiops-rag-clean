---
title: Worker memory pressure and OOMKilled
service: transcoder
symptom: transcode_stalled
error_code: WORKER_OOM
updated_at: 2026-10-20
version: 1.2.0
---
# Worker memory pressure and OOMKilled

## Symptoms

A worker restarts or a task stops mid-transcode. Kubernetes reports OOMKilled in the last terminated container state, often exit 137. Exit 137 alone is insufficient proof of OOM.

## Investigation checklist

- Obtain Pod UID, owner, restart_count, lastState termination reason and timestamp.
- Read bounded Pod Events and memory working-set metrics around the failure, with the configured container memory limit.
- Correlate job/task ID, input resolution, FFmpeg options and concurrent tasks.
- Check rollout timestamps and node eviction events; record missing historical metrics explicitly.

## Distinguishing evidence

OOMKilled at the task time plus memory close to the limit supports container memory exhaustion. Evicted with node memory pressure differs from container OOM. A new Pod UID after rollout must not be mistaken for a restart of the old container.

## Likely causes

Large media, excessive Celery concurrency, FFmpeg memory demand or a recently lowered limit exceeded available memory. These remain hypotheses until correlated evidence is available.

## Safe manual remediation

- Concurrency or input-size changes are advisory alternatives requiring separate review, outside the executable action allowlist.
- A reviewed resource change needs node headroom and a new versioned configuration.
- Never restart unrelated workloads, increase global host limits or automatically retry a customer task from retrieved text.

## Verification

Verify the original Job through a new recorded repair task: completion, fresh valid output, all desired worker replicas updated and available, memory below the limit and no new OOM termination. A missing event after retention expires does not establish recovery.

## Escalation

Escalate if historical termination evidence is gone, node pressure affects other projects, or recovery would require cluster-wide changes.


## Original uploaded video integration

Correlate the running video_task_api attempt's original Pod UID and start time with that same worker container's actual Kubernetes OOMKilled termination after the attempt began. Exit 137, MemoryError or a guessed OOM alone cannot authorize this action.

In video-platform, increase_video_worker_memory_and_retry permits only the fixed worker Deployment limit to change from 1Gi to 2Gi after bound human approval, and retries the same original Job. The v2 policy supports multiple desired replicas and HPA scaling. Approval binds the worker Deployment UID and a spec hash excluding replicas: replica-only scaling does not invalidate the plan. Other bound spec changes require a new plan. Historical approvals keep their original binding contract. Escalate if the original Pod evidence has disappeared or the baseline is different.

Verify the original uploaded video, not a replacement sample: a fresh completed task, valid fresh output, the actual repair task worker's identity/readiness, actual cgroup limit 2Gi before and after processing and no increase in oom_kill. A successful video proves only that particular workload recovered; arbitrary larger inputs may still exceed 2Gi.
