#!/usr/bin/env bash
set -eu

REPO_ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
PROFILE=${GUEST_PROFILE:-ubuntu-v1-hybrid}
WORK=${GUEST_WORK:-/tmp/proclimits-guest/$PROFILE}
# The images are the same bytes on every run, so they live apart from the keys and overlays a run creates -
# and outside /tmp, which a WSL or machine restart wipes. Re-downloading a gigabyte is the slowest thing here.
DOWNLOADS=${GUEST_DOWNLOADS:-${XDG_CACHE_HOME:-$HOME/.cache}/proclimits-guest}
SSH_PORT=${GUEST_SSH_PORT:-2222}
GUEST_USER=bench

UBUNTU=https://cloud-images.ubuntu.com/releases/22.04/release
FEDORA=https://download.fedoraproject.org/pub/fedora/linux/releases/45/Cloud/x86_64/images
DEBIAN=https://cloud.debian.org/images/cloud/trixie

# A profile is one machine to run the suite on: the Ubuntu ones exist for cgroup v1, the Fedora one for a
# second distribution and the Debian one for a long-term kernel. Nothing in the suite skips itself, so each
# profile says by name which tests its machine cannot carry, and everything else has to pass.
case $PROFILE in
  ubuntu-v1-hybrid | ubuntu-v1-legacy)
    # cgroup v1 cannot be produced on a modern host, and systemd honours the flags below only up to v255.
    # Ubuntu 22.04 ships systemd 249. Its kernel and initrd are published next to the image, which is what
    # lets the command line be set from here instead of editing the image.
    #
    # hybrid mounts the v1 controllers next to a controller-less cgroup2, so the sensor has to notice that the
    # unified hierarchy carries nothing and fall back per controller. legacy is plain v1.
    IMAGE_URL=${GUEST_IMAGE_URL:-$UBUNTU/ubuntu-22.04-server-cloudimg-amd64.img}
    KERNEL_URL=${GUEST_KERNEL_URL:-$UBUNTU/unpacked/ubuntu-22.04-server-cloudimg-amd64-vmlinuz-generic}
    INITRD_URL=${GUEST_INITRD_URL:-$UBUNTU/unpacked/ubuntu-22.04-server-cloudimg-amd64-initrd-generic}
    PACKAGES=docker.io
    PREPARE=''
    # What cgroup v1 costs, and it is the point of this profile: user scopes get no resource controllers, and
    # docker drives cgroups through cgroupfs, so a slice name means nothing to it. A rootless container has no
    # delegation to fall back on here, so podman is not installed either, nor is kind.
    # Everything else - docker, sudo, systemd, two cores - this guest has, and a test needing it has to pass.
    EXCLUDE='not unified and not systemd_slices and not kubernetes and not podman'
    INTERFACE=cgroup-v1
    CGROUP_ARGS='systemd.unified_cgroup_hierarchy=0'
    [ "$PROFILE" = ubuntu-v1-legacy ] && CGROUP_ARGS="$CGROUP_ARGS systemd.legacy_systemd_cgroup_controller=1"
    CMDLINE="root=/dev/vda1 console=ttyS0 $CGROUP_ARGS"
    ;;
  fedora-v2)
    # A second distribution, with a much newer systemd, on the cgroup v2 layout it defaults to. Nothing has to
    # be passed to the kernel, so the image boots through its own bootloader.
    # Fedora publishes no "latest" name, so the build is pinned. Bump it when the release is retired.
    IMAGE_URL=${GUEST_IMAGE_URL:-$FEDORA/Fedora-Cloud-Base-Generic-45-1.4.x86_64.qcow2}
    KERNEL_URL=''
    INITRD_URL=''
    # podman too: the second machine for the rootless lane, and on a newer systemd than any hosted runner -
    # which is what decides how much a user manager may delegate.
    PACKAGES='moby-engine podman'
    # A full cgroup v2 machine, so only the cluster tests are out: kind is not installed here.
    EXCLUDE='not kubernetes'
    INTERFACE=cgroup-v2
    # Only the test plumbing needs this: the suite bind-mounts the repository into containers, which SELinux
    # denies without relabelling. The sensor itself reads `/proc` and `/sys` and is not affected.
    PREPARE='setenforce 0'
    CMDLINE=''
    ;;
  debian-v2)
    # The hosted runners and the Fedora guest run the newest kernels. This guest runs 6.12, the long-term
    # kernel of Debian 13 and of RHEL 10, so cgroup v2 is also checked on a kernel servers run today. Debian
    # keeps its dated builds, so the build is pinned like Fedora's. The image boots through its own bootloader.
    IMAGE_URL=${GUEST_IMAGE_URL:-$DEBIAN/20261103-2651/debian-13-genericcloud-amd64-20261103-2651.qcow2}
    KERNEL_URL=''
    INITRD_URL=''
    # Rootless podman reaches the user manager over the user's own D-Bus, which Debian ships separately.
    # Without it podman falls back to writing cgroups itself, where it may not write, and every limit is lost.
    PACKAGES='docker.io podman dbus-user-session'
    # A full cgroup v2 machine, so only the cluster tests are out: kind is not installed here.
    EXCLUDE='not kubernetes'
    INTERFACE=cgroup-v2
    # The user manager started with the first ssh login, before the packages arrived, so it has no bus yet.
    PREPARE="systemctl restart user@\$(id -u $GUEST_USER).service"
    CMDLINE=''
    ;;
  *)
    echo "guest: GUEST_PROFILE must be ubuntu-v1-hybrid, ubuntu-v1-legacy, fedora-v2 or debian-v2" >&2
    exit 1
    ;;
esac

# scp spells the port -P and reads -p as "preserve timestamps", so the two need separate option strings.
# QEMU accepts the connection itself, so a guest that never reaches sshd leaves ssh waiting for a greeting. The
# timeout bounds every attempt of the wait loop below.
SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=10"
KEY=$WORK/id_ed25519

# shellcheck disable=SC2086
in_guest() { ssh -i "$KEY" $SSH_OPTS -p "$SSH_PORT" "$GUEST_USER@127.0.0.1" "$@"; }
# shellcheck disable=SC2086
copy_in() { scp -q -i "$KEY" $SSH_OPTS -P "$SSH_PORT" "$1" "$GUEST_USER@127.0.0.1:$2"; }

require_tools() {
  local missing=0

  command -v qemu-system-x86_64 >/dev/null || { echo "guest: qemu-system-x86 is required" >&2; missing=1; }
  command -v cloud-localds >/dev/null || { echo "guest: cloud-image-utils is required" >&2; missing=1; }
  command -v ssh >/dev/null || { echo "guest: openssh-client is required" >&2; missing=1; }
  # The guest gets this uv: the images ship a Python this package no longer supports.
  command -v uv >/dev/null || { echo "guest: uv is required, see https://docs.astral.sh/uv/" >&2; missing=1; }

  [ "$missing" -eq 0 ] || exit 1

  # Emulation needs no privileges but is several times slower, so it has to be asked for by name.
  if [ "${GUEST_ACCEL:-kvm}" = kvm ] && ! { [ -r /dev/kvm ] && [ -w /dev/kvm ]; }; then
    echo "guest: /dev/kvm is not usable. Run with GUEST_ACCEL=tcg to emulate instead, or grant access:" >&2
    echo "  on a runner: echo 'KERNEL==\"kvm\", GROUP=\"kvm\", MODE=\"0666\"' | sudo tee /etc/udev/rules.d/99-kvm4all.rules" >&2
    echo "  locally:     sudo usermod -aG kvm \$USER, then log in again" >&2
    exit 1
  fi
}

# Download once per machine, then hand back the cached path.
fetch() {
  local url=$1 dest="$DOWNLOADS/$(basename "$1")"

  if [ ! -s "$dest" ]; then
    echo "guest: downloading $(basename "$url")" >&2
    # These are hundreds of megabytes over a network this script does not control, and a run that fails here
    # says nothing about the sensor.
    curl -fsSL --retry 3 --retry-delay 5 --retry-connrefused -o "$dest" "$url"
  fi

  echo "$dest"
}

# Write the cloud-init seed that creates the user and installs a container engine.
write_seed() {
  [ -s "$KEY" ] || ssh-keygen -q -t ed25519 -N '' -f "$KEY"

  cat > "$WORK/user-data" <<EOF
#cloud-config
# Before the user, so the engine package adopts it and the first login already has it. Granting it afterwards
# would leave every session this script opens without it.
groups:
  - docker
users:
  - name: $GUEST_USER
    groups: docker
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    lock_passwd: true
    ssh_authorized_keys:
      - $(cat "$KEY.pub")
package_update: false
packages:
$(for package in $PACKAGES; do echo "  - $package"; done)
runcmd:
  # The systemd user manager does not start on its own for a user who is only ever reached over ssh.
  - loginctl enable-linger $GUEST_USER
  - systemctl enable --now docker
$([ -n "$PREPARE" ] && echo "  - $PREPARE")
EOF

  cloud-localds "$WORK/seed.img" "$WORK/user-data"
}

boot_guest() {
  local image=$1 kernel=$2 initrd=$3
  local boot=() cpu

  # The default QEMU processor does not advertise x86-64-v2, which the glibc of RHEL 9 and its rebuilds
  # refuses to start without. A guest has to be a realistic machine, or it would rule out those images.
  if [ "${GUEST_ACCEL:-kvm}" = kvm ]; then
    cpu=${GUEST_CPU:-host}
  else
    cpu=${GUEST_CPU:-max}
  fi

  # A throwaway overlay, so every run starts clean and the download stays untouched.
  rm -f "$WORK/run.qcow2"
  qemu-img create -q -f qcow2 -F qcow2 -b "$image" "$WORK/run.qcow2" 8G

  # A profile that has to set the kernel command line boots the kernel directly. The others use the image's
  # own bootloader.
  if [ -n "$kernel" ]; then
    boot=(-kernel "$kernel" -initrd "$initrd" -append "$CMDLINE")
    echo "guest: booting $PROFILE with '$CMDLINE'"
  else
    echo "guest: booting $PROFILE"
  fi

  # The network card is virtio, as on a cloud machine. QEMU's default is an emulated e1000, and the cloud
  # kernel of Debian has no driver for it, so that guest would come up without a network.
  qemu-system-x86_64 \
    -accel "${GUEST_ACCEL:-kvm}" -cpu "$cpu" -m "${GUEST_MEMORY:-4096}" -smp "${GUEST_CPUS:-2}" \
    -display none -serial file:"$WORK/console.log" \
    "${boot[@]}" \
    -drive file="$WORK/run.qcow2",if=virtio,format=qcow2 \
    -drive file="$WORK/seed.img",if=virtio,format=raw \
    -nic user,model=virtio-net-pci,hostfwd=tcp::"$SSH_PORT"-:22 \
    -pidfile "$WORK/qemu.pid" -daemonize
}

wait_for_guest() {
  echo -n 'guest: waiting for ssh'
  for _ in $(seq 1 180); do
    in_guest true 2>/dev/null && break
    echo -n .
    sleep 2
  done
  echo

  in_guest true 2>/dev/null || {
    echo 'guest: the guest never came up, the last of its console output:' >&2
    tail -30 "$WORK/console.log" >&2
    exit 1
  }

  # One mount per v1 controller, so counting both kinds says which layout the guest came up in: only cgroup
  # mounts is legacy, both kinds is hybrid, only cgroup2 is the unified hierarchy.
  echo -n 'guest: up, '
  in_guest 'printf "%s, cgroup mounts: %s v1, %s v2\n" \
    "$(. /etc/os-release && echo "$PRETTY_NAME")" \
    "$(grep -c " - cgroup " /proc/self/mountinfo || true)" "$(grep -c " - cgroup2 " /proc/self/mountinfo || true)"'

  # ssh answers as soon as sshd is up, while cloud-init is still installing packages behind it.
  echo 'guest: waiting for cloud-init to finish'
  in_guest 'cloud-init status --wait >/dev/null 2>&1 || true'
}

install_suite() {
  echo 'guest: copying the repository in'
  in_guest 'rm -rf sensor && mkdir -p sensor'
  tar -C "$REPO_ROOT" --exclude=.git --exclude=.venv --exclude=analysis --exclude=__pycache__ \
    -cf - src tests pyproject.toml uv.lock README.md LICENSE | in_guest 'tar -C sensor -xf -'

  copy_in "$(command -v uv)" uv
  in_guest 'sudo install -m 0755 uv /usr/local/bin/uv'
}

# Run the suite inside and hand back its exit code.
run_suite() {
  # The arguments travel through ssh as one string, so each has to carry its own quoting. Without it an
  # argument with spaces, such as `-k 'a or b'`, arrives as several.
  local args=''
  [ "$#" -eq 0 ] || args=" ${*@Q}"

  # The profile's exclusions travel with the command, and everything they leave has to pass inside the guest.
  local suite="cd sensor && uv run poe install-dev"
  suite="$suite && E2E_INTERFACE='$INTERFACE' uv run pytest tests/e2e -m ${EXCLUDE@Q}$args"
  local status=0

  echo 'guest: running the e2e suite inside'
  # With the groups of the login, not `sg docker`: that makes docker the primary group, and `newuidmap` then
  # refuses to map a user whose group is not their own - taking rootless podman down with it.
  in_guest "$suite" || status=$?

  return "$status"
}

shutdown_guest() {
  [ -s "$WORK/qemu.pid" ] && kill "$(cat "$WORK/qemu.pid")" 2>/dev/null
  rm -f "$WORK/qemu.pid"
}

require_tools
mkdir -p "$WORK" "$DOWNLOADS"

image=$(fetch "$IMAGE_URL")
kernel=''
initrd=''
if [ -n "$KERNEL_URL" ]; then
  kernel=$(fetch "$KERNEL_URL")
  initrd=$(fetch "$INITRD_URL")
fi

write_seed
boot_guest "$image" "$kernel" "$initrd"
trap shutdown_guest EXIT

wait_for_guest
install_suite

status=0
run_suite "$@" || status=$?
in_guest 'sudo poweroff' 2>/dev/null || true

exit "$status"
