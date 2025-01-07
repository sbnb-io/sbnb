# Confidential Computing Virtual Machine on Sbnb Linux

## Overview

This guide explains how to start a Virtual Machine (VM) in Confidential Computing (CC) mode using AMD SEV-SNP technology, available from AMD Epyc Gen 3 CPUs onwards.

Confidential Computing encrypts the memory and CPU states of the virtual machine, transforming it into a secure "black box". This setup allows workloads with sensitive data to operate securely, even in shared or untrusted environments.

## Prerequisites

- **Bare Metal Server** with **AMD SEV-SNP** support enabled in BIOS.
    - For example, take a look at this [Reddit post](https://www.reddit.com/r/homelab/comments/1hmnnwg/built_a_powerful_and_silent_amd_epyc_home_server/) where we built a powerful and quiet AMD EPYC 3rd Gen home server together with my kids.
- **Sbnb Linux Boot**:
    - Refer to the main **Sbnb Linux** [README](https://github.com/sbnb-io/sbnb/blob/main/README.md) for instructions on how to boot your server into Sbnb Linux.

## Step-by-step Guide

### 1. SSH into Bare Metal Server

Ensure your bare metal server is booted into Sbnb Linux and SSH into it using Tailscale.

```
ssh user@your-server-tailscale-name-or-ip
```

### 2. Confirm AMD SEV-SNP is Enabled

Run the following command to check SEV-SNP status:

```
sevctl ok
```

If SEV-SNP is enabled, the command will return an OK messages.


Errors indicate missing BIOS configurations or unsupported CPUs.

### 3. Run Docker Container with Required Tools

Launch a pre-built Docker container with SVSM, OVMF, and QEMU that supports AMD SEV-SNP:

```
docker run -it --privileged -v /dev/kvm:/dev/kvm -v /dev/sev:/dev/sev sbnb/svsm bash
```
Note: For those interested, here is a link to the [Dockerfile](https://github.com/sbnb-io/sbnb/blob/main/containers/svsm/Dockerfile) for exploration.
It is based on the official SVSM documentation. We created it to simplify the process of building Confidential Computing (CC) software.

### 4. Download Sbnb Linux Image

Download the Sbnb Linux `sbnb.raw` image from the official release page:

https://github.com/sbnb-io/sbnb/releases

### 5. Inject Tailscale Key into Image

Prepare the image by injecting your actual Tailscale key:

```
mkdir sbnb
mount -o offset=$((2048*512)) sbnb.raw sbnb
echo tskey-auth-YOUR-KEY > sbnb/sbnb-tskey.txt
umount sbnb
```

### 6. Start Confidential Computing VM

Set environment variables and start the VM:

```
export IGVM=/usr/qemu-svsm/coconut-qemu.igvm
export BOOT_IMAGE=sbnb.raw
export VCPU=16
export MEM=32G

/usr/qemu-svsm/bin/qemu-system-x86_64 \
  -enable-kvm \
  -cpu EPYC-Milan-v2 \
  -machine q35,confidential-guest-support=sev0,memory-backend=ram1,igvm-cfg=igvm0 \
  -object memory-backend-memfd,id=ram1,size=${MEM},share=true,prealloc=false,reserve=false \
  -object sev-snp-guest,id=sev0,cbitpos=51,reduced-phys-bits=1 \
  -object igvm-cfg,id=igvm0,file=$IGVM \
  -smp ${VCPU} \
  -no-reboot \
  -netdev user,id=vmnic -device e1000,netdev=vmnic,romfile= \
  -drive file=${BOOT_IMAGE},if=none,id=disk0,format=raw,snapshot=off \
  -device virtio-scsi-pci,id=scsi0,disable-legacy=on,iommu_platform=on \
  -device scsi-hd,drive=disk0,bootindex=0 \
  -nographic
```

### 7. Remote Attestation of VM

Once the VM is up, perform remote attestation to verify its integrity. Use the following command:

```
docker run -it sbnb/remote-attestation remote-attestation.sh root@<vm-ip>
```
Note: If you're interested, check out this [link](https://github.com/sbnb-io/sbnb/tree/main/containers/remote-attestation) to the container referenced in the command above.
It simplifies the remote attestation process for Confidential Computing (CC) VMs.

Example output for successful attestation:

```
# docker run -it sbnb/remote-attestation remote-attestation.sh root@100.83.253.96
Warning: Permanently added '100.83.253.96' (ED25519) to the list of known hosts.
attestation-report.bin                                                                                                      100% 1184   191.4KB/s   00:00    
The AMD ARK was self-signed!
The AMD ASK was signed by the AMD ARK!
The VCEK was signed by the AMD ASK!
Reported TCB Boot Loader from certificate matches the attestation report.
Reported TCB TEE from certificate matches the attestation report.
Reported TCB SNP from certificate matches the attestation report.
Reported TCB Microcode from certificate matches the attestation report.
VEK signed the Attestation Report!
    _  _____ _____ _____ ____ _____ _____ ____  
   / \|_   _|_   _| ____/ ___|_   _| ____|  _ \ 
  / _ \ | |   | | |  _| \___ \ | | |  _| | | | |
 / ___ \| |   | | | |___ ___) || | | |___| |_| |
/_/   \_\_|   |_| |_____|____/ |_| |_____|____/ 
                                                
The remote Confidential Computing VM has successfully passed AMD SEV-SNP and vTPM attestation.
#
```

Warning! If the attestation fails or shows different output, **do not proceed** with using the VM, as it may have been tampered with.

---

### Congratulations!
You now have a secure, remotely attested Confidential Computing VM ready for sensitive workloads.
