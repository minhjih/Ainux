# Ainux Ubuntu Remix Builder

This directory contains automation to assemble an Ubuntu-based live ISO that
bootstraps the AI-native workflows documented in `docs/ai_friendly_os_design.md`.
The scripts rely on `debootstrap` and the standard Ubuntu live ISO toolchain,
allowing you to customize the distribution while maintaining compatibility with
upstream updates.

## Features

* Uses Ubuntu 22.04 LTS (`jammy`) as the baseline.
* Installs the Ainux automation stack (Ansible, Python tooling, telemetry
  helpers) plus NVIDIA drivers, CUDA toolkit, and container runtime support.
* Preloads infrastructure scheduling toolchain (SLURM clients, networking
  diagnostics, IPMI utilities) so AI agents can coordinate complex hardware
  operations out-of-the-box.
* Seeds a default `ainux` operator account with configuration derived from the
  design document (automation profile, accelerator provisioning defaults).
* Hardened SSH configuration and MOTD branding.
* Generates both GRUB and ISOLINUX boot loaders for BIOS/UEFI compatibility.

## Prerequisites

Run the build on an Ubuntu (or Debian-based) machine with the following
packages installed:

```bash
sudo apt-get update
sudo apt-get install -y debootstrap squashfs-tools xorriso isolinux \
  grub-pc-bin grub-efi-amd64-bin mtools rsync
```

You must execute the build as `root` (or via `sudo`) because debootstrap and the
ISO generation steps require elevated privileges.

## Directory Layout

```
build/ubuntu-ainux/
├── build.sh               # Primary orchestration script
├── config/
│   ├── packages.txt       # Extra packages installed inside the live system
│   ├── chroot_setup.sh    # Additional configuration executed in the chroot
│   ├── sources.list       # Custom apt mirror definition
│   └── (optional) grub.cfg for further boot menu customization
└── overlay/               # Drop-in files copied into the root filesystem
```

Populate the `overlay/` directory with configuration snippets, systemd units, or
other files that should be injected into the root filesystem verbatim.

## Building the ISO

```bash
cd build/ubuntu-ainux
sudo ./build.sh --release jammy --arch amd64 --output ~/ainux-jammy.iso
```

The script creates a `work/` directory containing the debootstrap chroot and ISO
staging tree. By default the directory is removed on success. Pass `--keep-work`
if you want to inspect the intermediate artifacts.

The resulting ISO can be booted in a virtual machine or written to a USB drive
for bare-metal installation/testing:

```bash
sudo dd if=~/ainux-jammy.iso of=/dev/sdX bs=4M status=progress && sync
```

## Extending the Build

* **Additional Packages:** Add them to `config/packages.txt` (one per line).
* **Post-Install Logic:** Modify `config/chroot_setup.sh` to run extra commands
  inside the chroot. For complex flows consider invoking Ansible playbooks.
* **Hardware Blueprints:** Place YAML or JSON templates inside `overlay/` or
  extend the provided examples in `/usr/local/share/ainux/playbooks/`.

### AI-Orchestrated Scheduling Commands

The chroot customization seeds three helper commands exposed to the default
`ainux` user:

| Command | Purpose |
|---------|---------|
| `ainux-scheduler` | Execute declarative maintenance blueprints or relay SLURM job submissions with guard rails. |
| `ainux-network-orchestrator` | Inspect network state and apply packet/QoS policies from reusable templates. |
| `ainux-cluster-health` | Gather GPU, sensor, BMC, and scheduler telemetry for quick triage. |

Blueprint samples live under `/usr/local/share/ainux/playbooks/` inside the
ISO and are implemented as Ansible playbooks so they can be versioned and
audited. Extend or replace them to reflect your infrastructure requirements.

This initial build pipeline establishes the foundation for an Ubuntu-based
Ainux operating system. Future iterations can integrate the intelligent
hardware provisioning agents, user-facing orchestrators, and UI layers defined
in the overarching architecture document.
