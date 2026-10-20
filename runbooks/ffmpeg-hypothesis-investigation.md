---
title: FFmpeg failure hypothesis investigation
service: transcoder
symptom: transcode_error
error_code: INVESTIGATION
updated_at: 2026-10-16
version: 2.0
---
## Competing hypotheses
A failed video task alone does not establish its cause. Compare unsupported encoder, input parsing failure, worker interruption, and resource or dependency failure. Treat these as hypotheses; cite the original job/task and the execution time.

## Encoder hypothesis and counterevidence
Unknown encoder or Encoder not found in the original failed task's stderr supports an encoder-selection failure. A failed original task with an explicit Unknown encoder error establishes the immediate encoder-selection failure; full version/configuration history is follow-up investigation, not missing proof of that error. Inspect get_encoder_inventory for the executing worker's snapshot. Absence of a snapshot means unknown, not that an encoder is missing. An inventory taken by another container is not evidence of the worker environment. If the requested encoder is present, investigate option incompatibility or a different FFmpeg binary rather than claiming a missing encoder.

## Input hypothesis and limitations
probe_video_input runs a fixed, local-file FFprobe command against the retained original upload. readable=true only establishes that a video stream can be opened and metadata parsed; it does not prove every frame is decodable. A timeout or unavailable file is not proof of corrupted media. A deterministic parser error supports an input or format hypothesis; compare it with the task's stderr before selecting a cause. Replacing or deleting the original input is advisory only and has no approved action.

## Worker interruption hypothesis
Compare task start time, original Pod UID and OOMKilled finish time. A current healthy Pod does not rule out an earlier OOM. Exit 137 or a high memory metric alone does not prove OOM. Metrics and logs from other videos on the shared worker must not become evidence for this job.

## Repair options and verification
For an evidence-supported unknown encoder, compare retry_video_ffmpeg_h264 with manual investigation of deployment configuration. H264 retry must retain the same job/input and produce a new recorded task; verify task completion and actual output codec with FFprobe. Lower risk does not waive the action allowlist, version binding, current preflight or authenticated approval. Changing the image, deleting inputs, installing codecs or arbitrary shell commands are advisory only unless separately implemented as bounded policies.
