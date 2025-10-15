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
#   debootstrap, squashfs-tools, xorriso, isolinux, mtools, dosfstools
#   approx. 10GB free disk space
#   network access to Ubuntu mirrors
#
# The script intentionally keeps the logic modular: each phase is
# encapsulated inside a function so that integrators can mix in their own
# automation logic or replace pieces with Ainux-native services.

RELEASE="jammy"
ARCH="amd64"
DEFAULT_ARCHIVE_MIRROR="http://archive.ubuntu.com/ubuntu"
DEFAULT_PORTS_MIRROR="http://ports.ubuntu.com/ubuntu-ports"
MIRROR=""
CUSTOM_MIRROR=0
declare -a MIRROR_CANDIDATES=()

APT_RETRY_COUNT=5
APT_RETRY_TIMEOUT=30
APT_FORCE_IPV4=1
ISO_LABEL="AINUX"
WORK_DIR="$SCRIPT_DIR/work"
ROOTFS_DIR="$WORK_DIR/chroot"
ISO_DIR="$WORK_DIR/iso"
CONFIG_DIR="$SCRIPT_DIR/config"
OVERLAY_DIR="$SCRIPT_DIR/overlay"
AI_CLIENT_DIR="$REPO_ROOT/ainux_ai"
BRANDING_DIR="$REPO_ROOT/folder"
EFI_STAGING_DIR="$WORK_DIR/efi"
METADATA_FILE="$WORK_DIR/.build-meta"
STAGE_DIR="$WORK_DIR/.stages"
KEEP_WORK=1
DISK_IMAGE_PATH=""
DISK_IMAGE_SIZE="16G"
OUTPUT_PATH=""
CHROOT_MOUNTED=0
declare -a LOOP_DEVICES=()
declare -a DISK_MOUNTS=()
declare -a DISK_MOUNT_REMOVE=()

EFI_GRUB_TARGET=""
EFI_BOOT_FILENAME=""
EFI_PACKAGE_NAME=""

HOST_ARCH=""
USE_FOREIGN_STAGE=0
QEMU_STATIC_BIN=""
FOREIGN_QEMU_BASENAME=""

PACKAGES_FILE="$CONFIG_DIR/packages.txt"
CHROOT_SCRIPT="$CONFIG_DIR/chroot_setup.sh"
GRUB_CFG_FILE="$CONFIG_DIR/grub.cfg"
LIVE_KERNEL_PARAMS="boot=casper quiet splash usbcore.autosuspend=-1 ---"

usage() {
  cat <<USAGE
Usage: $0 [options]

Options:
  -r, --release <ubuntu release>   Ubuntu codename to base on (default: $RELEASE)
  -a, --arch <architecture>        Target architecture (default: $ARCH)
  -m, --mirror <url>               Ubuntu mirror URL (default: auto)
  -l, --label <label>              ISO label (default: $ISO_LABEL)
  -o, --output <path>              Output ISO path (default: <repo>/output/ainux-<release>-<arch>.iso)
  --disk-image <path>              Optional raw disk image output path
  --disk-size <size>               Size for disk image (default: $DISK_IMAGE_SIZE)
  -c, --clean-work                 Remove working directories after completion (default: keep)
      --keep-work                  Deprecated alias; working directories are kept by default
  -h, --help                       Show this help message
USAGE
}

determine_efi_target() {
  case "$ARCH" in
    amd64|x86_64)
      EFI_GRUB_TARGET="x86_64-efi"
      EFI_BOOT_FILENAME="BOOTX64.EFI"
      EFI_PACKAGE_NAME="grub-efi-amd64-bin"
      ;;
    arm64|aarch64)
      EFI_GRUB_TARGET="arm64-efi"
      EFI_BOOT_FILENAME="BOOTAA64.EFI"
      EFI_PACKAGE_NAME="grub-efi-arm64-bin"
      ;;
    *)
      echo "[warn] Unsupported architecture for EFI generation: $ARCH. EFI boot files will not be produced." >&2
      EFI_GRUB_TARGET=""
      EFI_BOOT_FILENAME=""
      EFI_PACKAGE_NAME=""
      ;;
  esac
}

cleanup() {
  if [[ ${CHROOT_MOUNTED:-0} -eq 1 ]]; then
    cleanup_chroot_env
  fi
  remove_foreign_qemu_helper || true
  if (( ${#DISK_MOUNTS[@]} )); then
    for (( idx=${#DISK_MOUNTS[@]}-1; idx>=0; idx-- )); do
      local mount_point="${DISK_MOUNTS[$idx]}"
      local remove_dir="${DISK_MOUNT_REMOVE[$idx]}"
      if [[ -n "$mount_point" && -d "$mount_point" ]]; then
        if mountpoint -q "$mount_point"; then
          sudo umount -lf "$mount_point" || true
        fi
        if [[ "$remove_dir" == "1" ]]; then
          sudo rmdir "$mount_point" 2>/dev/null || true
        fi
      fi
    done
  fi
  if (( ${#LOOP_DEVICES[@]} )); then
    for loopdev in "${LOOP_DEVICES[@]}"; do
      if [[ -n "$loopdev" ]] && losetup "$loopdev" >/dev/null 2>&1; then
        sudo losetup -d "$loopdev" || true
      fi
    done
  fi
  if [[ -d "$EFI_STAGING_DIR" && ${KEEP_WORK:-0} -eq 0 ]]; then
    sudo rm -rf "$EFI_STAGING_DIR"
  fi
  if [[ ${KEEP_WORK:-0} -eq 0 ]]; then
    echo "[cleanup] Removing work directory: $WORK_DIR"
    sudo rm -rf "$WORK_DIR"
  else
    echo "[cleanup] Keeping work directory at: $WORK_DIR"
  fi
}

stage_marker_path() {
  local stage="$1"
  echo "$STAGE_DIR/$stage"
}

mark_stage() {
  local stage="$1"
  local metadata="${2:-}"
  mkdir -p "$STAGE_DIR"
  printf '%s\n' "$metadata" > "$(stage_marker_path "$stage")"
}

clear_stage() {
  local stage="$1"
  local path
  path="$(stage_marker_path "$stage")"
  if [[ -f "$path" ]]; then
    rm -f "$path"
  fi
}

read_stage_metadata() {
  local stage="$1"
  local path
  path="$(stage_marker_path "$stage")"
  if [[ -f "$path" ]]; then
    cat "$path"
  fi
}

should_skip_stage() {
  local stage="$1"
  local expected="${2:-}"
  local current
  current="$(read_stage_metadata "$stage")"
  if [[ -z "$current" ]]; then
    return 1
  fi
  if [[ "$current" == "$expected" ]]; then
    echo "[resume] Stage '$stage' already satisfied (metadata match); skipping"
    return 0
  fi
  echo "[resume] Stage '$stage' metadata changed (expected '$expected', found '$current'); rerunning"
  return 1
}

validate_existing_metadata() {
  if [[ ${KEEP_WORK:-0} -eq 0 ]]; then
    return
  fi
  if [[ ! -f "$METADATA_FILE" ]]; then
    return
  fi
  local stored_release=""
  local stored_arch=""
  while IFS='=' read -r key value; do
    case "$key" in
      release) stored_release="$value" ;;
      arch) stored_arch="$value" ;;
    esac
  done < "$METADATA_FILE"
  if [[ -n "$stored_release" && "$stored_release" != "$RELEASE" ]]; then
    echo "[resume] Existing work directory targets release '$stored_release'. Use --clean-work or remove $WORK_DIR for a fresh build." >&2
    exit 1
  fi
  if [[ -n "$stored_arch" && "$stored_arch" != "$ARCH" ]]; then
    echo "[resume] Existing work directory targets architecture '$stored_arch'. Use --clean-work or remove $WORK_DIR for a fresh build." >&2
    exit 1
  fi
}

ensure_build_metadata() {
  mkdir -p "$WORK_DIR"
  cat > "$METADATA_FILE" <<EOF
release=$RELEASE
arch=$ARCH
timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF
  mkdir -p "$STAGE_DIR"
}

normalize_arch() {
  local value="$1"
  case "$value" in
    x86_64)
      echo "amd64" ;;
    aarch64)
      echo "arm64" ;;
    armv7l)
      echo "armhf" ;;
    i686|i386)
      echo "i386" ;;
    *)
      echo "$value" ;;
  esac
}

register_loop_device() {
  local loopdev="$1"
  if [[ -n "$loopdev" ]]; then
    LOOP_DEVICES+=("$loopdev")
  fi
}

register_mount_point() {
  local mount_point="$1"
  local remove_dir="${2:-0}"
  if [[ -n "$mount_point" ]]; then
    DISK_MOUNTS+=("$mount_point")
    DISK_MOUNT_REMOVE+=("$remove_dir")
  fi
}

add_mirror_candidate() {
  local candidate="$1"
  if [[ -z "$candidate" ]]; then
    return
  fi
  for existing in "${MIRROR_CANDIDATES[@]}"; do
    if [[ "$existing" == "$candidate" ]]; then
      return
    fi
  done
  MIRROR_CANDIDATES+=("$candidate")
}

fallback_mirrors_for_arch() {
  local target_arch="$(normalize_arch "$1")"
  case "$target_arch" in
    arm64|aarch64)
      cat <<'EOF'
http://archive.ubuntu.com/ubuntu
http://kr.archive.ubuntu.com/ubuntu
http://mirror.kakao.com/ubuntu
EOF
      ;;
    amd64|x86_64)
      cat <<'EOF'
http://kr.archive.ubuntu.com/ubuntu
http://mirror.kakao.com/ubuntu
http://ftp.harukasan.org/ubuntu
EOF
      ;;
    *)
      cat <<'EOF'
http://archive.ubuntu.com/ubuntu
http://kr.archive.ubuntu.com/ubuntu
EOF
      ;;
  esac
}

render_sources_with_candidates() {
  local template="$1"
  local dest="$2"
  shift 2
  local -a candidates=("$@")
  if [[ ! -f "$template" || ${#candidates[@]} -eq 0 ]]; then
    return
  fi

  local tmp_file
  tmp_file="$(mktemp)"
  local idx=0
  for mirror in "${candidates[@]}"; do
    if [[ -z "$mirror" ]]; then
      continue
    fi
    local escaped
    escaped="$(printf '%s\n' "$mirror" | sed 's/[&\\/]/\\&/g')"
    if (( idx > 0 )); then
      printf '\n' >> "$tmp_file"
    fi
    sed "s|@UBUNTU_MIRROR@|$escaped|g" "$template" >> "$tmp_file"
    idx=$(( idx + 1 ))
  done
  sudo mkdir -p "$(dirname "$dest")"
  sudo cp "$tmp_file" "$dest"
  rm -f "$tmp_file"
}

write_apt_retry_config() {
  local conf_dir="$ROOTFS_DIR/etc/apt/apt.conf.d"
  sudo mkdir -p "$conf_dir"
  local tmp_file
  tmp_file="$(mktemp)"
  {
    printf 'Acquire::Retries "%d";\n' "$APT_RETRY_COUNT"
    printf 'Acquire::http::Timeout "%d";\n' "$APT_RETRY_TIMEOUT"
    printf 'Acquire::https::Timeout "%d";\n' "$APT_RETRY_TIMEOUT"
    printf 'Acquire::ftp::Timeout "%d";\n' "$APT_RETRY_TIMEOUT"
    printf 'Acquire::http::Pipeline-Depth "0";\n'
    if [[ $APT_FORCE_IPV4 -eq 1 ]]; then
      printf 'Acquire::ForceIPv4 "true";\n'
    fi
  } > "$tmp_file"
  sudo cp "$tmp_file" "$conf_dir/99ainux-retries"
  rm -f "$tmp_file"
}

default_mirror_for_arch() {
  local target_arch="$(normalize_arch "$1")"
  case "$target_arch" in
    arm64|armhf|armel)
      echo "$DEFAULT_PORTS_MIRROR"
      ;;
    *)
      echo "$DEFAULT_ARCHIVE_MIRROR"
      ;;
  esac
}

resolve_host_arch() {
  if command -v dpkg >/dev/null 2>&1; then
    HOST_ARCH="$(dpkg --print-architecture)"
  else
    HOST_ARCH="$(normalize_arch "$(uname -m)")"
  fi
  HOST_ARCH="$(normalize_arch "$HOST_ARCH")"
}

resolve_qemu_static() {
  case "$ARCH" in
    amd64|x86_64)
      QEMU_STATIC_BIN="/usr/bin/qemu-x86_64-static" ;;
    arm64|aarch64)
      QEMU_STATIC_BIN="/usr/bin/qemu-aarch64-static" ;;
    armhf)
      QEMU_STATIC_BIN="/usr/bin/qemu-arm-static" ;;
    armel)
      QEMU_STATIC_BIN="/usr/bin/qemu-arm-static" ;;
    i386)
      QEMU_STATIC_BIN="/usr/bin/qemu-i386-static" ;;
    riscv64)
      QEMU_STATIC_BIN="/usr/bin/qemu-riscv64-static" ;;
    *)
      QEMU_STATIC_BIN=""
      ;;
  esac
}

check_dependencies() {
  local cmd_deps=(debootstrap mksquashfs xorriso rsync mkfs.vfat)
  local missing=()
  for dep in "${cmd_deps[@]}"; do
    if ! command -v "$dep" >/dev/null 2>&1; then
      missing+=("$dep")
    fi
  done

  if [[ -n "$DISK_IMAGE_PATH" ]]; then
    local disk_cmds=(parted losetup mkfs.ext4 blkid)
    for dep in "${disk_cmds[@]}"; do
      if ! command -v "$dep" >/dev/null 2>&1; then
        missing+=("$dep")
      fi
    done
  fi

  local isolinux_bin="/usr/lib/ISOLINUX/isolinux.bin"
  local ldlinux_c32="/usr/lib/syslinux/modules/bios/ldlinux.c32"
  if [[ ! -f "$isolinux_bin" ]]; then
    missing+=("isolinux (file $isolinux_bin)")
  fi
  if [[ ! -f "$ldlinux_c32" ]]; then
    missing+=("syslinux modules (file $ldlinux_c32)")
  fi

  if [[ $USE_FOREIGN_STAGE -eq 1 ]]; then
    resolve_qemu_static
    if [[ -z "$QEMU_STATIC_BIN" ]]; then
      missing+=("qemu-user-static (unsupported architecture mapping for $ARCH)")
    elif [[ ! -x "$QEMU_STATIC_BIN" ]]; then
      missing+=("qemu-user-static (binary $QEMU_STATIC_BIN)")
    fi
  fi

  if (( ${#missing[@]} )); then
    echo "[error] Missing build dependencies:" >&2
    printf '  - %s\n' "${missing[@]}" >&2
    local hint_pkgs="debootstrap squashfs-tools xorriso isolinux mtools dosfstools rsync"
    if [[ -n "$DISK_IMAGE_PATH" ]]; then
      hint_pkgs+=" parted e2fsprogs util-linux"
    fi
    if [[ $USE_FOREIGN_STAGE -eq 1 ]]; then
      hint_pkgs+=" qemu-user-static binfmt-support"
    fi
    echo "[hint] Install required packages: sudo apt-get install -y $hint_pkgs" >&2
    exit 1
  fi
}

prepare_directories() {
  if [[ ${KEEP_WORK:-0} -eq 0 ]]; then
    sudo rm -rf "$WORK_DIR"
  fi
  mkdir -p "$ROOTFS_DIR" "$ISO_DIR" "$EFI_STAGING_DIR" "$STAGE_DIR"
}

sync_resolv_conf() {
  if [[ -e /etc/resolv.conf ]]; then
    sudo mkdir -p "$ROOTFS_DIR/etc"
    if [[ -L "$ROOTFS_DIR/etc/resolv.conf" || -f "$ROOTFS_DIR/etc/resolv.conf" ]]; then
      sudo rm -f "$ROOTFS_DIR/etc/resolv.conf"
    fi
    sudo cp /etc/resolv.conf "$ROOTFS_DIR/etc/resolv.conf"
  else
    echo "[warn] Host /etc/resolv.conf not found; DNS queries inside the chroot may fail." >&2
  fi
}

remove_foreign_qemu_helper() {
  if [[ -n "$FOREIGN_QEMU_BASENAME" ]]; then
    local helper_path="$ROOTFS_DIR/usr/bin/$FOREIGN_QEMU_BASENAME"
    if [[ -e "$helper_path" ]]; then
      echo "[bootstrap] Removing QEMU helper from target rootfs"
      sudo rm -f "$helper_path"
    fi
    FOREIGN_QEMU_BASENAME=""
  fi
}

run_foreign_second_stage() {
  local mounted=0
  echo "[bootstrap] Preparing mounts for foreign second stage"
  sudo mkdir -p \
    "$ROOTFS_DIR/dev" "$ROOTFS_DIR/dev/pts" \
    "$ROOTFS_DIR/proc" "$ROOTFS_DIR/sys" "$ROOTFS_DIR/run"
  sync_resolv_conf
  sudo mount --bind /dev "$ROOTFS_DIR/dev"
  sudo mount --bind /dev/pts "$ROOTFS_DIR/dev/pts"
  sudo mount -t proc /proc "$ROOTFS_DIR/proc"
  sudo mount -t sysfs /sys "$ROOTFS_DIR/sys"
  sudo mount -t tmpfs tmpfs "$ROOTFS_DIR/run"
  mounted=1

  echo "[bootstrap] Executing second-stage debootstrap inside chroot"
  if ! sudo chroot "$ROOTFS_DIR" /debootstrap/debootstrap --second-stage; then
    echo "[error] Foreign second stage failed while configuring base packages" >&2
    if [[ -f "$ROOTFS_DIR/debootstrap/debootstrap.log" ]]; then
      echo "[error] Dumping tail of debootstrap log:" >&2
      sudo tail -n 50 "$ROOTFS_DIR/debootstrap/debootstrap.log" >&2 || true
      if [[ -d "$WORK_DIR" ]]; then
        sudo cp "$ROOTFS_DIR/debootstrap/debootstrap.log" "$WORK_DIR/debootstrap.log" || true
        echo "[hint] Full log preserved at $WORK_DIR/debootstrap.log" >&2
      fi
    fi
    KEEP_WORK=1
    if [[ $mounted -eq 1 ]]; then
      sudo umount -lf "$ROOTFS_DIR/dev/pts" || true
      sudo umount -lf "$ROOTFS_DIR/dev" || true
      sudo umount -lf "$ROOTFS_DIR/proc" || true
      sudo umount -lf "$ROOTFS_DIR/sys" || true
      sudo umount -lf "$ROOTFS_DIR/run" || true
    fi
    exit 1
  fi

  if [[ ! -x "$ROOTFS_DIR/usr/bin/apt-get" || ! -x "$ROOTFS_DIR/usr/bin/dpkg" ]]; then
    echo "[error] Foreign second stage completed without installing core apt/dpkg tools." >&2
    echo "[error] 이는 대개 QEMU 에뮬레이션 중단(예: SIGSEGV)이나 누락된 의존성으로 인해 발생합니다." >&2
    if [[ -f "$ROOTFS_DIR/debootstrap/debootstrap.log" ]]; then
      echo "[hint] Inspect $ROOTFS_DIR/debootstrap/debootstrap.log 또는 $WORK_DIR/debootstrap.log for details." >&2
    fi
    echo "[hint] 동일 아키텍처 호스트에서 빌드하거나 qemu-user-static/ binfmt 설정을 다시 확인해 주세요." >&2
    KEEP_WORK=1
    if [[ $mounted -eq 1 ]]; then
      sudo umount -lf "$ROOTFS_DIR/dev/pts" || true
      sudo umount -lf "$ROOTFS_DIR/dev" || true
      sudo umount -lf "$ROOTFS_DIR/proc" || true
      sudo umount -lf "$ROOTFS_DIR/sys" || true
      sudo umount -lf "$ROOTFS_DIR/run" || true
    fi
    exit 1
  fi

  if [[ $mounted -eq 1 ]]; then
    sudo umount -lf "$ROOTFS_DIR/dev/pts" || true
    sudo umount -lf "$ROOTFS_DIR/dev" || true
    sudo umount -lf "$ROOTFS_DIR/proc" || true
    sudo umount -lf "$ROOTFS_DIR/sys" || true
    sudo umount -lf "$ROOTFS_DIR/run" || true
  fi
}

bootstrap_base() {
  echo "[bootstrap] Running debootstrap for $RELEASE/$ARCH"
  local stage_meta="release=$RELEASE arch=$ARCH"
  if should_skip_stage "bootstrap" "$stage_meta"; then
    if [[ -f "$ROOTFS_DIR/etc/os-release" ]]; then
      return
    fi
    echo "[resume] Bootstrap marker present but root filesystem is missing; rebuilding" >&2
    clear_stage "bootstrap"
    clear_stage "packages"
    clear_stage "chroot-script"
  fi
  if [[ -d "$ROOTFS_DIR" && -n "$(ls -A "$ROOTFS_DIR" 2>/dev/null)" ]]; then
    echo "[resume] Root filesystem at $ROOTFS_DIR already exists without a matching stage marker." >&2
    echo "[resume] Use --clean-work or remove the directory to rebuild from scratch." >&2
    exit 1
  fi
  local success=0
  local attempt=0
  if [[ $USE_FOREIGN_STAGE -eq 1 ]]; then
    echo "[bootstrap] Host architecture ($HOST_ARCH) differs from target ($ARCH); using foreign bootstrap"
    resolve_qemu_static
    if [[ -z "$QEMU_STATIC_BIN" || ! -x "$QEMU_STATIC_BIN" ]]; then
      echo "[error] Required qemu-user-static binary not found for $ARCH (expected $QEMU_STATIC_BIN)" >&2
      exit 1
    fi
  fi

  for candidate in "${MIRROR_CANDIDATES[@]}"; do
    if [[ -z "$candidate" ]]; then
      continue
    fi
    attempt=$(( attempt + 1 ))
    if (( attempt == 1 )); then
      echo "[bootstrap] Using mirror: $candidate"
    else
      echo "[bootstrap] Retrying with fallback mirror: $candidate"
    fi
    if [[ $USE_FOREIGN_STAGE -eq 1 ]]; then
      if sudo debootstrap --arch="$ARCH" --foreign "$RELEASE" "$ROOTFS_DIR" "$candidate"; then
        MIRROR="$candidate"
        sudo mkdir -p "$ROOTFS_DIR/usr/bin"
        local qemu_basename
        qemu_basename="$(basename "$QEMU_STATIC_BIN")"
        sudo cp "$QEMU_STATIC_BIN" "$ROOTFS_DIR/usr/bin/"
        FOREIGN_QEMU_BASENAME="$qemu_basename"
        run_foreign_second_stage
        success=1
        break
      fi
    else
      if sudo debootstrap --arch="$ARCH" "$RELEASE" "$ROOTFS_DIR" "$candidate"; then
        MIRROR="$candidate"
        success=1
        break
      fi
    fi
    local status=$?
    echo "[bootstrap] debootstrap failed with exit $status using mirror $candidate" >&2
    if (( attempt < ${#MIRROR_CANDIDATES[@]} )); then
      echo "[bootstrap] Cleaning partial rootfs before next attempt" >&2
      sudo rm -rf "$ROOTFS_DIR"
      sudo mkdir -p "$ROOTFS_DIR"
    fi
  done

  if [[ $success -ne 1 ]]; then
    echo "[error] Failed to bootstrap $RELEASE/$ARCH after trying ${#MIRROR_CANDIDATES[@]} mirrors" >&2
    exit 1
  fi

  mark_stage "bootstrap" "$stage_meta"
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
    if (( ${#MIRROR_CANDIDATES[@]} > 1 )); then
      echo "[apt] Rendering sources.list with primary mirror $MIRROR and ${#MIRROR_CANDIDATES[@]} total candidates"
    else
      echo "[apt] Rendering sources.list for $ARCH using mirror $MIRROR"
    fi
    render_sources_with_candidates "$sources_file" "$ROOTFS_DIR/etc/apt/sources.list" "${MIRROR_CANDIDATES[@]}"
  fi
  write_apt_retry_config
}

install_packages() {
  local pkg_hash="none"
  if [[ -f "$PACKAGES_FILE" ]]; then
    pkg_hash="$(sha256sum "$PACKAGES_FILE" | awk '{print $1}')"
  fi
  local stage_meta="hash=$pkg_hash"
  if should_skip_stage "packages" "$stage_meta"; then
    return
  fi
  if [[ ! -f "$PACKAGES_FILE" ]]; then
    echo "[packages] No additional packages requested; skipping"
    mark_stage "packages" "$stage_meta"
    return
  fi

  echo "[packages] Installing additional packages"
  sudo tee "$ROOTFS_DIR/tmp/install_packages.sh" >/dev/null <<'INSTALLPKG'
#!/usr/bin/env bash
set -euo pipefail

/usr/bin/apt-get update

while IFS= read -r line; do
  pkg="$(printf '%s\n' "$line" | sed -e 's/#.*$//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
  if [[ -z "$pkg" ]]; then
    continue
  fi

  optional=0
  if [[ "${pkg:0:1}" == "?" ]]; then
    optional=1
    pkg="${pkg:1}"
  fi

  if [[ $optional -eq 1 ]]; then
    if /usr/bin/apt-get install -y "$pkg"; then
      continue
    fi
    status=$?
    echo "[packages] Optional package $pkg unavailable (exit $status); skipping" >&2
    /usr/bin/apt-get -y --fix-broken install >/dev/null 2>&1 || true
  else
    /usr/bin/apt-get install -y "$pkg"
  fi
done < /tmp/packages.txt

/usr/bin/apt-get clean
INSTALLPKG
  sudo chmod +x "$ROOTFS_DIR/tmp/install_packages.sh"
  sudo chroot "$ROOTFS_DIR" /tmp/install_packages.sh
  sudo rm -f "$ROOTFS_DIR/tmp/install_packages.sh" "$ROOTFS_DIR/tmp/packages.txt"
  mark_stage "packages" "$stage_meta"
}

run_chroot_script() {
  local script_hash="none"
  if [[ -f "$CHROOT_SCRIPT" ]]; then
    script_hash="$(sha256sum "$CHROOT_SCRIPT" | awk '{print $1}')"
  fi
  local stage_meta="hash=$script_hash"
  if should_skip_stage "chroot-script" "$stage_meta"; then
    return
  fi
  if [[ ! -f "$CHROOT_SCRIPT" ]]; then
    echo "[chroot] No custom chroot setup script provided; skipping"
    mark_stage "chroot-script" "$stage_meta"
    return
  fi
  echo "[chroot] Executing custom chroot setup script"
  sudo chroot "$ROOTFS_DIR" /usr/bin/env bash -c "/tmp/chroot_setup.sh"
  sudo rm -f "$ROOTFS_DIR/tmp/chroot_setup.sh"
  mark_stage "chroot-script" "$stage_meta"
}

generate_efi_bootloader() {
  if [[ -z "$EFI_GRUB_TARGET" || -z "$EFI_BOOT_FILENAME" ]]; then
    echo "[efi] EFI generation skipped for architecture $ARCH"
    return
  fi

  echo "[efi] Generating GRUB EFI binary inside chroot"
  local tmp_cfg="$ROOTFS_DIR/tmp/iso-grub.cfg"
  sudo tee "$tmp_cfg" >/dev/null <<GRUB
set default=0
set timeout=5

menuentry "Ainux Live" {
    search --file --set=root /casper/vmlinuz
    linux /casper/vmlinuz $LIVE_KERNEL_PARAMS
    initrd /casper/initrd
}
GRUB
  sudo chroot "$ROOTFS_DIR" grub-mkstandalone -O "$EFI_GRUB_TARGET" -o /tmp/"$EFI_BOOT_FILENAME" "boot/grub/grub.cfg=/tmp/iso-grub.cfg"
  sudo cp "$ROOTFS_DIR/tmp/$EFI_BOOT_FILENAME" "$EFI_STAGING_DIR/$EFI_BOOT_FILENAME"
  sudo rm -f "$ROOTFS_DIR/tmp/$EFI_BOOT_FILENAME" "$tmp_cfg"
}

prepare_chroot_env() {
  echo "[chroot] Mounting proc/sys/dev"
  sync_resolv_conf
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
  sudo chroot "$ROOTFS_DIR" /usr/bin/apt-get update
  local required_pkgs=(linux-generic casper discover laptop-detect os-prober network-manager)
  local optional_pkgs=(lupin-casper)
  if [[ "$ARCH" == "amd64" || "$ARCH" == "x86_64" ]]; then
    required_pkgs+=(grub-pc-bin)
  fi
  if [[ -n "$EFI_PACKAGE_NAME" ]]; then
    required_pkgs+=("$EFI_PACKAGE_NAME")
  fi

  if [[ ${#required_pkgs[@]} -gt 0 ]]; then
    sudo chroot "$ROOTFS_DIR" /usr/bin/apt-get install -y --no-install-recommends "${required_pkgs[@]}"
  fi

  for pkg in "${optional_pkgs[@]}"; do
    if sudo chroot "$ROOTFS_DIR" /usr/bin/apt-cache show "$pkg" >/dev/null 2>&1; then
      if ! sudo chroot "$ROOTFS_DIR" /usr/bin/apt-get install -y --no-install-recommends "$pkg"; then
        echo "[live] Optional package '$pkg' failed to install; continuing without it" >&2
      fi
    else
      echo "[live] Optional package '$pkg' not available on current mirror; skipping" >&2
    fi
  done
  sudo chroot "$ROOTFS_DIR" /usr/bin/apt-get clean
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
    cat <<GRUBCFG | sudo tee "$ISO_DIR/boot/grub/grub.cfg" >/dev/null
set default=0
set timeout=5

menuentry "Ainux Live" {
    set gfxpayload=keep
    linux   /casper/vmlinuz $LIVE_KERNEL_PARAMS
    initrd  /casper/initrd
}
GRUBCFG
  fi

  cat <<ISOCFG | sudo tee "$ISO_DIR/isolinux/isolinux.cfg" >/dev/null
UI vesamenu.c32
PROMPT 0
MENU TITLE Ainux Live ISO
TIMEOUT 50

LABEL live
  menu label ^Try Ainux without installing
  kernel /casper/vmlinuz
  append initrd=/casper/initrd $LIVE_KERNEL_PARAMS
ISOCFG
}

stage_bootloader_files() {
  echo "[bootloader] Copying isolinux and GRUB assets"
  local isolinux_bin="/usr/lib/ISOLINUX/isolinux.bin"
  local ldlinux_c32="/usr/lib/syslinux/modules/bios/ldlinux.c32"
  if [[ ! -f "$isolinux_bin" || ! -f "$ldlinux_c32" ]]; then
    echo "[error] isolinux assets not found. Ensure the 'isolinux' package is installed." >&2
    exit 1
  fi
  sudo cp "$isolinux_bin" "$ISO_DIR/isolinux/"
  sudo cp "$ldlinux_c32" "$ISO_DIR/isolinux/"
  if [[ -n "$EFI_BOOT_FILENAME" && -f "$EFI_STAGING_DIR/$EFI_BOOT_FILENAME" ]]; then
    sudo mkdir -p "$ISO_DIR/EFI/BOOT"
    sudo cp "$EFI_STAGING_DIR/$EFI_BOOT_FILENAME" "$ISO_DIR/EFI/BOOT/$EFI_BOOT_FILENAME"
  fi
}

create_efi_image() {
  if [[ -z "$EFI_BOOT_FILENAME" || ! -f "$ISO_DIR/EFI/BOOT/$EFI_BOOT_FILENAME" ]]; then
    echo "[efi] Skipping EFI image generation (EFI binary missing)"
    return
  fi

  echo "[efi] Creating EFI system partition image"
  local efi_img="$ISO_DIR/efi.img"
  sudo dd if=/dev/zero of="$efi_img" bs=1M count=20 status=none
  sudo mkfs.vfat "$efi_img" >/dev/null
  local mnt_dir
  mnt_dir="$(mktemp -d)"
  sudo mount -o loop "$efi_img" "$mnt_dir"
  sudo mkdir -p "$mnt_dir/EFI/BOOT"
  sudo cp "$ISO_DIR/EFI/BOOT/$EFI_BOOT_FILENAME" "$mnt_dir/EFI/BOOT/$EFI_BOOT_FILENAME"
  sudo umount "$mnt_dir"
  rmdir "$mnt_dir"
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

create_disk_image() {
  if [[ -z "$DISK_IMAGE_PATH" ]]; then
    echo "[disk] Skipping raw disk image creation (not requested)"
    return
  fi

  local output_path="$DISK_IMAGE_PATH"
  echo "[disk] Building raw disk image at $output_path ($DISK_IMAGE_SIZE)"
  sudo mkdir -p "$(dirname "$output_path")"
  sudo rm -f "$output_path"
  sudo truncate -s "$DISK_IMAGE_SIZE" "$output_path"

  echo "[disk] Partitioning disk image"
  sudo parted -s "$output_path" mklabel gpt
  sudo parted -s "$output_path" mkpart ESP fat32 1MiB 513MiB
  sudo parted -s "$output_path" set 1 boot on
  sudo parted -s "$output_path" set 1 esp on
  sudo parted -s "$output_path" mkpart primary ext4 513MiB 100%

  local loopdev
  loopdev=$(sudo losetup --find --show --partscan "$output_path")
  register_loop_device "$loopdev"
  sudo partprobe "$loopdev" >/dev/null 2>&1 || true
  sleep 1

  local part1="${loopdev}p1"
  local part2="${loopdev}p2"
  if [[ ! -e "$part1" && -e "${loopdev}p01" ]]; then
    part1="${loopdev}p01"
  fi
  if [[ ! -e "$part2" && -e "${loopdev}p02" ]]; then
    part2="${loopdev}p02"
  fi
  if [[ ! -e "$part1" || ! -e "$part2" ]]; then
    echo "[error] Unable to locate loop partitions for $loopdev" >&2
    exit 1
  fi

  echo "[disk] Formatting EFI ($part1) and root ($part2) partitions"
  sudo mkfs.vfat -F 32 "$part1" >/dev/null
  sudo mkfs.ext4 -F "$part2" >/dev/null

  local root_mount
  root_mount="$(mktemp -d)"
  sudo mount "$part2" "$root_mount"
  register_mount_point "$root_mount" 1
  sudo mkdir -p "$root_mount/boot/efi"
  sudo mount "$part1" "$root_mount/boot/efi"
  register_mount_point "$root_mount/boot/efi" 0

  echo "[disk] Syncing root filesystem into disk image"
  sudo rsync -aHAX --delete "$ROOTFS_DIR"/ "$root_mount"/

  local root_uuid efi_uuid
  root_uuid="$(sudo blkid -s UUID -o value "$part2")"
  efi_uuid="$(sudo blkid -s UUID -o value "$part1")"
  if [[ -n "$root_uuid" && -n "$efi_uuid" ]]; then
    sudo tee "$root_mount/etc/fstab" >/dev/null <<FSTAB
UUID=$root_uuid / ext4 defaults 0 1
UUID=$efi_uuid /boot/efi vfat umask=0077 0 1
FSTAB
  fi

  sudo truncate -s0 "$root_mount/etc/machine-id"
  sudo touch "$root_mount/etc/machine-id"

  echo "[disk] Installing bootloader inside disk image"
  local bind
  for bind in dev dev/pts proc sys run; do
    sudo mount --bind "/$bind" "$root_mount/$bind"
    register_mount_point "$root_mount/$bind" 0
  done

  local grub_pkgs=()
  case "$ARCH" in
    amd64)
      grub_pkgs=(grub-efi-amd64-signed shim-signed grub-pc)
      ;;
    arm64)
      grub_pkgs=(grub-efi-arm64-signed shim-signed)
      ;;
    *)
      grub_pkgs=("grub-efi-$ARCH")
      ;;
  esac

  if (( ${#grub_pkgs[@]} )); then
    local install_needed=()
    local pkg
    for pkg in "${grub_pkgs[@]}"; do
      if ! sudo chroot "$root_mount" dpkg -s "$pkg" >/dev/null 2>&1; then
        install_needed+=("$pkg")
      fi
    done
    if (( ${#install_needed[@]} )); then
      sudo chroot "$root_mount" /usr/bin/apt-get update
      if ! sudo chroot "$root_mount" /usr/bin/apt-get install -y "${install_needed[@]}"; then
        echo "[warn] Failed to install GRUB packages (${install_needed[*]}) inside disk image; continuing" >&2
      fi
    fi
  fi

  case "$ARCH" in
    amd64)
      sudo chroot "$root_mount" grub-install --target=x86_64-efi --efi-directory=/boot/efi --bootloader-id=Ainux --recheck
      sudo chroot "$root_mount" grub-install --target=i386-pc --recheck "$loopdev" || true
      ;;
    arm64)
      sudo chroot "$root_mount" grub-install --target=arm64-efi --efi-directory=/boot/efi --bootloader-id=Ainux --recheck
      ;;
    *)
      sudo chroot "$root_mount" grub-install --efi-directory=/boot/efi --bootloader-id=Ainux --recheck || true
      ;;
  esac
  sudo chroot "$root_mount" update-grub

  # Tear down mounts so the image file can be attached immediately
  if (( ${#DISK_MOUNTS[@]} )); then
    for (( idx=${#DISK_MOUNTS[@]}-1; idx>=0; idx-- )); do
      local mount_point="${DISK_MOUNTS[$idx]}"
      local remove_dir="${DISK_MOUNT_REMOVE[$idx]}"
      if [[ -n "$mount_point" && -d "$mount_point" ]]; then
        if mountpoint -q "$mount_point"; then
          sudo umount -lf "$mount_point" || true
        fi
        if [[ "$remove_dir" == "1" ]]; then
          sudo rmdir "$mount_point" 2>/dev/null || true
        fi
      fi
    done
  fi
  DISK_MOUNTS=()
  DISK_MOUNT_REMOVE=()

  if [[ -n "$loopdev" ]]; then
    if losetup "$loopdev" >/dev/null 2>&1; then
      sudo losetup -d "$loopdev" || true
    fi
  fi
  LOOP_DEVICES=()

  echo "[disk] Disk image ready: $output_path"
}

human_readable_bytes() {
  local bytes="$1"
  if command -v numfmt >/dev/null 2>&1; then
    numfmt --to=iec --suffix=B "$bytes"
  else
    echo "${bytes}B"
  fi
}

build_xorriso_base_args() {
  local -n _args_ref="$1"
  _args_ref=(-as mkisofs
    -r -V "$ISO_LABEL" -J -l
    -b isolinux/isolinux.bin
    -c isolinux/boot.cat
    -no-emul-boot -boot-load-size 4 -boot-info-table
    -eltorito-alt-boot -e efi.img -no-emul-boot)
  local isohdpfx="/usr/lib/ISOLINUX/isohdpfx.bin"
  if [[ -f "$isohdpfx" ]]; then
    _args_ref+=(-isohybrid-mbr "$isohdpfx" -isohybrid-gpt-basdat)
  fi
}

estimate_iso_size_bytes() {
  local base_args=()
  build_xorriso_base_args base_args
  local cmd=(xorriso)
  cmd+=("${base_args[@]}" "-quiet" "-print-size" "$ISO_DIR")
  local raw_output
  if ! raw_output="$("${cmd[@]}")"; then
    echo "[error] Failed to estimate ISO size with xorriso." >&2
    exit 1
  fi
  local sector_count
  sector_count=$(echo "$raw_output" | awk 'NF && $1 ~ /^[0-9]+$/ {print $1; exit}')
  if [[ -z "$sector_count" ]]; then
    echo "[error] Unexpected xorriso size output: $raw_output" >&2
    exit 1
  fi
  echo $(( sector_count * 2048 ))
}

assemble_iso() {
  local output_iso="$OUTPUT_PATH"
  echo "[iso] Building ISO at $output_iso"
  local output_dir="$(dirname "$output_iso")"
  local available_bytes
  available_bytes=$(df -PB1 "$output_dir" | awk 'NR==2 {print $4}')
  if [[ -z "$available_bytes" ]]; then
    echo "[error] Failed to determine free space for $output_dir" >&2
    exit 1
  fi
  local iso_bytes
  iso_bytes=$(estimate_iso_size_bytes)
  local safety_margin=$(( 100 * 1024 * 1024 ))
  local estimated_bytes=$(( iso_bytes + safety_margin ))
  if (( available_bytes < estimated_bytes )); then
    echo "[error] Not enough free space in $output_dir for ISO creation." >&2
    echo "[error] Estimated requirement: $(human_readable_bytes "$estimated_bytes"), available: $(human_readable_bytes "$available_bytes")." >&2
    echo "[hint] Free additional space or rerun with --output pointing to a larger volume." >&2
    exit 1
  fi
  local iso_human
  iso_human=$(human_readable_bytes "$iso_bytes")
  echo "[iso] Estimated ISO size: $iso_human (plus safety margin)"
  rm -f "$output_iso"
  local base_args=()
  build_xorriso_base_args base_args
  local xorriso_cmd=("sudo" "xorriso")
  xorriso_cmd+=("${base_args[@]}" "-o" "$output_iso" "$ISO_DIR")
  "${xorriso_cmd[@]}"
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
      --disk-image)
        DISK_IMAGE_PATH="$2"; shift 2 ;;
      --disk-size)
        DISK_IMAGE_SIZE="$2"; shift 2 ;;
      -c|--clean-work)
        KEEP_WORK=0; shift ;;
      -k|--keep-work)
        echo "[warn] --keep-work is the default behaviour; flag retained for compatibility" >&2
        KEEP_WORK=1; shift ;;
      -h|--help)
        usage; exit 0 ;;
      *)
        echo "Unknown option: $1" >&2
        usage
        exit 1 ;;
    esac
  done

  resolve_host_arch
  local normalized_target="$(normalize_arch "$ARCH")"
  if [[ -n "$HOST_ARCH" && "$HOST_ARCH" != "$normalized_target" ]]; then
    USE_FOREIGN_STAGE=1
    ARCH="$normalized_target"
  else
    ARCH="$normalized_target"
  fi

  MIRROR_CANDIDATES=()
  if [[ -z "$MIRROR" ]]; then
    MIRROR="$(default_mirror_for_arch "$ARCH")"
    CUSTOM_MIRROR=0
    echo "[mirror] Using default mirror for $ARCH: $MIRROR"
  else
    CUSTOM_MIRROR=1
    echo "[mirror] Using custom mirror: $MIRROR"
  fi
  add_mirror_candidate "$MIRROR"
  if [[ $CUSTOM_MIRROR -eq 0 ]]; then
    while IFS= read -r fallback; do
      add_mirror_candidate "$fallback"
    done < <(fallback_mirrors_for_arch "$ARCH")
    if (( ${#MIRROR_CANDIDATES[@]} > 1 )); then
      echo "[mirror] Fallback mirrors registered: ${MIRROR_CANDIDATES[*]:1}"
    fi
  fi

  local default_iso_path="$REPO_ROOT/output/ainux-$RELEASE-$ARCH.iso"
  if [[ -z "$OUTPUT_PATH" ]]; then
    OUTPUT_PATH="$default_iso_path"
    echo "[output] Default ISO output path: $OUTPUT_PATH"
  else
    echo "[output] Custom ISO output path: $OUTPUT_PATH"
  fi
  mkdir -p "$(dirname "$OUTPUT_PATH")"

  validate_existing_metadata
  determine_efi_target
  check_dependencies
  prepare_directories
  ensure_build_metadata
  bootstrap_base
  seed_configuration_files
  prepare_chroot_env
  configure_apt
  install_packages
  configure_live_boot
  run_chroot_script
  generate_efi_bootloader
  cleanup_chroot_env
  remove_foreign_qemu_helper
  copy_overlay
  prepare_iso_structure
  stage_bootloader_files
  create_manifest
  create_squashfs
  create_efi_image
  create_md5sum
  create_disk_image
  assemble_iso
}

main "$@"
