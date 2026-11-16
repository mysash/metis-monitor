#!/bin/sh
# Automatic security updates for metis (DGX Spark, Ubuntu 24.04) – run once on the host as root:
#   sudo sh ~/stacks/metis-monitor/host/setup-unattended-upgrades.sh
#
# Policy:
#   - only Ubuntu security pockets (noble-security, ESM infra/apps security)
#   - never kernel, NVIDIA driver/libraries, CUDA, DKMS, Docker/containerd (do those deliberately via SSH)
#   - nothing from the NVIDIA Spark repository or NVIDIA PPAs (not in the allowed origins)
#   - no automatic reboot (the dashboard shows "Neustart nötig")
#   - never prompt: keep locally modified config files
set -eu

[ "$(id -u)" = 0 ] || { echo "bitte mit sudo ausführen" >&2; exit 1; }

DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends unattended-upgrades

cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF

cat > /etc/apt/apt.conf.d/52metis-unattended-upgrades <<'EOF'
// metis: security updates only, never GPU stack / kernel / container runtime, no reboot.
// Overrides the package default in 50unattended-upgrades (later files win for scalar options,
// lists are replaced via #clear).
#clear Unattended-Upgrade::Allowed-Origins;
#clear Unattended-Upgrade::Origins-Pattern;
Unattended-Upgrade::Allowed-Origins {
        "${distro_id}:${distro_codename}-security";
        "${distro_id}ESMApps:${distro_codename}-apps-security";
        "${distro_id}ESM:${distro_codename}-infra-security";
};

#clear Unattended-Upgrade::Package-Blacklist;
Unattended-Upgrade::Package-Blacklist {
        "^linux-image";
        "^linux-headers";
        "^linux-modules";
        "^linux-nvidia";
        "^linux-generic";
        "^linux-tools";
        "^linux-objects";
        "^linux-signatures";
        "^linux-restricted-modules";
        "^linux-firmware";
        "nvidia";
        "^libnvidia";
        "^cuda";
        "^libcuda";
        "^libcudnn";
        "^libnccl";
        "^libcublas";
        "^tensorrt";
        "^dkms";
        "-dkms$";
        "^xserver-xorg-video-nvidia";
        "^docker";
        "^containerd";
        "^runc";
        "^nvidia-container";
        "^libnvidia-container";
};

Unattended-Upgrade::Automatic-Reboot "false";
Unattended-Upgrade::Remove-Unused-Dependencies "false";
Unattended-Upgrade::Remove-New-Unused-Dependencies "false";
Unattended-Upgrade::MinimalSteps "true";
Unattended-Upgrade::SyslogEnable "true";

Dpkg::Options {
        "--force-confdef";
        "--force-confold";
};
EOF

echo
echo "== Probelauf (installiert nichts) =="
unattended-upgrade --dry-run --debug 2>&1 | grep -E "Allowed origins|Packages that will be upgraded|blacklist|No packages found|Initial" | head -40 || true
echo
echo "Timer: $(systemctl is-enabled apt-daily.timer) / $(systemctl is-enabled apt-daily-upgrade.timer)"
systemctl list-timers apt-daily-upgrade.timer --no-pager | sed -n 2p
echo "Fertig. Log: /var/log/unattended-upgrades/unattended-upgrades.log"
