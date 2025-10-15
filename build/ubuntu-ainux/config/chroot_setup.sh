#!/usr/bin/env bash
set -euo pipefail

# This script runs inside the chroot environment to configure additional
# Ainux services.

export DEBIAN_FRONTEND=noninteractive

# Enable systemd services required for live boot by creating the target symlinks
enable_service() {
  local service="$1"
  local target_dir="/etc/systemd/system/multi-user.target.wants"
  mkdir -p "$target_dir"
  ln -sf "/lib/systemd/system/${service}" "$target_dir/${service}"
}

enable_service NetworkManager.service
enable_service ssh.service
enable_service ufw.service
enable_service gdm3.service

# Ensure NetworkManager owns all interfaces and seed predictable netplan
# defaults so DHCP works in hypervisors like VMware out of the box.
mkdir -p /etc/netplan
cat <<'NETPLAN' > /etc/netplan/01-ainux-network.yaml
network:
  version: 2
  renderer: NetworkManager
NETPLAN
netplan generate >/dev/null 2>&1 || true

# Guarantee the hostname is corrected on every boot (casper can reset it)
cat <<'HOSTSERVICE' > /etc/systemd/system/ainux-hostname.service
[Unit]
Description=Persist the Ainux hostname during live boots
After=systemd-remount-fs.service

[Service]
Type=oneshot
ExecStart=/usr/bin/hostnamectl set-hostname ainux
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
HOSTSERVICE
enable_service ainux-hostname.service

# Ensure the live environment consistently presents the Ainux identity.
echo "ainux" > /etc/hostname
if grep -q '^127\.0\.1\.1' /etc/hosts; then
  sed -i 's/^127\\.0\\.1\\.1.*/127.0.1.1\tainux/' /etc/hosts
else
  echo "127.0.1.1\tainux" >> /etc/hosts
fi
hostname ainux || true

mkdir -p /etc/cloud/cloud.cfg.d
cat <<'CLOUD' > /etc/cloud/cloud.cfg.d/99-ainux-preserve-hostname.cfg
preserve_hostname: true
manage_etc_hosts: false
CLOUD

# Create the default Ainux orchestrator user
if ! id -u ainux >/dev/null 2>&1; then
  useradd -m -s /bin/bash ainux
  echo "ainux:ainuxos" | chpasswd
  usermod -aG sudo,adm,video,docker ainux || true
fi

# Auto-login the Ainux operator on the live console so users are not
# prompted for credentials when booting the ISO in a VM or bare metal.
mkdir -p /etc/systemd/system/getty@tty1.service.d
cat <<'AUTOLOGIN' > /etc/systemd/system/getty@tty1.service.d/override.conf
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin ainux --noclear %I $TERM
AUTOLOGIN

cat <<'GDMCONF' > /etc/gdm3/custom.conf
[daemon]
AutomaticLoginEnable=true
AutomaticLogin=ainux

[security]

[xdmcp]

[chooser]

[debug]
Enable=false
GDMCONF

mkdir -p /etc/dconf/profile
cat <<'DPROFILE' > /etc/dconf/profile/gdm
user-db:user
system-db:gdm
DPROFILE
cat <<'UPROFILE' > /etc/dconf/profile/user
user-db:user
system-db:local
UPROFILE

mkdir -p /etc/dconf/db/gdm.d
cat <<'GDMBG' > /etc/dconf/db/gdm.d/00-ainux-background
[org/gnome/desktop/background]
picture-uri='file:///usr/share/ainux/branding/ainux.png'
picture-uri-dark='file:///usr/share/ainux/branding/ainux.png'
primary-color='#0A1324'
secondary-color='#0A1324'
GDMBG

cat <<'OSRELEASE' > /etc/os-release
NAME="Ainux"
VERSION="22.04 LTS (Jammy)"
ID=ainux
ID_LIKE=ubuntu
PRETTY_NAME="Ainux 22.04 LTS (Jammy)"
VERSION_ID="22.04"
HOME_URL="https://ainux.example.com"
SUPPORT_URL="https://ainux.example.com/support"
BUG_REPORT_URL="https://ainux.example.com/issues"
PRIVACY_POLICY_URL="https://ainux.example.com/privacy"
VERSION_CODENAME=jammy
UBUNTU_CODENAME=jammy
OSRELEASE

cat <<'LSBRELEASE' > /etc/lsb-release
DISTRIB_ID=Ainux
DISTRIB_RELEASE=22.04
DISTRIB_CODENAME=jammy
DISTRIB_DESCRIPTION="Ainux 22.04 LTS (Jammy)"
LSBRELEASE

cat <<'ISSUE' > /etc/issue
Ainux 22.04 LTS (Jammy) \n \l
ISSUE
printf 'Ainux 22.04 LTS (Jammy)\n' > /etc/issue.net

if [[ -f /etc/default/grub ]]; then
  sed -i 's/^GRUB_CMDLINE_LINUX_DEFAULT=.*/GRUB_CMDLINE_LINUX_DEFAULT="quiet splash usbcore.autosuspend=-1"/' /etc/default/grub
fi

mkdir -p /home/ainux/.config/ainux
cat <<'PROFILE' > /home/ainux/.config/ainux/profile.yaml
version: 1
identity:
  display_name: "Ainux Orchestrator"
  description: "Default operator account for AI-native automation"
workspace:
  repositories:
    - name: ainux-automation
      url: https://github.com/example/ainux-automation.git
accelerators:
  provisioning:
    default_driver: nvidia-driver-535
    cuda_toolkit: nvidia-cuda-toolkit
    container_runtime: nvidia-container-toolkit
PROFILE
chown -R ainux:ainux /home/ainux/.config

if [[ ! -f /home/ainux/.config/ainux/ai_client.json ]]; then
  cat <<'AICONFIG' > /home/ainux/.config/ainux/ai_client.json
{
  "version": 1,
  "default_provider": null,
  "providers": {}
}
AICONFIG
  chown ainux:ainux /home/ainux/.config/ainux/ai_client.json
  chmod 600 /home/ainux/.config/ainux/ai_client.json
fi

# Seed shell profile with helper aliases
cat <<'BASHRC' >> /home/ainux/.bashrc
# Ainux automation helpers
alias ainux-hw-scan='sudo lshw -C display -C network'
alias ainux-driver-report='dpkg -l | grep -E "nvidia|cuda"'
alias ainux-diagnostics='sudo journalctl -p 3 -xb'
alias ainux-schedule='sudo /usr/local/bin/ainux-scheduler'
alias ainux-net-orchestrate='sudo /usr/local/bin/ainux-network-orchestrator'
alias ainux-cluster-health='sudo /usr/local/bin/ainux-cluster-health'
alias ainux-hw='ainux-ai-chat hardware'
alias ainux-chat='ainux-ai-chat chat --interactive'
alias ainux-orchestrate='ainux-ai-chat orchestrate'
alias ainux-fabric='ainux-ai-chat context snapshot'
alias ainux-ui='ainux-ai-chat ui'
BASHRC

# Configure Docker to run reliably on read-only live media by preferring
# fuse-overlayfs. The configuration is skipped when a custom daemon.json
# already exists so that installed systems can override the storage driver.
mkdir -p /etc/docker
if [[ ! -f /etc/docker/daemon.json ]]; then
  cat <<'DOCKERCFG' > /etc/docker/daemon.json
{
  "storage-driver": "fuse-overlayfs",
  "features": {
    "buildkit": true
  }
}
DOCKERCFG
fi

# Install the GPT client toolkit for shell and automation use
if [[ -d /tmp/ainux_ai ]]; then
  install -d /usr/local/lib/ainux
  cp -a /tmp/ainux_ai /usr/local/lib/ainux/
  cat <<'AINUXCLIENT' > /usr/local/bin/ainux-client
#!/usr/bin/env bash
set -euo pipefail
PYTHONPATH="/usr/local/lib/ainux:${PYTHONPATH:-}" exec python3 -m ainux_ai "$@"
AINUXCLIENT
  chmod +x /usr/local/bin/ainux-client
  ln -sf ainux-client /usr/local/bin/ainux-ai-chat
  ln -sf /usr/local/bin/ainux-client /home/ainux/ainux-client
  chown ainux:ainux /home/ainux/ainux-client
  rm -rf /tmp/ainux_ai
fi

if [[ -d /tmp/ainux_branding ]]; then
  install -d /usr/share/ainux/branding
  cp -a /tmp/ainux_branding/*.png /usr/share/ainux/branding/ 2>/dev/null || true
  chmod 644 /usr/share/ainux/branding/*.png 2>/dev/null || true
  rm -rf /tmp/ainux_branding
fi

PYTHONPATH="/usr/local/lib/ainux:${PYTHONPATH:-}" python3 - <<'PY'
import base64
from pathlib import Path

from ainux_ai.ui import assets

branding_dir = Path("/usr/share/ainux/branding")
branding_dir.mkdir(parents=True, exist_ok=True)

background_dir = Path("/usr/share/backgrounds/ainux")
background_dir.mkdir(parents=True, exist_ok=True)

icon_sizes = [64, 128, 256, 512]
icon_root = Path("/usr/share/icons/hicolor")

sources = {
    "ainux.png": assets.DEFAULT_AINUX_LOGO_BASE64,
    "ainux_penguin.png": assets.DEFAULT_AINUX_PENGUIN_BASE64,
}

def resolve_asset(name: str, fallback_b64: str) -> bytes:
    override = branding_dir / name
    if override.exists():
        return override.read_bytes()
    data = base64.b64decode(fallback_b64)
    if not override.exists():
        override.write_bytes(data)
    return data

for filename, b64_data in sources.items():
    data = resolve_asset(filename, b64_data)

    target = background_dir / filename
    target.write_bytes(data)

    if filename == "ainux.png":
        default_wallpaper = Path("/usr/share/backgrounds/ainux_default.png")
        default_wallpaper.write_bytes(data)

    for size in icon_sizes:
        icon_dir = icon_root / f"{size}x{size}" / "apps"
        icon_dir.mkdir(parents=True, exist_ok=True)
        icon_path = icon_dir / "ainux.png"
        icon_path.write_bytes(data)

symbolic_dir = icon_root / "scalable" / "apps"
symbolic_dir.mkdir(parents=True, exist_ok=True)
symbolic_icon = symbolic_dir / "ainux.svg"
if not symbolic_icon.exists():
    svg_content = (
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 128 128'>\n"
        "  <rect width='128' height='128' rx='28' fill='#0A1324'/>\n"
        "  <path d='M64 22c-20 0-36 16-36 36s16 36 36 36 36-16 36-36S84 22 64 22zm0 8c15.5 0 28 12.5 28 28s-12.5 28-28 28S36 73.5 36 58 48.5 30 64 30z' fill='#7BDCF7'/>\n"
        "  <circle cx='52' cy='54' r='6' fill='white'/>\n"
        "  <circle cx='76' cy='54' r='6' fill='white'/>\n"
        "  <circle cx='52' cy='54' r='2.5' fill='#0A1324'/>\n"
        "  <circle cx='76' cy='54' r='2.5' fill='#0A1324'/>\n"
        "  <path d='M64 70c-8 0-15 4-18 10 6 6 12 9 18 9s12-3 18-9c-3-6-10-10-18-10z' fill='#F5A623'/>\n"
        "</svg>\n"
    )
    symbolic_icon.write_text(svg_content, encoding="utf-8")
PY

mkdir -p /etc/dconf/db/local.d
cat <<'LOCALDCONF' > /etc/dconf/db/local.d/00-ainux-desktop
[org/gnome/desktop/background]
picture-uri='file:///usr/share/ainux/branding/ainux.png'
picture-uri-dark='file:///usr/share/ainux/branding/ainux.png'
primary-color='#0A1324'
secondary-color='#0A1324'

[org/gnome/desktop/screensaver]
picture-uri='file:///usr/share/backgrounds/ainux/ainux.png'

[org/gnome/shell]
favorite-apps=['firefox.desktop','org.gnome.Terminal.desktop','ainux-studio.desktop']

[org/gnome/desktop/interface]
color-scheme='prefer-dark'
gtk-theme='Yaru-dark'
icon-theme='Yaru'
cursor-theme='Yaru'
enable-animations=true

[org/gnome/settings-daemon/plugins/power]
sleep-inactive-ac-timeout=0
sleep-inactive-ac-type='nothing'
LOCALDCONF

dconf update

gtk-update-icon-cache -f /usr/share/icons/hicolor 2>/dev/null || true

cat <<'DESKTOP' > /usr/share/applications/ainux-studio.desktop
[Desktop Entry]
Type=Application
Name=Ainux Studio
Comment=Launch the AI-native orchestration studio
Exec=/usr/local/bin/ainux-client ui
Icon=ainux
Terminal=false
Categories=Utility;Development;
StartupNotify=true
DESKTOP

install -d /home/ainux/.config/autostart
cat <<'AUTOSTART' > /home/ainux/.config/autostart/ainux-studio.desktop
[Desktop Entry]
Type=Application
Name=Ainux Studio
Comment=Launch the AI-native orchestration studio
Exec=/usr/local/bin/ainux-client ui
Icon=ainux
Terminal=false
X-GNOME-Autostart-enabled=true
AUTOSTART
chown -R ainux:ainux /home/ainux/.config/autostart

if [[ ! -f /home/ainux/.config/ainux/context_fabric.json ]]; then
  PYTHONPATH="/usr/local/lib/ainux:${PYTHONPATH:-}" python3 - <<'PY'
from ainux_ai.context import ContextFabric

fabric = ContextFabric()
fabric.merge_metadata({
    "seeded_by": "chroot_setup",
    "profile": "ainux-operator",
})
fabric.ingest_setting("operator.username", "ainux", scope="system")
fabric.record_event("fabric.bootstrap", {"source": "chroot_setup"})
fabric.save("/home/ainux/.config/ainux/context_fabric.json")
PY
  chown ainux:ainux /home/ainux/.config/ainux/context_fabric.json
  chmod 600 /home/ainux/.config/ainux/context_fabric.json
fi

# Configure motd
cat <<'MOTD' > /etc/update-motd.d/99-ainux
#!/bin/sh
echo "Welcome to Ainux - the AI-native operating system"
MOTD
chmod +x /etc/update-motd.d/99-ainux

# Harden SSH defaults
sed -i 's/^#PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config
sed -i 's/^#PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config

# Prepare CUDA verification helper
cat <<'CUDA_CHECK' > /usr/local/bin/ainux-verify-cuda
#!/usr/bin/env bash
set -euo pipefail

if ! command -v nvidia-smi >/dev/null 2>&1; then
  echo "nvidia-smi not found. Ensure NVIDIA drivers are installed." >&2
  exit 1
fi

nvidia-smi
nvcc --version || echo "nvcc not available - install CUDA toolkit"
CUDA_CHECK
chmod +x /usr/local/bin/ainux-verify-cuda

# Ensure localhost inventory for ansible-driven blueprints
mkdir -p /etc/ansible
cat <<'ANSIBLE_HOSTS' > /etc/ansible/hosts
[local]
localhost ansible_connection=local
ANSIBLE_HOSTS

# Seed AI-driven scheduling blueprints and helpers
mkdir -p /usr/local/share/ainux/playbooks/hardware
mkdir -p /usr/local/share/ainux/playbooks/network

cat <<'HW_BLUEPRINT' > /usr/local/share/ainux/playbooks/hardware/maintenance_window.yml
---
- name: Coordinate maintenance window for accelerator fleet
  hosts: localhost
  gather_facts: true
  vars:
    maintenance_window: "{{ maintenance_window | default('22:00-23:00') }}"
    drain_slurm_jobs: "{{ drain_slurm | default(true) }}"
    reboot_after: "{{ reboot | default(false) }}"
  tasks:
    - name: Summarize requested maintenance plan
      ansible.builtin.debug:
        msg:
          - "Maintenance window: {{ maintenance_window }}"
          - "Services impacted: {{ services | default(['docker']) }}"
          - "Drain SLURM jobs: {{ drain_slurm_jobs }}"
          - "Reboot after maintenance: {{ reboot_after }}"

    - name: Trigger graceful stop for declared services
      ansible.builtin.service:
        name: "{{ item }}"
        state: stopped
      loop: "{{ services | default(['docker']) }}"
      when: (services | default(['docker'])) | length > 0

    - name: Detect availability of SLURM tooling
      ansible.builtin.command: command -v scontrol
      register: ainux_scontrol_check
      changed_when: false
      failed_when: false

    - name: Drain local node from SLURM scheduler if requested
      ansible.builtin.command:
        cmd: "scontrol update nodename={{ ansible_hostname }} state=DRAIN reason='Ainux maintenance window'"
      when:
        - drain_slurm_jobs | bool
        - ainux_scontrol_check.rc == 0
      changed_when: true

    - name: Capture NVIDIA device telemetry before change
      ansible.builtin.command: nvidia-smi --query-gpu=name,temperature.gpu,utilization.gpu --format=csv,noheader
      register: gpu_report
      failed_when: false

    - name: Display GPU telemetry snapshot
      ansible.builtin.debug:
        var: gpu_report.stdout_lines

    - name: Flag that a reboot should be scheduled
      ansible.builtin.debug:
        msg: "Reboot will be triggered at the end of the maintenance window"
      when: reboot_after | bool

    - name: Create summary artifact
      ansible.builtin.copy:
        dest: /var/log/ainux/maintenance_plan.log
        content: |
          ---
          generated_at: "{{ ansible_date_time.iso8601 }}"
          maintenance_window: "{{ maintenance_window }}"
          services: {{ (services | default(['docker'])) | to_nice_yaml(indent=2) }}
          drain_slurm_jobs: {{ drain_slurm_jobs }}
          reboot_after: {{ reboot_after }}
          gpu_snapshot: |
            {{ gpu_report.stdout | default('n/a') }}
        owner: root
        group: adm
        mode: '0640'

    - name: Schedule reboot if requested
      ansible.builtin.command: shutdown -r +5 "Ainux scheduled maintenance reboot"
      when: reboot_after | bool
      changed_when: true
HW_BLUEPRINT

cat <<'NET_BLUEPRINT' > /usr/local/share/ainux/playbooks/network/packet_shaping.yml
---
- name: Configure adaptive packet policies
  hosts: localhost
  gather_facts: false
  vars:
    qos_target: "{{ qos_target | default('latency') }}"
    interface: "{{ interface | default('eth0') }}"
    rate_limit_mbps: "{{ rate_limit_mbps | default(200) }}"
    nft_table: ainux-qos
  tasks:
    - name: Ensure nftables package is present
      ansible.builtin.package:
        name: nftables
        state: present

    - name: Create nftables table for ainux policies
      ansible.builtin.command: "nft add table inet {{ nft_table }}"
      register: nft_table_create
      failed_when: false
      changed_when: "'already exists' not in nft_table_create.stderr"

    - name: Flush existing ruleset within table
      ansible.builtin.command: "nft flush table inet {{ nft_table }}"

    - name: Apply shaping chain
      ansible.builtin.command: >-
        nft add chain inet {{ nft_table }} qos { type filter hook postrouting priority 0 \; }
      register: nft_chain
      failed_when: false
      changed_when: "'already exists' not in nft_chain.stderr"

    - name: Configure rate limit rule
      ansible.builtin.command: >-
        nft add rule inet {{ nft_table }} qos oifname {{ interface }} limit rate {{ rate_limit_mbps }} mbytes/second counter accept
      register: nft_rule
      failed_when: false
      changed_when: "'already exists' not in nft_rule.stderr"

    - name: Ensure nftables include directory exists
      ansible.builtin.file:
        path: /etc/nftables.d
        state: directory
        owner: root
        group: root
        mode: '0755'

    - name: Persist nftables configuration
      ansible.builtin.copy:
        dest: /etc/nftables.d/ainux-qos.nft
        content: |
          table inet {{ nft_table }} {
            chain qos {
              type filter hook postrouting priority 0;
              oifname {{ interface }} limit rate {{ rate_limit_mbps }} mbytes/second counter accept
            }
          }
        owner: root
        group: root
        mode: '0644'

    - name: Ensure main nftables config includes drop-in directory
      ansible.builtin.lineinfile:
        path: /etc/nftables.conf
        regexp: '^include "/etc/nftables.d/\\*\\.nft"'
        line: 'include "/etc/nftables.d/*.nft"'
        create: yes

    - name: Reload nftables service
      ansible.builtin.service:
        name: nftables
        state: restarted
NET_BLUEPRINT

mkdir -p /etc/nftables.d
mkdir -p /var/log/ainux

cat <<'SCHEDULER' > /usr/local/bin/ainux-scheduler
#!/usr/bin/env bash
set -euo pipefail
exec /usr/local/bin/ainux-ai-chat scheduler "$@"
SCHEDULER
chmod +x /usr/local/bin/ainux-scheduler

cat <<'NETCTL' > /usr/local/bin/ainux-network-orchestrator
#!/usr/bin/env bash
set -euo pipefail
exec /usr/local/bin/ainux-ai-chat network "$@"
NETCTL
chmod +x /usr/local/bin/ainux-network-orchestrator

cat <<'HEALTH' > /usr/local/bin/ainux-cluster-health
#!/usr/bin/env bash
set -euo pipefail
exec /usr/local/bin/ainux-ai-chat cluster "$@"
HEALTH
chmod +x /usr/local/bin/ainux-cluster-health

# Preconfigure cloud-init datasource for local builds
cat <<'CLOUDCFG' > /etc/cloud/cloud.cfg.d/99-ainux.cfg
users:
  - default
system_info:
  default_user:
    name: ainux
    lock_passwd: true
    gecos: Ainux Orchestrator
    groups: [adm, cdrom, dip, lxd, plugdev, sudo]
    shell: /bin/bash
CLOUDCFG

apt-get clean
rm -rf /var/lib/apt/lists/*
