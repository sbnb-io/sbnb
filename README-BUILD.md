# Build Sbnb Linux Image

## Prerequisites

Ubuntu 24.04 is the recommended build environment.

Install the required packages:

```bash
sudo apt-get install -y build-essential sed make binutils diffutils gcc g++ \
  bash patch gzip bzip2 perl tar cpio unzip rsync file bc findutils gawk \
  wget python3 git libncurses-dev libssl-dev systemd-boot-efi qemu-utils zip
```

## Build

Clone the repository and initialize submodules:

```bash
git clone https://github.com/sbnb-io/sbnb.git
cd sbnb
git submodule init
git submodule update
```

Start the build:

```bash
cd buildroot
make BR2_EXTERNAL=.. sbnb_defconfig
make -j $(nproc)
```

## Build Output

After a successful build, the following files are generated in `output/images/`:

| File | Description |
|------|-------------|
| `sbnb.efi` | UEFI bootable Sbnb image in Unified Kernel Image (UKI) format. Integrates the Linux kernel, kernel arguments (cmdline), and initramfs into a single image. |
| `sbnb.raw` | Disk image ready to be written directly to a USB flash drive. Features a GPT partition table and a bootable VFAT partition containing `sbnb.efi`. |
