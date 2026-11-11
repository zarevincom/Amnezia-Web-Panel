# OpenFlux Marketplace installation

Install OpenFlux from a managed VPS Marketplace with optional Yandex Docs, Mail.ru and Cups.online transports and encrypted direct fallback. Require at least one service transport. Use the existing Docker lifecycle and a single service instance, L4 userspace exit, node-v1.3.1 Linux amd64/arm64 binaries verified against upstream release SHA-256. No host routing changes or privileged container.

Approaches: upstream systemd installer has its own lifecycle/updater; a Docker service fits the panel's status/start/stop/removal mechanisms. Choose Docker, retaining upstream config and share-link generation. The user requested implementation, push and then a VPS update; implementation choices follow existing project patterns.

Keep the key in protected VPS files only. Admin-only no-store endpoint generates openflux:// using the pinned core; ordinary status/state contain no keys or links. Validate ports and carrier URLs before SSH. Refuse reinstall of an existing container. Preserve concurrent panel state edits during installation. Failed starts remove only the newly created container. Removal deletes only this service's directory. Shared channel access has no per-user revocation.

Verify manager failure paths, API authorization/validation/state merging, rendered templates/JS, translation completeness and full unittest suite. Prepare exact-commit deployment and backups using the existing VPS deployment arrangement. Do not install OpenFlux itself on any managed VPS merely by updating panel code.
