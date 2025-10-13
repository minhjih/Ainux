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
  rm -rf /tmp/ainux_ai
fi

if [[ -d /tmp/ainux_branding ]]; then
  install -d /usr/share/ainux/branding
  cp -a /tmp/ainux_branding/*.png /usr/share/ainux/branding/ 2>/dev/null || true
  chmod 644 /usr/share/ainux/branding/*.png 2>/dev/null || true
  rm -rf /tmp/ainux_branding
fi

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
