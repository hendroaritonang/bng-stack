# BNG Stack

Custom BNG stack for VPP-based PPPoE broadband access — one-line installer.

## Components

| Component | Description |
|---|---|
| **accel-pppd** | Custom build with VyOS VPP plugin (`libvyos_vpp.so`) |
| **pppoe-neigh-sync** | PPPoE session → Linux ARP/neighbor sync daemon |
| **bng-monitor** | FastAPI web dashboard (sessions, policer, RADIUS, alerts) |
| **VPP configs** | startup.conf + bootstrap.cli templates |
| **FRR** | BGP/zebra for route redistribution |

## Prerequisites

- Ubuntu 22.04 or 24.04 (amd64)
- VPP custom build (VyOS `vyos20260322204722`) installed
- FRR installed

## Install

```bash
curl -fsSL https://hendroaritonang.github.io/bng-stack/install.sh | sudo bash
```

Interactive wizard will ask for:
- Network interfaces (uplink, PPPoE)
- VPP CPU/hugepage config
- Number of BR instances + VLAN/IP per BR
- RADIUS server + secret
- BNG Monitor port + credentials

## Manual apt install

```bash
echo "deb [trusted=yes] https://hendroaritonang.github.io/bng-stack stable main" \
  | sudo tee /etc/apt/sources.list.d/bng-stack.list
sudo apt update
sudo apt install bng-stack-runtime bng-monitor
sudo bng-stack-configure
sudo bng-stack-start
```

## Commands

```bash
bng-stack-configure    # re-run setup wizard
bng-stack-start        # start all services in correct order
bng-stack-status       # show service + session status
```

## Build locally

```bash
git clone https://github.com/hendroaritonang/bng-stack
cd bng-stack
make VERSION=1.0.0
# Output: dist/*.deb + dist/apt-repo/
```

## Release new version

```bash
git tag v1.0.1
git push origin v1.0.1
# GitHub Actions builds .deb, creates Release, deploys apt repo to GitHub Pages
```
