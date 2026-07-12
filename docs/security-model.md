# Reefy OS Security Model

*How Reefy reduces attack surface, protects system integrity, and makes firmware origin independently verifiable*

*July 11, 2026*

Reefy OS is a purpose-built Linux distribution designed around security, recoverability, and verifiability from the beginning. Its security does not depend on one feature or the assumption that software never has bugs. The model combines a traceable software supply chain, a minimal host, an immutable base filesystem, recoverable A/B updates, an actively maintained LTS kernel, containerized applications, and encrypted persistent storage.

The objective is straightforward: run less code on the host, make the code that does run inspectable, keep applications and persistent data outside the firmware base, and make failures recoverable.

## Security model at a glance

| Area | Security design |
|---|---|
| Firmware origin | Public builds and signed provenance tie artifacts to source. |
| Minimal host | Buildroot selects required components; no on-device package manager. |
| Integrity and recovery | Read-only root, A/B firmware, health checks, and watchdog rollback. |
| Kernel | Linux LTS, exact version pinning, hardening, and ongoing updates. |
| Applications and data | Containers separate workloads; LUKS2 encrypts persistent storage. |
| Connectivity | Outbound tunnels reduce direct public-network exposure. |

## Security goals and assumptions

Reefy OS is designed to reduce several common risks:

- opaque firmware whose source and build origin cannot be inspected;
- general-purpose host environments containing unrelated packages and services;
- configuration drift caused by partial package upgrades;
- persistent mutation of the shipped root filesystem;
- failed updates that leave a remote device unbootable;
- application dependency changes contaminating the host OS; and
- disclosure of an encrypted internal drive after it is separated from the USB key.

The model does not assume that open source is automatically secure, that containers are perfect isolation, or that a newer kernel has no vulnerabilities.

## 1. A traceable software supply chain

The [Reefy repository is public](https://github.com/reefyai/reefy). It contains the Reefy-owned source, Buildroot configuration, kernel configuration, root filesystem overlay, image-assembly scripts, tests, and GitHub Actions workflows used to build Reefy OS.

The finished image also incorporates third-party projects and hardware-support components such as vendor firmware and drivers. Their selection and integration recipes are visible in the tree even when an upstream vendor distributes a binary component rather than source.

By exposing source changes, build instructions, artifact digests, and signed provenance, Reefy is designed to reduce supply-chain attack surface and make malicious code injection or post-build artifact substitution easier to detect and investigate.

The current build path is:

1. Git identifies an exact Reefy source commit. The Buildroot source is recorded as a submodule commit.
2. [`configs/reefy_defconfig`](https://github.com/reefyai/reefy/blob/dca4747e5692cdf3d67d7f472e4c7aadc5e86f3f/configs/reefy_defconfig) declares the architecture, kernel source and version, filesystems, networking tools, Docker, Python modules, firmware, drivers, and other packages selected for the host.
3. The public [`firmware-build.yml`](https://github.com/reefyai/reefy/blob/dca4747e5692cdf3d67d7f472e4c7aadc5e86f3f/.github/workflows/firmware-build.yml) checks out the source and submodule, configures Buildroot, builds the images, checks the expected kernel and required modules, and uploads production and development artifacts.
4. GitHub records the source SHA, workflow, trigger, logs, result, and artifact digests. For example, [run 28710201963](https://github.com/reefyai/reefy/actions/runs/28710201963) completed successfully from commit [`dca4747e…`](https://github.com/reefyai/reefy/commit/dca4747e5692cdf3d67d7f472e4c7aadc5e86f3f).
5. The workflow creates signed build provenance for the production and development `.efi` and `.raw` files. The example run’s [public attestation](https://github.com/reefyai/reefy/attestations/33883769) binds their digests to the repository, source commit, workflow, event, and run.

For public repositories, GitHub uses Sigstore’s public-good instance and records the attestation in a public transparency log. [GitHub documents the information contained in an artifact attestation](https://docs.github.com/en/actions/concepts/security/artifact-attestations).

## 2. A purpose-built host with less software drift

Reefy OS is assembled with [Buildroot](https://buildroot.org/downloads/manual/manual.html). It does not begin with a general-purpose Ubuntu or Debian installation and remove a few visible applications afterward.

Buildroot produces a complete root filesystem from an explicit configuration. This has three important effects:

- The top-level package selection is deliberate and reviewable. Buildroot resolves required dependencies, but packages are not included merely because a conventional desktop or server distribution normally ships them.
- The device has no `apt`, `dnf`, or similar package manager. The base OS is upgraded as a complete firmware unit rather than accumulating an untested combination of partial package updates.
- Application dependencies live in containers instead of being installed into the host filesystem.

This is a smaller attack surface, not a zero attack surface. Reefy still requires Docker, OpenSSH, secure device communication, storage and encryption tools, system services, and drivers for diverse PC and GPU hardware. Each component must be maintained. The benefit is that the set is intentional and visible.

## 3. An immutable base with explicit persistence

The build creates the root filesystem as SquashFS, packages it in an initramfs, and embeds it with the kernel in a Unified Kernel Image. The process is visible in [`create_initramfs.sh`](https://github.com/reefyai/reefy/blob/dca4747e5692cdf3d67d7f472e4c7aadc5e86f3f/board/reefy/reefy/scripts/create_initramfs.sh) and the early-boot [`init`](https://github.com/reefyai/reefy/blob/dca4747e5692cdf3d67d7f472e4c7aadc5e86f3f/board/reefy/reefy/scripts/init).

At boot, the read-only SquashFS becomes the lower layer of an overlay filesystem. Normal host writes land in a temporary upper layer and disappear after reboot. The shipped base remains a fixed unit instead of changing gradually over the device’s lifetime.

Persistent application data and explicitly configured overlays live separately, preserving required state without modifying the read-only firmware base.

## 4. Recoverable A/B firmware updates

Reefy places firmware in two bootable slots. An update is written to the inactive slot and selected for a one-time boot. Health checks confirm the new slot only after critical services start successfully.

If confirmation does not arrive, the boot watchdog reboots the machine and the firmware returns to the previous slot. A separate hardware watchdog covers kernel or systemd hangs, while an application watchdog checks important runtime services. The complete flow is documented in the [watchdog architecture](https://github.com/reefyai/reefy/blob/dca4747e5692cdf3d67d7f472e4c7aadc5e86f3f/docs/watchdog-architecture.md).

This design protects availability and makes updates safer to deploy to remote machines. A/B rollback does not prove that an update is authentic; build provenance and boot-time signature enforcement are separate controls.

Reefy packages the kernel, initramfs, and OS metadata as a single UKI EFI binary. The resulting binary is ready to be signed with customer-controlled Secure Boot keys, extending trust from the public build into the platform boot chain.

## 5. LTS kernel maintenance and exploit mitigation

Reefy moved from Linux 6.14 to the Linux 6.18 long-term-maintenance series. Kernel.org lists 6.18 as supported through December 2028. Stable and LTS releases receive selected bug fixes and security-relevant fixes without requiring devices to move to every new mainline kernel. Kernel.org maintains the list of [active LTS branches](https://www.kernel.org/category/releases.html).

Each firmware build pins a specific kernel revision, and CI verifies the expected kernel and module outputs.

The Reefy [`kernel-config`](https://github.com/reefyai/reefy/blob/dca4747e5692cdf3d67d7f472e4c7aadc5e86f3f/board/reefy/reefy/kernel-config) enables defense-in-depth features including:

- strong stack protection;
- kernel address-space randomization;
- strict kernel and module read/write/execute permissions;
- hardened user-copy checks and `FORTIFY_SOURCE`;
- seccomp filtering; and
- unprivileged BPF disabled by default.

These controls reduce exploitability. They do not replace timely kernel updates or demonstrate that every possible vulnerability has been eliminated.

## 6. Application isolation

Reefy applications run in Docker containers. This keeps application filesystems, libraries, and update cycles outside the immutable host base. An application can change without installing its dependencies directly into the operating system.

The container boundary must still be treated realistically. Container images need their own provenance, patching, least-privilege configuration, and dependency review. Host mounts, device access, Linux capabilities, and privileged mode can weaken isolation. A container is a useful security boundary only when its permissions match the workload’s actual needs.

## 7. Encrypted persistent storage

Persistent Reefy data is stored in LUKS2-encrypted storage. The current design places the unlock key on the USB boot device and stores application data on the USB data partition or an internal drive. The [storage architecture](https://github.com/reefyai/reefy/blob/dca4747e5692cdf3d67d7f472e4c7aadc5e86f3f/docs/storage-architecture.md) documents the partition layout and key lifecycle.

Separating the USB key from an internal data drive makes the drive unreadable on its own. This is useful when drives are removed, replaced, or transported separately.

Encryption also protects data at rest, not a running system after authorized unlock. A compromised host process with sufficient permission can access data that the operating system has already mounted.

## 8. Network and control-plane boundary

Reefy uses outbound secure tunnels for remote access, avoiding the need for direct router port forwarding, a static public IP address, or a publicly exposed dashboard on the device. This reduces direct WAN exposure.

It does not eliminate network trust. The tunnel client, device credentials, remote control plane, OpenSSH, LAN-accessible applications, and any installed container services remain part of the system’s attack surface. Their authentication, authorization, update cadence, and expected network destinations should be treated as part of the security model rather than as external details.

## Verifying a current build

With the GitHub CLI installed, a reviewer can download the production artifact from the example run and verify both firmware files:

```bash
gh run download 28710201963 \
  --repo reefyai/reefy \
  --name reefy-prod \
  --dir reefy-prod

gh attestation verify reefy-prod/reefy-prod.efi \
  --repo reefyai/reefy

gh attestation verify reefy-prod/reefy-prod.raw \
  --repo reefyai/reefy
```

[GitHub documents the verification command](https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/use-artifact-attestations). A successful result confirms the signed origin and displays the associated build metadata. It does not replace inspection of the source, configuration, dependencies, or runtime behavior.

Reviewers can also clone the repository at the attested commit, initialize the recorded Buildroot submodule, and follow the public workflow on a compatible Linux host. That independently exercises the build recipe. Until Reefy demonstrates reproducible builds, the locally generated bytes may differ because of environmental inputs.

## Security as an ongoing process

Reefy’s security model combines source visibility, a minimal and immutable host, traceable firmware, recoverable updates, workload separation, and encrypted storage.

Security is not a permanent label attached to an image. It is the ongoing work of reducing trust, documenting the trust that remains, verifying build and delivery paths, updating dependencies, and testing boundaries. Reefy OS is designed so that each layer can be inspected and improved independently.
