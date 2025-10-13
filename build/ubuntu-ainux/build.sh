#!/usr/bin/env bash
set -euo pipefail

if [[ "${AINUX_ALLOW_BUILD:-}" != "1" ]]; then
  cat <<'EOWARN'
[safety] 이 스크립트는 Ainux 전용 부팅 ISO 이미지만 생성하며, 현재 호스트 OS를 수정하지 않습니다.
[safety] 다만, debootstrap/SquashFS 작업이 시스템 리소스를 크게 사용하므로 안전장치가 기본 활성화되어 있습니다.
[safety] 빌드를 계속하려면 충분한 리소스를 갖춘 전용 머신 또는 임시 VM에서 AINUX_ALLOW_BUILD=1 환경 변수를 명시적으로 설정하세요.
[safety] 예시: sudo AINUX_ALLOW_BUILD=1 ./build.sh --release jammy --arch amd64 --output ~/ainux-jammy.iso
EOWARN
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

LOG_FILE="${AINUX_BUILD_LOG:-/tmp/ainux-build.log}"
mkdir -p "$(dirname "$LOG_FILE")"
exec > >(tee -a "$LOG_FILE") 2>&1
echo "[log] Streaming build output to $LOG_FILE"

# Ainux Ubuntu remix build script.
# This script bootstraps an Ubuntu-based live ISO customised with the
# intelligent automation hooks described in the Ainux design document.
# It uses debootstrap to assemble a root filesystem, applies additional
# configuration and packages, and finally generates a bootable ISO image.

# Requirements (on the host machine):
#   sudo/root privileges
#   debootstrap, squashfs-tools, xorriso, isolinux, grub-pc-bin,
#   grub-efi-amd64-bin, mtools
#   approx. 10GB free disk space
#   network access to Ubuntu mirrors
#
# The script intentionally keeps the logic modular: each phase is
# encapsulated inside a function so that integrators can mix in their own
# automation logic or replace pieces with Ainux-native services.

RELEASE="jammy"
ARCH="amd64"
MIRROR="http://archive.ubuntu.com/ubuntu"
ISO_LABEL="AINUX"
WORK_DIR="$SCRIPT_DIR/work"
ROOTFS_DIR="$WORK_DIR/chroot"
ISO_DIR="$WORK_DIR/iso"
CONFIG_DIR="$SCRIPT_DIR/config"
OVERLAY_DIR="$SCRIPT_DIR/overlay"
AI_CLIENT_DIR="$REPO_ROOT/ainux_ai"
BRANDING_DIR="$REPO_ROOT/folder"
CHROOT_MOUNTED=0

PACKAGES_FILE="$CONFIG_DIR/packages.txt"
CHROOT_SCRIPT="$CONFIG_DIR/chroot_setup.sh"
GRUB_CFG_FILE="$CONFIG_DIR/grub.cfg"

usage() {
  cat <<USAGE
Usage: $0 [options]

Options:
  -r, --release <ubuntu release>   Ubuntu codename to base on (default: $RELEASE)
  -a, --arch <architecture>        Target architecture (default: $ARCH)
  -m, --mirror <url>               Ubuntu mirror URL (default: $MIRROR)
  -l, --label <label>              ISO label (default: $ISO_LABEL)
  -o, --output <path>              Output ISO path (default: ./ainux-<release>-<arch>.iso)
  -k, --keep-work                  Keep working directories after completion
  -h, --help                       Show this help message
USAGE
}

cleanup() {
  if [[ ${CHROOT_MOUNTED:-0} -eq 1 ]]; then
    cleanup_chroot_env
  fi
  if [[ ${KEEP_WORK:-0} -eq 0 ]]; then
    echo "[cleanup] Removing work directory: $WORK_DIR"
    sudo rm -rf "$WORK_DIR"
  else
    echo "[cleanup] Keeping work directory at: $WORK_DIR"
  fi
}

check_dependencies() {
  local deps=(debootstrap mksquashfs xorriso isolinux grub-mkstandalone grub-mkrescue mtools rsync)
  for dep in "${deps[@]}"; do
    if ! command -v "$dep" >/dev/null 2>&1; then
      echo "Missing dependency: $dep" >&2
      echo "Install required packages: sudo apt-get install -y debootstrap squashfs-tools xorriso isolinux grub-pc-bin grub-efi-amd64-bin mtools rsync" >&2
      exit 1
    fi
  done
}

prepare_directories() {
  mkdir -p "$ROOTFS_DIR" "$ISO_DIR"
}

bootstrap_base() {
  echo "[bootstrap] Running debootstrap for $RELEASE/$ARCH"
  sudo debootstrap --arch="$ARCH" "$RELEASE" "$ROOTFS_DIR" "$MIRROR"
}

copy_overlay() {
  if [[ -d "$OVERLAY_DIR" ]]; then
    echo "[overlay] Applying overlay files"
    sudo rsync -aHAX "$OVERLAY_DIR"/ "$ROOTFS_DIR"/
  fi
}

configure_apt() {
  local sources_file="$CONFIG_DIR/sources.list"
  if [[ -f "$sources_file" ]]; then
    echo "[apt] Copying custom sources.list"
    sudo cp "$sources_file" "$ROOTFS_DIR/etc/apt/sources.list"
  fi
}

install_packages() {
  if [[ -f "$PACKAGES_FILE" ]]; then
    echo "[packages] Installing additional packages"
    sudo chroot "$ROOTFS_DIR" /usr/bin/env bash -c "apt-get update && grep -Ev '^[[:space:]]*(#|$)' /tmp/packages.txt | xargs -r apt-get install -y"
    sudo rm -f "$ROOTFS_DIR/tmp/packages.txt"
  fi
}

run_chroot_script() {
  if [[ -f "$CHROOT_SCRIPT" ]]; then
    echo "[chroot] Executing custom chroot setup script"
    sudo chroot "$ROOTFS_DIR" /usr/bin/env bash -c "/tmp/chroot_setup.sh"
    sudo rm -f "$ROOTFS_DIR/tmp/chroot_setup.sh"
  fi
}

prepare_chroot_env() {
  echo "[chroot] Mounting proc/sys/dev"
  sudo mount --bind /dev "$ROOTFS_DIR/dev"
  sudo mount --bind /dev/pts "$ROOTFS_DIR/dev/pts"
  sudo mount -t proc /proc "$ROOTFS_DIR/proc"
  sudo mount -t sysfs /sys "$ROOTFS_DIR/sys"
  sudo mount -t tmpfs tmpfs "$ROOTFS_DIR/run"
  CHROOT_MOUNTED=1
}

cleanup_chroot_env() {
  echo "[chroot] Unmounting proc/sys/dev"
  sudo umount -lf "$ROOTFS_DIR/dev/pts" || true
  sudo umount -lf "$ROOTFS_DIR/dev" || true
  sudo umount -lf "$ROOTFS_DIR/proc" || true
  sudo umount -lf "$ROOTFS_DIR/sys" || true
  sudo umount -lf "$ROOTFS_DIR/run" || true
  CHROOT_MOUNTED=0
}

seed_configuration_files() {
  echo "[seed] Copying configuration artifacts into chroot"
  sudo mkdir -p "$ROOTFS_DIR/tmp"
  if [[ -f "$PACKAGES_FILE" ]]; then
    sudo cp "$PACKAGES_FILE" "$ROOTFS_DIR/tmp/packages.txt"
  fi
  if [[ -f "$CHROOT_SCRIPT" ]]; then
    sudo cp "$CHROOT_SCRIPT" "$ROOTFS_DIR/tmp/chroot_setup.sh"
    sudo chmod +x "$ROOTFS_DIR/tmp/chroot_setup.sh"
  fi
  if [[ -d "$AI_CLIENT_DIR" ]]; then
    sudo rm -rf "$ROOTFS_DIR/tmp/ainux_ai"
    sudo cp -a "$AI_CLIENT_DIR" "$ROOTFS_DIR/tmp/"
  fi
  if [[ -d "$BRANDING_DIR" ]]; then
    sudo rm -rf "$ROOTFS_DIR/tmp/ainux_branding"
    sudo mkdir -p "$ROOTFS_DIR/tmp/ainux_branding"
    sudo cp -a "$BRANDING_DIR"/*.png "$ROOTFS_DIR/tmp/ainux_branding/" 2>/dev/null || true
  fi
}

configure_live_boot() {
  echo "[live] Setting up live boot configuration"
  sudo chroot "$ROOTFS_DIR" apt-get update
  sudo chroot "$ROOTFS_DIR" apt-get install -y --no-install-recommends linux-generic casper lupin-casper discover laptop-detect os-prober network-manager
  sudo chroot "$ROOTFS_DIR" apt-get clean
  sudo rm -f "$ROOTFS_DIR/etc/machine-id"
  sudo touch "$ROOTFS_DIR/etc/machine-id"
}

create_squashfs() {
  echo "[squashfs] Creating filesystem.squashfs"
  sudo mksquashfs "$ROOTFS_DIR" "$ISO_DIR/casper/filesystem.squashfs" -comp xz -e boot
  sudo cp "$ROOTFS_DIR/boot/vmlinuz" "$ISO_DIR/casper/vmlinuz"
  sudo cp "$ROOTFS_DIR/boot/initrd.img" "$ISO_DIR/casper/initrd"
}

prepare_iso_structure() {
  echo "[iso] Preparing ISO skeleton"
  sudo mkdir -p "$ISO_DIR/casper" "$ISO_DIR/isolinux" "$ISO_DIR/boot/grub"
  if [[ -f "$GRUB_CFG_FILE" ]]; then
    sudo cp "$GRUB_CFG_FILE" "$ISO_DIR/boot/grub/grub.cfg"
  else
    cat <<'GRUBCFG' | sudo tee "$ISO_DIR/boot/grub/grub.cfg" >/dev/null
set default=0
set timeout=5

menuentry "Ainux Live" {
    set gfxpayload=keep
    linux   /casper/vmlinuz boot=casper quiet splash ---
    initrd  /casper/initrd
}
GRUBCFG
  fi

  cat <<'ISOCFG' | sudo tee "$ISO_DIR/isolinux/isolinux.cfg" >/dev/null
UI vesamenu.c32
PROMPT 0
MENU TITLE Ainux Live ISO
TIMEOUT 50

LABEL live
  menu label ^Try Ainux without installing
  kernel /casper/vmlinuz
  append initrd=/casper/initrd boot=casper quiet splash ---
ISOCFG
}

create_manifest() {
  echo "[manifest] Generating package manifest"
  sudo chroot "$ROOTFS_DIR" dpkg-query -W --showformat='${Package} ${Version}\n' | sudo tee "$ISO_DIR/casper/filesystem.manifest" >/dev/null
  sudo cp "$ISO_DIR/casper/filesystem.manifest" "$ISO_DIR/casper/filesystem.manifest-desktop"
  echo "ubiquity (remove)" | sudo tee -a "$ISO_DIR/casper/filesystem.manifest-desktop" >/dev/null
}

create_md5sum() {
  echo "[checksum] Calculating md5sum.txt"
  (cd "$ISO_DIR" && sudo find . -type f -print0 | sudo xargs -0 md5sum) | sudo tee "$ISO_DIR/md5sum.txt" >/dev/null
}

assemble_iso() {
  local output_iso="${OUTPUT_PATH:-$(pwd)/ainux-$RELEASE-$ARCH.iso}"
  echo "[iso] Building ISO at $output_iso"
  sudo grub-mkrescue -o "$output_iso" "$ISO_DIR" -- -volid "$ISO_LABEL"
  echo "[iso] ISO created: $output_iso"
}

main() {
  trap cleanup EXIT
  if [[ $EUID -ne 0 ]]; then
    echo "This script must be run as root (try: sudo $0 ...)" >&2
    exit 1
  fi

  while [[ $# -gt 0 ]]; do
    case $1 in
      -r|--release)
        RELEASE="$2"; shift 2 ;;
      -a|--arch)
        ARCH="$2"; shift 2 ;;
      -m|--mirror)
        MIRROR="$2"; shift 2 ;;
      -l|--label)
        ISO_LABEL="$2"; shift 2 ;;
      -o|--output)
        OUTPUT_PATH="$2"; shift 2 ;;
      -k|--keep-work)
        KEEP_WORK=1; shift ;;
      -h|--help)
        usage; exit 0 ;;
      *)
        echo "Unknown option: $1" >&2
        usage
        exit 1 ;;
    esac
  done

  check_dependencies
  prepare_directories
  bootstrap_base
  seed_configuration_files
  prepare_chroot_env
  configure_apt
  install_packages
  configure_live_boot
  run_chroot_script
  cleanup_chroot_env
  copy_overlay
  prepare_iso_structure
  create_manifest
  create_squashfs
  create_md5sum
  assemble_iso
}

main "$@"
