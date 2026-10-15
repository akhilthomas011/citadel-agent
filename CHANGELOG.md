# Changelog

## Unreleased - v1.0 preparation

### Scope

Citadel Agent operates an existing Citadel hub through upstream contracts.
It does not provision the full accelerator or promise parity with every
upstream API. CLI and DevUI are supported local interfaces; DevUI is not a
production multi-user UI.

Container Apps hosting with a UI is the intended next deployment destination.
Hosting, authentication, persistence, user isolation and UI requirements will
be specified separately. No production hosted-service readiness is claimed.

### Included capabilities

- Model discovery and generated clients; access, backend, publish and alert
  contracts with prepared changes, what-if and explicit approval.
- Diagnostics, smoke tests, usage estimates and reviewed pricing seeding.
- Key rotation, GitOps recording, upstream compatibility and synchronization.
- Hardened manifest validation, approval diffs and workflow filename handling.
- Reviewed dependency locking for checkout-based CI and contracts jobs.
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
