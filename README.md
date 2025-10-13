# Ainux

Ainux is an AI-native operating system concept that layers intelligent
automation and hardware orchestration on top of a familiar Linux user
experience. This repository now contains both the high-level architecture
vision and the initial tooling required to assemble an Ubuntu-based live ISO
that demonstrates those ideas.

## Repository Structure

- `docs/ai_friendly_os_design.md` – Design document outlining the Ainux vision
  and automation architecture.
- `build/ubuntu-ainux/` – Scripts and configuration to generate an Ubuntu
  remix ISO with Ainux defaults baked in, including AI-driven maintenance,
  scheduling, and network automation helpers.

## Quick Start

To build the prototype ISO:

```bash
git clone https://github.com/<your-org>/Ainux.git
cd Ainux/build/ubuntu-ainux
sudo ./build.sh --release jammy --arch amd64 --output ~/ainux-jammy.iso
```

Refer to `build/ubuntu-ainux/README.md` for prerequisites and customization
options, including the new scheduling/packet-management blueprints seeded into
the live image.
