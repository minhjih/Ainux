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
* Bundles the `ainux-ai-chat` CLI so the live system can authenticate to
  OpenAI-compatible GPT endpoints and capture transcripts for auditing.
* Installs the context fabric toolkit so operators can snapshot files, settings,
  and events for AI-driven workflows.
* Ships the hardware automation service (inventory scanner, driver/firmware
  catalog, telemetry collector) so GPU/accelerator upkeep can run hands-free.
* Ships a browser-based orchestration studio so operators can review natural
  language conversations, plans, and execution logs side-by-side.
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

### GPT API bootstrap

The live system ships with `/usr/local/bin/ainux-ai-chat`, a wrapper around the
Python module located at `/usr/local/lib/ainux/ainux_ai`. Configure an API key
once and the chat client becomes available to automations and shell workflows:

```bash
ainux-ai-chat configure --default          # prompt for key/model/base URL
ainux-ai-chat set-key --api-key sk-new-... # rotate the stored API key later
ainux-ai-chat chat --message "GPU 상태 점검 루틴 만들어줘"
ainux-ai-chat orchestrate "CUDA 스택을 최신화해줘" --dry-run
```

For unattended scripts, export credentials via environment variables prior to
invocation (e.g., `AINUX_GPT_API_KEY`, `AINUX_GPT_MODEL`, `AINUX_GPT_BASE_URL`).

Transcripts generated by `ainux-ai-chat` can be persisted by passing
`--history /var/log/ainux/chat.jsonl`, enabling compliance teams to audit
intent capture and responses alongside infrastructure actions. The
`orchestrate` subcommand prints structured plans and execution logs; combine it
with `--json` to export data into other observability systems.

### Context fabric state management

The live image seeds `~/.config/ainux/context_fabric.json` for the `ainux`
operator. Use the bundled CLI to inspect or extend the shared knowledge graph
that powers orchestration:

```bash
ainux-ai-chat context snapshot --limit-events 10
ainux-ai-chat context ingest-file /etc/ainux/profile.yaml --label profile --tag bootstrap
ainux-ai-chat context record-event maintenance.completed --data '{"status": "ok"}'
ainux-ai-chat orchestrate "GPU 재부팅 후 상태 보고서 작성" --use-fabric
```

All context changes are persisted back to the JSON state file so subsequent
sessions and automation runs have access to the latest system view.

### Hardware automation toolkit

The live image exposes `ainux-ai-chat hardware` and a convenience alias
`ainux-hw`. The command family can refresh the hardware inventory, manage the
driver/firmware catalog, generate dependency-aware install plans, and capture
telemetry snapshots:

```bash
ainux-hw scan --json                # merge detected devices into the catalog
ainux-hw catalog drivers            # review driver blueprints shipped on the ISO
ainux-hw plan --apply --dry-run     # preview the commands required for updates
ainux-hw telemetry --samples 5      # collect health metrics (records fabric events)
```

Use `--catalog-path` to place the JSON catalog on shared storage and
`--fabric-path` to reuse a remote context fabric. When run without `--no-fabric`
the CLI logs inventory refreshes, catalog changes, and plan execution summaries
to the shared knowledge graph so the orchestrator and browser studio surface a
complete hardware timeline.

### Browser orchestration studio

The live image exposes `/usr/local/bin/ainux-ai-chat ui`, and the default shell
profile creates a short alias `ainux-ui`. Launching the command starts a local
web server (default `http://127.0.0.1:8787`) that renders a glassmorphism UI
with three synchronized panes:

1. 자연어 대화 타임라인 – 사용자 프롬프트, 실행 모드, 경고 배지를 카드로 표시합니다.
2. 계획·명령 로그 – 승인/차단 여부와 실행 결과를 단계별로 정리합니다.
3. 컨텍스트 패브릭 현황 – 저장 경로, 노드/엣지/이벤트 수, 최근 이벤트 타임라인을
   즉시 확인할 수 있습니다.

UI 하단의 토글로 드라이런/실행, 오프라인 모드, 패브릭 사용 여부를 즉시 조정할
수 있으며, 브라우저를 열지 않고 서버만 띄우고 싶다면 `ainux-ui --no-browser`
또는 원격 포트 포워딩과 함께 사용할 수 있습니다.

This initial build pipeline establishes the foundation for an Ubuntu-based
Ainux operating system. The seeded orchestrator offers a first pass at turning
자연어 요청 into actionable plans; future iterations will attach deeper
hardware provisioning agents, user-facing orchestrators, and UI layers defined
in the overarching architecture document.
