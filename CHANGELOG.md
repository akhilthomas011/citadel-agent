---
title: Changelog
description: Implemented Citadel capabilities, deployment scope and release acceptance boundaries.
ms.date: 2026-11-08
---

## Unreleased - v1.0 preparation

### Scope

Citadel Agent operates an existing Citadel hub through upstream contracts.
It does not provision the full accelerator or promise parity with every
upstream API. CLI and DevUI are supported local interfaces; DevUI is not a
production multi-user UI.

The three-page Linux App Service UI/BFF, private Container Apps agent and
scheduled workers are locally implemented and verified. Production hosted
readiness remains gated by reviewed publication and real customer acceptance.

Greenfield deployment now includes APIM and related infrastructure through the
pinned upstream Bicep, followed by product-host/identity bootstrap and
installation. The current installer still requires existing prerequisite hosts;
greenfield orchestration is planned rather than claimed as implemented.

### Included capabilities

- Model discovery and generated clients; access, backend, publish and alert
  contracts with prepared changes, what-if and explicit approval.
- Diagnostics, smoke tests, usage estimates and reviewed pricing seeding.
- Key rotation, GitOps recording, upstream compatibility and synchronization.
- Hardened manifest validation, approval diffs and workflow filename handling.
- Reviewed runtime and build dependency locking for CI and contracts jobs.
- Self-contained wheel/sdist assets with clean-installed CLI/runtime checks.
- Hosted Entra sessions, exact hub grants, persisted conversations and
  hash-bound independently approved PR-only changes.
- Resumable FinOps reports, maintained analytics, six analytical views and
  report-bound authorized exports with explicit pricing/quality limitations.
- Scoped Operations request evidence, alerts, pipeline telemetry and
  nonfinancial attribution; durable assistant work with no started-effect replay.
- Private hosting Bicep, preview-first workflows and sealed Linux installer
  artifacts without customer-specific source or dependency rebuilds.
- Root MIT license, preserving existing source and upstream notices.

### Compatibility and release gates

- Test matrix: Python 3.11-3.13 on Ubuntu and Python 3.11 on Windows.
- Azure CLI/Bicep and Git remain external prerequisites for deployment tasks.
- Production requires externally enforced GitHub review and Azure RBAC/OIDC.
- Costs require operator-reviewed pricing; usage ingestion may be delayed.
- Notebook acceptance has model/network limitations documented in
  [validation](docs/validation.md). Live tests are opt-in and incur costs.

The version remains pre-release until package acceptance, protected deployment
governance and final CI validation pass. No v1.0 tag has been published.
