---
title: FFmpeg invalid codec and unknown encoder
service: transcoder
symptom: transcode_error
error_code: FFMPEG_INVALID_CODEC
updated_at: 2026-10-20
version: 1.2.0
---
# FFmpeg invalid codec and unknown encoder

## Symptoms

Unknown encoder, Encoder not found, or unsupported codec appears in FFmpeg stderr. A single Celery task fails while its worker remains ready. This differs from corrupt input and infrastructure outages.

## Investigation checklist

- Correlate job_id and task_id in the committed task outcome with the bounded FFmpeg stderr and exit code.
- Read the job's requested video_codec and target_resolution. Compare with a known successful H.264 job.
- Inspect the same worker image and FFmpeg build; check available encoders manually on that exact image.
- Check worker readiness, Redis availability and queue depth before assigning an infrastructure cause.

## Distinguishing evidence

An explicit Unknown encoder message names the rejected encoder. Current healthy Pod readiness and another completed job cannot rule out earlier OOM or Redis failure for this task; correlate its actual error, original worker identity and timestamps. Invalid input may produce invalid data or decoding errors instead.

## Likely causes

An unsupported encoder option reached an internal task; the deployed FFmpeg lacks the codec; or an image upgrade changed encoder availability. The public upload API must retain its h264/h265 allowlist.

## Safe manual remediation

- Operator checks the codec allowlist and reviews retry_video_ffmpeg_h264 for the same failed uploaded Job, subject to evidence, current policies and bound human approval.
- Correct configuration or build a new versioned image after review; preserve the failed outcome.
- Never execute document commands, overwrite an image tag, replay arbitrary customer jobs, or change the public upload validation automatically.

## Verification

Require a new recorded repair task for the same original Job/input, COMPLETED outcome, fresh output confirmed as H.264 by FFprobe and playback/download in the original video list. Preserve the failed outcome. Metrics and alert clearing support but do not replace original-video verification. One passing video does not prove all source media are valid.

## Escalation

Escalate if stderr is missing, the encoder exists but still fails, multiple healthy codecs fail, or task/image identity cannot be verified. Keep alternative hypotheses and evidence gaps.


## Original uploaded video integration

For namespace video-platform, correlate the committed video_task_api execution error and FAILED task with the exact original job_id and task_id. An explicit Unknown encoder or Encoder not found plus that task failure supports this direct diagnosis even when optional Loki logs are unavailable. The error alone does not establish an absent FFmpeg build feature.

The permitted action retry_video_ffmpeg_h264 changes only this failed video's codec to H.264 and retries the same Job after bound authenticated human approval. Preserve its original filename and input. The action does not install codecs or change the public h264/h265 allowlist. Require a new completed task outcome and ffprobe-confirmed fresh H.264 output for the same Job. The original video list must expose playback/download. Retain the result until the user deletes it.
