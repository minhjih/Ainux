# Ainux Ubuntu Remix Builder

This directory contains automation to assemble an Ubuntu-based live ISO that
bootstraps the AI-native workflows documented in `docs/ai_friendly_os_design.md`.
The process **never modifies the host operating system**; all work happens in a
temporary `work/` tree and the result is a standalone ISO image you can flash or
boot in other environments. The scripts rely on `debootstrap` and the standard
Ubuntu live ISO toolchain, allowing you to customize the distribution while
maintaining compatibility with upstream updates.

## Features

* Uses Ubuntu 22.04 LTS (`jammy`) as the baseline.
* Installs the Ainux automation stack (Ansible, Python tooling, telemetry
  helpers) plus NVIDIA drivers, CUDA toolkit, and container runtime support.
  GPU-related packages are marked as optional, so the build continues even if a
  particular architecture or mirror does not publish them.
* Pulls in Ubuntu's `linux-generic` meta package so kernel headers are always
  present, letting DKMS-based drivers (예: NVIDIA 스택) compile cleanly during the
  image build instead of failing with missing header errors.
* Treats `iptables-persistent` as an optional package so hypervisors without
  netfilter modules (또는 미러에 패키지가 없는 경우)에서도 빌드가 멈추지 않습니다.
* Installs the `ubuntu-desktop-minimal` GNOME session, enables GDM auto-login for
  the `ainux` user, and autostarts the browser-based Ainux Studio so the live ISO
  immediately feels like a desktop OS rather than a server shell.
* Bundles Ubuntu's `ubiquity` installer stack (GTK frontend, slideshow) and seeds
  `Install Ainux` launchers on the desktop and Activities overview so the live ISO
  can deploy onto NVMe/SSD/HDD targets without requiring a separate installer image.
* Forces Netplan to use the NetworkManager renderer and bundles
  `open-vm-tools-desktop` so VMware/VirtualBox/QEMU guests obtain networking
  automatically without manual configuration.
* Replaces Ubuntu branding with Ainux identity across `/etc/os-release`,
  `/etc/issue`, MOTD, GNOME backgrounds, icons, and the login greeter so prompts
  and UI surfaces display `ainux` instead of `ubuntu`. Branding assets pulled
  from `folder/ainux.png` and `folder/ainux_penguin.png` are copied into the ISO
  and become the default wallpaper, lock screen, icons, and studio imagery; the
  same files dropped into `/usr/share/ainux/branding` on a running system take
  effect immediately.
* Adds a persistent hostname service so casper-based live boots and fresh
  installs always present themselves as `ainux`, keeping the shell prompt and
  display manager on-brand.
* Injects `usbcore.autosuspend=-1` into the live kernel command line and the
  installed system's GRUB defaults to suppress VMware "usb 2-1" descriptor
  errors reported on shutdown or reboot.
* Configures Docker to default to the `fuse-overlayfs` storage driver so live
  sessions on read-only media avoid overlay "mapping" errors while still
  allowing installed systems to switch back to `overlay2` later.
* Preloads infrastructure scheduling toolchain (SLURM clients, networking
  diagnostics, IPMI utilities) so AI agents can coordinate complex hardware
  operations out-of-the-box.
* Seeds a default `ainux` operator account with configuration derived from the
  design document (automation profile, accelerator provisioning defaults).
* Bundles the `ainux-client` CLI (with an `ainux-ai-chat` alias) so the live system can authenticate to
  OpenAI-compatible GPT endpoints and capture transcripts for auditing.
* Installs the context fabric toolkit so operators can snapshot files, settings,
  and events for AI-driven workflows.
* Ships the hardware automation service (inventory scanner, driver/firmware
  catalog, telemetry collector) so GPU/accelerator upkeep can run hands-free.
* Ships a browser-based orchestration studio so operators can review natural
  language conversations, plans, and execution logs side-by-side.
* Hardened SSH configuration and MOTD branding.
* Generates both GRUB and ISOLINUX boot loaders for BIOS/UEFI compatibility.
* (Optional) Builds a raw GPT disk image (`--disk-image`) with EFI/BIOS GRUB,
  UUID-based `fstab`, and all Ainux tooling preinstalled so NVMe/SSD targets or
  VM disks boot straight into Ainux without appearing as a removable ISO.

## Prerequisites

Run the build on an Ubuntu (or Debian-based) machine with the following
packages installed:

```bash
sudo apt-get update
sudo apt-get install -y debootstrap squashfs-tools xorriso isolinux \
  mtools dosfstools rsync
```

If you plan to create a raw disk image via `--disk-image`, also install tools
for partitioning and filesystem creation:

```bash
sudo apt-get install -y parted e2fsprogs util-linux
```

The build script now generates the GRUB EFI binary from inside the Ubuntu
chroot, so you do **not** need to install additional GRUB packages on the host
machine. As long as the standard ISO tooling above is available, the workflow
produces a hybrid BIOS/UEFI image automatically.

If you are **cross-building** (for example, assembling an `arm64` ISO on an
`amd64` host), also install:

```bash
sudo apt-get install -y qemu-user-static binfmt-support
```

The script detects the architecture mismatch, performs the first debootstrap
stage with `--foreign`, copies the matching QEMU static binary into the chroot,
and then triggers the second stage inside the chroot. Skipping these packages
leads to debootstrap errors such as `Failure trying to run: chroot ... /bin/true`
because the host kernel cannot execute the target architecture binaries.

> 🌐 **ARM 타깃 기본 미러:** `--arch arm64`(또는 `armhf`/`armel`)로 빌드하면 스크립트가
> `http://ports.ubuntu.com/ubuntu-ports` 미러를 자동 선택합니다. 해당 미러는
> 아시아, 특히 한국에서 가장 안정적으로 ARM 패키지를 제공하므로 추가 설정 없이도
> 빠르게 이미지를 구성할 수 있습니다. 필요 시 `--mirror` 옵션으로 원하는 URL을
> 지정하면 즉시 덮어쓸 수 있습니다.

> 📡 **DNS/네트워크 확인:** 빌더는 chroot 안에서 패키지를 내려받기 전에 호스트의
> `/etc/resolv.conf`를 복사합니다. 호스트가 사설 DNS, VPN, 프록시 등을 사용한다면
> 해당 설정이 `/etc/resolv.conf`에 올바르게 반영돼 있어야 하며, 파일이 비어 있거나
> 존재하지 않으면 chroot 내부에서 `Temporary failure resolving ...` 오류가 발생합니다.
> 이런 경우에는 호스트의 DNS 구성을 먼저 정상화한 뒤 다시 빌드를 시도하세요.

During a foreign build the script keeps the QEMU helper inside the chroot until
all package configuration tasks finish, preventing confusing errors like
``/usr/bin/apt-get: No such file or directory`` that arise when the host tries
to execute target-architecture binaries without an interpreter. Custom chroot
scripts should leave the helper in place; the builder removes it automatically
right before the filesystem is packed into the ISO.

If QEMU crashes (for example `QEMU internal SIGSEGV`) during the second stage,
debootstrap may stop before `apt-get`/`dpkg` are installed. The builder now
detects this condition, aborts immediately, and preserves
`work/debootstrap.log` so you can review the failing package. In that case,
double-check the `qemu-user-static` version and binfmt registration or retry on
a host that matches the target architecture.

If debootstrap reports `Failure while configuring required packages`, the
second stage aborted while configuring the base system. The build script now
bind-mounts `/proc`, `/sys`, and `/dev` automatically and preserves the full
log at `work/debootstrap.log` (even when the run fails) so you can inspect the
exact package that stopped the process. Re-run with `--keep-work` for further
analysis if needed.

You must execute the build as `root` (or via `sudo`) because debootstrap and the
ISO generation steps require elevated privileges.

## Directory Layout

```
build/ubuntu-ainux/
├── build.sh               # Primary orchestration script
├── config/
│   ├── packages.txt       # Extra packages installed inside the live system
│   ├── chroot_setup.sh    # Additional configuration executed in the chroot
│   ├── sources.list       # Custom apt mirror definition (uses @UBUNTU_MIRROR@ placeholder)
│   └── (optional) grub.cfg for further boot menu customization
└── overlay/               # Drop-in files copied into the root filesystem
└── ../folder/             # (optional) Branding PNGs picked up automatically if present
```

Populate the `overlay/` directory with configuration snippets, systemd units, or
other files that should be injected into the root filesystem verbatim.

## Building the ISO

> ⚠️ **리소스 경고:** debootstrap, SquashFS 압축, ISO 패키징 과정은 수 기가바이트의
> 디스크 공간과 높은 CPU/메모리 사용률을 요구합니다. 운영 중인 서버에서는
> 빌드를 실행하지 말고, 전용 빌드 박스나 임시 VM을 사용하세요. 실수로 실행하는
> 일을 막기 위해 `build.sh`는 기본적으로 종료하며, 안전하다고 판단되는 환경에서
> `AINUX_ALLOW_BUILD=1`을 지정해야만 진행됩니다.

```bash
cd build/ubuntu-ainux
sudo AINUX_ALLOW_BUILD=1 ./build.sh --release jammy --arch amd64 --output ~/ainux-jammy.iso
```

To generate a bootable raw disk (ideal for NVMe passthrough or VM disks) at the
same time, pass `--disk-image` and optionally `--disk-size`:

```bash
sudo AINUX_ALLOW_BUILD=1 ./build.sh \
  --release jammy --arch amd64 \
  --output ~/ainux-jammy.iso \
  --disk-image ~/ainux-jammy.img \
  --disk-size 16G
```

The script streams all output to `/tmp/ainux-build.log` (override with
`AINUX_BUILD_LOG`) so you can review progress or diagnose failures if the run is
interrupted. Expect status lines such as `[bootstrap]`, `[overlay]`, and
`[squashfs]` as each phase completes.

The script creates a `work/` directory containing the debootstrap chroot and ISO
staging tree. By default the directory is removed on success. Pass `--keep-work`
if you want to inspect the intermediate artifacts.

The resulting ISO can be booted in a virtual machine or written to a USB drive
for bare-metal installation/testing:

```bash
sudo dd if=~/ainux-jammy.iso of=/dev/sdX bs=4M status=progress && sync
```

### Installing onto NVMe/SSD from the live session

The generated ISO boots into a branded GNOME desktop running as the `ainux`
user. To install the system onto a persistent NVMe/SSD/HDD volume, launch the
**Install Ainux** shortcut placed on both the desktop and the Activities
overview. Under the hood it runs Ubuntu's `ubiquity` GTK frontend with Ainux
branding and defaults.

1. Choose your language, keyboard layout, and timezone as usual.
2. Select the destination disk/partition (NVMe, SATA, virtual disk, etc.).
3. Proceed through the installer; the default `ainux` account is created with the
   same `ainuxos8` password seeded in the live session.
4. When prompted, remove the ISO/USB and reboot—GRUB will load the newly
   installed system directly from the internal disk.

You can also run the installer from a terminal:

```bash
/usr/local/bin/ainux-install
```

When executed as a non-root user, the helper uses `pkexec` to elevate and then
calls `ubiquity --no-migration-assistant`, matching the desktop launcher
behaviour.

### 라이브 세션 접속 정보

라이브 환경이 부팅되면 콘솔 `tty1`에서 `ainux` 계정으로 자동 로그인되도록
`getty` 오버라이드를 구성해 두었습니다. 만약 그래픽 로그인 매니저나 다른
TTY에서 수동 로그인이 필요한 경우 기본 자격 증명은 아래와 같습니다.

- 사용자 이름: `ainux`
- 비밀번호: `ainuxos8`

설치한 뒤에는 `passwd`로 비밀번호를 변경하거나, 새 운영자 계정을 생성해
사용하는 것을 권장합니다.

## Extending the Build

* **Additional Packages:** Add them to `config/packages.txt` (one per line).
  Prefix a package with `?` to treat it as optional—useful for hardware-specific
  drivers that may be missing from certain architectures or mirrors.
  예를 들어 기본 구성에서는 `?iptables-persistent`로 표시하여 netfilter가 비활성화된
  VM에서도 빌드가 중단되지 않도록 했습니다. 방화벽 상태를 유지하고 싶다면 `?`를 제거하거나
  ISO 부팅 후 수동으로 패키지를 설치하세요.
  라이브 부팅 단계에서 사용하는 `lupin-casper` 패키지도 ports 계열 미러에는 존재하지 않는
  경우가 많아, 스크립트가 자동으로 옵션 처리하고 있습니다. 패키지를 반드시 포함해야 한다면
  제공하는 미러를 사용하거나 ISO 생성 이후 수동으로 추가하세요.
  라이브 세션에서 Docker가 overlay2 검사 중 실패하지 않도록 `fuse-overlayfs` 패키지를
  기본 포함시키고 `/etc/docker/daemon.json`을 `fuse-overlayfs` 드라이버로 초기화합니다.
  디스크에 설치한 후 overlay2로 변경하려면 해당 파일을 편집 또는 삭제한 뒤 Docker를
  재시작하면 됩니다.
* **Branding Overrides:** 리포지토리 루트에 위치한 `folder/` 디렉터리에 `ainux.png`,
  `ainux_penguin.png` 파일을 추가하면 빌드 시 `/usr/share/ainux/branding/`으로 복사되어
  GNOME 배경, 잠금 화면, 아이콘, 웹 스튜디오 전반에 반영됩니다. 폴더가 비어 있거나 파일이
  없으면 패키지에 내장된 기본 아트워크가 사용됩니다.
* **Post-Install Logic:** Modify `config/chroot_setup.sh` to run extra commands
  inside the chroot. For complex flows consider invoking Ansible playbooks.
* **Hardware Blueprints:** Place YAML or JSON templates inside `overlay/` or
  extend the provided examples in `/usr/local/share/ainux/playbooks/`.

### AI-Orchestrated Scheduling Commands

The chroot customization seeds three helper commands exposed to the default
`ainux` user. Each is a thin wrapper around the matching `ainux-client`
subcommand (the legacy `ainux-ai-chat` alias is still provided) so you get
identical behaviour on the host and inside the live ISO:

| Command | Purpose |
|---------|---------|
| `ainux-scheduler` | Proxy for `ainux-client scheduler`, covering blueprint execution, SLURM job submission, status/cancel flows, and maintenance-window logging tied to the context fabric. |
| `ainux-network-orchestrator` | Proxy for `ainux-client network`, managing declarative network profiles (interfaces, VLANs, QoS, nftables rules) with dry-run previews. |
| `ainux-cluster-health` | Proxy for `ainux-client cluster`, generating single-shot or streaming health snapshots that include load, memory, GPUs, scheduler queues, and interface counters. |

Blueprint samples live under `/usr/local/share/ainux/playbooks/` inside the ISO
and are implemented as Ansible playbooks so they can be versioned and audited.
Extend or replace them to reflect your infrastructure requirements; the Python
services resolve additional paths such as `~/.config/ainux/playbooks` so local
customisations are automatically discovered.

### GPT API bootstrap

The live system ships with `/usr/local/bin/ainux-client` (plus a compatibility
symlink `/usr/local/bin/ainux-ai-chat`) and also drops a convenience symlink at
`/home/ainux/ainux-client` so live sessions can run either `ainux-client` or
`./ainux-client` without additional setup. The wrapper delegates to the Python
module located at `/usr/local/lib/ainux/ainux_ai`. Configure an API key once and
the chat client becomes available to automations and shell workflows:

```bash
ainux-client configure --default          # prompt for key/model/base URL
ainux-client set-key --api-key sk-new-... # rotate the stored API key later
ainux-client chat --message "GPU 상태 점검 루틴 만들어줘"
ainux-client orchestrate "CUDA 스택을 최신화해줘" --dry-run
```

For unattended scripts, export credentials via environment variables prior to
invocation (e.g., `AINUX_GPT_API_KEY`, `AINUX_GPT_MODEL`, `AINUX_GPT_BASE_URL`).

Transcripts generated by `ainux-client` can be persisted by passing
`--history /var/log/ainux/chat.jsonl`, enabling compliance teams to audit
intent capture and responses alongside infrastructure actions. The
`orchestrate` subcommand prints structured plans and execution logs; combine it
with `--json` to export data into other observability systems.

### Context fabric state management

The live image seeds `~/.config/ainux/context_fabric.json` for the `ainux`
operator. Use the bundled CLI to inspect or extend the shared knowledge graph
that powers orchestration:

```bash
ainux-client context snapshot --limit-events 10
ainux-client context ingest-file /etc/ainux/profile.yaml --label profile --tag bootstrap
ainux-client context record-event maintenance.completed --data '{"status": "ok"}'
ainux-client orchestrate "GPU 재부팅 후 상태 보고서 작성" --use-fabric
```

All context changes are persisted back to the JSON state file so subsequent
sessions and automation runs have access to the latest system view.

### Hardware automation toolkit

The live image exposes `ainux-client hardware` and a convenience alias
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

The live image exposes `/usr/local/bin/ainux-client ui`, and the default shell
profile creates a short alias `ainux-ui`. Launching the command starts a local
web server (default `http://127.0.0.1:8787`) that renders a glassmorphism UI
with three synchronized panes. Release 0.8 embeds the square Ainux logo and
penguin mascot as base64 assets so the hero banner, desktop wallpaper, and
floating mascot always render, while the build optionally copies overrides from
`folder/` into `/usr/share/ainux/branding` to mirror your customized desktop
experience:

1. 자연어 대화 타임라인 – 사용자 프롬프트, 실행 모드, 경고 배지를 카드로 표시합니다.
2. 계획·명령 로그 – 승인/차단 여부와 실행 결과를 단계별로 정리합니다.
3. 컨텍스트 패브릭 현황 – 저장 경로, 노드/엣지/이벤트 수, 최근 이벤트 타임라인을
   즉시 확인할 수 있습니다.

UI 하단의 토글로 드라이런/실행, 오프라인 모드, 패브릭 사용 여부를 즉시 조정할
수 있으며, 배경에 사용되는 로고/마스코트 이미지는 `/usr/share/ainux/branding`
을 교체하면 즉시 반영됩니다. 브라우저를 열지 않고 서버만 띄우고 싶다면
`ainux-ui --no-browser` 또는 원격 포트 포워딩과 함께 사용할 수 있습니다.

This initial build pipeline establishes the foundation for an Ubuntu-based
Ainux operating system. The seeded orchestrator offers a first pass at turning
자연어 요청 into actionable plans; future iterations will attach deeper
hardware provisioning agents, user-facing orchestrators, and UI layers defined
in the overarching architecture document.
