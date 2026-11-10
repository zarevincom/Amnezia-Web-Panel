# VK TURN Proxy installation

The user selected Moroka8 + WRAP + WireGuard and authorized implementation, GitHub push and deployment of the updated panel. VLESS is deferred.

Add a single `vkturn` service to the existing Marketplace. Reuse the panel's standard WireGuard container and client CRUD. If it is absent, install it without replacing any existing backend; stop on conflicting ports or an unhealthy backend. Removing VK TURN leaves WireGuard and its profiles intact. Refuse WireGuard removal while VK TURN is installed.

Pin Moroka8 v1.10.1 server binaries for Linux amd64/arm64 and verify their published SHA-256. Run the binary in an unprivileged, read-only Docker container on the host network, forwarding only to the local published WireGuard endpoint. Store a random 32-byte WRAP key in a protected host directory, outside panel state and status responses. Expose it only through admin-only settings/export endpoints. Default listener: 56000/udp. Retain settings on container restart. Refuse accidental reinstallation of an existing proxy.

Profiles stay under WireGuard. Add a VK export action: generate a separate config with Endpoint 127.0.0.1:9000 and MTU 1280, plus an anton48 iOS import link using SRTP-WRAP when the administrator supplies their VK call link. Never modify the ordinary profile. Explain client installation and TURN route exclusions for desktop/Android. IPv4 tunnel routing for VK exports avoids advertising unsupported IPv6.

Approaches: original cacggghp Legacy was rejected due to documented throttling; anton48 SRTP has a narrower client combination; Moroka8 WRAP has the requested desktop/Android clients and documented iOS compatibility.

Verification: manager tests with fake SSH for failure paths, admin and validation API tests, real template rendering/JavaScript checks, full unittest suite, a disposable server-side runtime smoke test if available. Live carrier/VK end-to-end testing requires the user's devices and call link and is reported separately.

Release from an ordinary branch in the primary repository. Publish the feature branch to the fork; deploy its exact tested commit from a Git archive to the existing `web` Compose service in /opt/amneziavpnphp, with a consistent SQLite snapshot, retained old code/image and rollback on health failure. Do not install VPN services on the panel VPS merely by deploying panel code.
