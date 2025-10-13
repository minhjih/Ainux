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

# Create the default Ainux orchestrator user
if ! id -u ainux >/dev/null 2>&1; then
  useradd -m -s /bin/bash ainux
  echo "ainux:ainux" | chpasswd
  usermod -aG sudo,adm,video,docker ainux || true
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

# Seed shell profile with helper aliases
cat <<'BASHRC' >> /home/ainux/.bashrc
# Ainux automation helpers
alias ainux-hw-scan='sudo lshw -C display -C network'
alias ainux-driver-report='dpkg -l | grep -E "nvidia|cuda"'
alias ainux-diagnostics='sudo journalctl -p 3 -xb'
alias ainux-schedule='sudo /usr/local/bin/ainux-scheduler'
alias ainux-net-orchestrate='sudo /usr/local/bin/ainux-network-orchestrator'
alias ainux-cluster-health='sudo /usr/local/bin/ainux-cluster-health'
BASHRC

# Configure motd
cat <<'MOTD' > /etc/update-motd.d/99-ainux
#!/bin/sh
echo "Welcome to Ainux - the AI-native Ubuntu remix"
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

BLUEPRINT_ROOT="/usr/local/share/ainux/playbooks"

usage() {
  cat <<'USAGE'
Usage: ainux-scheduler <command> [options]

Commands:
  list                        List available automation blueprints
  blueprint <name> [vars]     Execute an Ansible blueprint (pass extra vars as KEY=VALUE)
  job <sbatch args>           Submit a SLURM job for hardware-aware scheduling
  status [squeue args]        Show queued jobs (via squeue)
  cancel <jobid>              Cancel a SLURM job (via scancel)
USAGE
}

list_blueprints() {
  find "$BLUEPRINT_ROOT" -type f \( -name '*.yml' -o -name '*.yaml' \) -printf '%P\n' | sort
}

ensure_ansible() {
  if ! command -v ansible-playbook >/dev/null 2>&1; then
    echo "ansible-playbook is required for blueprint execution" >&2
    exit 2
  fi
}

ensure_slurm_cmd() {
  local cmd="$1"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "${cmd} not available. Install slurm-client or connect to a scheduler." >&2
    exit 2
  fi
}

cmd="${1:-}"
case "$cmd" in
  list)
    list_blueprints
    ;;
  blueprint)
    ensure_ansible
    shift
    blueprint="${1:-}"
    if [[ -z "$blueprint" ]]; then
      echo "Blueprint name required" >&2
      usage
      exit 1
    fi
    shift || true
    if [[ "$blueprint" != *.yml && "$blueprint" != *.yaml ]]; then
      blueprint="${blueprint}.yml"
    fi
    target=""
    if [[ -f "${BLUEPRINT_ROOT}/${blueprint}" ]]; then
      target="${BLUEPRINT_ROOT}/${blueprint}"
    else
      target=$(find "$BLUEPRINT_ROOT" -type f -name "$blueprint" | head -n1 || true)
    fi
    if [[ -z "$target" ]]; then
      echo "Blueprint ${blueprint} not found under ${BLUEPRINT_ROOT}" >&2
      exit 1
    fi
    extra_vars=()
    for arg in "$@"; do
      extra_vars+=("--extra-vars" "$arg")
    done
    ansible-playbook "$target" "${extra_vars[@]}"
    ;;
  job)
    shift || true
    ensure_slurm_cmd sbatch
    sbatch "$@"
    ;;
  status)
    shift || true
    ensure_slurm_cmd squeue
    squeue "$@"
    ;;
  cancel)
    shift || true
    ensure_slurm_cmd scancel
    if [[ $# -eq 0 ]]; then
      echo "Provide job ID to cancel" >&2
      exit 1
    fi
    scancel "$@"
    ;;
  ""|-h|--help)
    usage
    ;;
  *)
    echo "Unknown command: $cmd" >&2
    usage
    exit 1
    ;;
esac
SCHEDULER
chmod +x /usr/local/bin/ainux-scheduler

cat <<'NETCTL' > /usr/local/bin/ainux-network-orchestrator
#!/usr/bin/env bash
set -euo pipefail

BLUEPRINT_ROOT="/usr/local/share/ainux/playbooks/network"

usage() {
  cat <<'USAGE'
Usage: ainux-network-orchestrator <command> [options]

Commands:
  scan                          Summarize NIC, VLAN, and packet stats
  plan <blueprint>              Show blueprint details
  apply <blueprint> [vars]      Execute network automation blueprint
USAGE
}

ensure_ansible() {
  if ! command -v ansible-playbook >/dev/null 2>&1; then
    echo "ansible-playbook is required for network automation" >&2
    exit 2
  fi
}

blueprint_path() {
  local name="$1"
  if [[ "$name" != *.yml && "$name" != *.yaml ]]; then
    name="${name}.yml"
  fi
  if [[ -f "${BLUEPRINT_ROOT}/${name}" ]]; then
    printf '%s' "${BLUEPRINT_ROOT}/${name}"
    return 0
  fi
  local found
  found=$(find "$BLUEPRINT_ROOT" -type f -name "$name" | head -n1 || true)
  if [[ -n "$found" ]]; then
    printf '%s' "$found"
    return 0
  fi
  echo "Blueprint $1 not found" >&2
  exit 1
}

cmd="${1:-}"
case "$cmd" in
  scan)
    shift || true
    echo "[NIC Inventory]"
    lshw -C network || true
    echo
    echo "[Link Metrics]"
    for iface in $(ls /sys/class/net); do
      echo "- $iface"
      ethtool "$iface" 2>/dev/null | grep -E 'Speed|Duplex|Link detected' || true
    done
    echo
    echo "[Traffic Snapshot]"
    ip -s link
    echo
    echo "[Active nftables policies]"
    nft list ruleset 2>/dev/null | sed 's/^/  /'
    ;;
  plan)
    shift || true
    blueprint="${1:-}"
    if [[ -z "$blueprint" ]]; then
      echo "Blueprint name required" >&2
      usage
      exit 1
    fi
    path=$(blueprint_path "$blueprint")
    echo "Showing blueprint: $path"
    cat "$path"
    ;;
  apply)
    ensure_ansible
    shift || true
    blueprint="${1:-}"
    if [[ -z "$blueprint" ]]; then
      echo "Blueprint name required" >&2
      usage
      exit 1
    fi
    shift || true
    path=$(blueprint_path "$blueprint")
    extra_vars=()
    for arg in "$@"; do
      extra_vars+=("--extra-vars" "$arg")
    done
    ansible-playbook "$path" "${extra_vars[@]}"
    ;;
  ""|-h|--help)
    usage
    ;;
  *)
    echo "Unknown command: $cmd" >&2
    usage
    exit 1
    ;;
esac
NETCTL
chmod +x /usr/local/bin/ainux-network-orchestrator

cat <<'HEALTH' > /usr/local/bin/ainux-cluster-health
#!/usr/bin/env bash
set -euo pipefail

echo "[GPU Health]"
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=name,temperature.gpu,utilization.gpu,memory.used --format=csv
else
  echo "nvidia-smi unavailable"
fi

echo
echo "[Sensors]"
if command -v sensors >/dev/null 2>&1; then
  sensors || true
else
  echo "lm-sensors not configured"
fi

echo
echo "[Power / BMC]"
if command -v ipmitool >/dev/null 2>&1; then
  ipmitool sdr list 2>/dev/null || echo "IPMI not accessible"
else
  echo "ipmitool not installed"
fi

echo
echo "[SLURM Jobs]"
if command -v squeue >/dev/null 2>&1; then
  squeue || true
else
  echo "slurm-client not installed"
fi

echo
echo "[Network Interfaces]"
ip -brief addr || true
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
