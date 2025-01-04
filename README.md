## Sbnb Linux

Sbnb Linux is a revolutionary minimalist Linux distribution designed to boot bare-metal servers and enable remote connections through fast tunnels. It is ideal for environments ranging from home labs to distributed data centers. Sbnb Linux is simplified, automated, and resilient to power outages, supporting confidential computing to ensure secure operations in untrusted locations.

## How to Boot Your Server into Sbnb Linux

1. **Download the `sbnb.raw` image**
    - Head to the **Release** section of this repository and download the `sbnb.raw` image (182 MB).
2. **Write the Image to a USB Flash Drive**
    - The following steps are for macOS, but they can be adapted for Linux or Windows.
    - **Identify the USB flash drive:**
        
        Run the command:
        
        ```bash
        diskutil list
        ```
        
        Look for an entry like `/dev/disk4 (external, physical)`.
        
        ⚠️ **Warning:** This process will erase all data on the USB drive. Verify the disk size carefully to avoid overwriting the wrong device.
        
    - **Write the `sbnb.raw` image to the USB drive:**
        
        ```bash
        sudo diskutil unmountDisk /dev/disk4
        dd if=sbnb.raw of=/dev/disk4
        ```
        
        Replace `/dev/disk4` with the correct disk number from the previous step.
        
3. **Add Your Tailscale Key to the USB Drive**
    - Create a mount point and mount the USB drive:
        
        ```bash
        mkdir sbnb
        sudo mount -t msdos /dev/disk4s1 sbnb
        ```
        
    - Add your Tailscale key to the drive:Replace **`tskey-auth-KEY`** with your actual Tailscale key. For instructions on generating a key, visit [tailscale.com](https://tailscale.com/).
        
        ```bash
        echo "tskey-auth-KEY" > sbnb/sbnb-tskey.txt
        ```
        
    - Unmount the USB drive:
        
        ```bash
        sudo diskutil unmountDisk /dev/disk4
        ```
        
4. **Boot the Server**
    - Insert the USB drive into your server and power it on.
    - You may need to select the USB flash drive as the first boot device in your BIOS/UEFI settings if another operating system is installed or if network boot is enabled.
    - Booting may take **5 to 10 minutes**, depending on your server’s BIOS configuration.
5. **Verify the Server on Tailscale**
    - After booting, check your server in the **Tailscale machine list**.
6. **Done!**
    - You can now SSH into the server using Tailscale SSO methods, such as Google Auth.
7. **Next Steps**
    - For development and testing, we recommend running the following command after SSH-ing into the server
    ```bash
    sbnb-dev-env.sh
    ```
    This will transition your environment from the minimalist setup to a full Docker container running Debian/Ubuntu, preloaded with useful development tools.

## Running the "Hello World" Example on Sbnb Linux

After connecting to Sbnb Linux via SSH, you can easily run an Ubuntu container that prints "Hello, World!" by executing the following command:

```
docker run ubuntu echo "Hello, World!"
```

Congratulations! Your Sbnb Linux environment is now up and running. We're excited to see what you'll create next!

## Build sbnb Image Yourself

- Clone this repository.
```
git clone https://github.com/sbnb-io/sbnb.git
cd sbnb
git submodule init
git submodule update
```
- Start the build process.
```
cd buildroot
make BR2_EXTERNAL=.. sbnb_defconfig
make -j $(nproc)
```
- After a successful build, the following files will be generated:
    - **`output/images/sbnb.efi`** – A UEFI bootable Sbnb image in Unified Kernel Image (UKI) format. This file integrates the Linux kernel, kernel arguments (cmdline), and initramfs into a single image.
    - **`output/images/sbnb.raw`** – A disk image ready to be written directly to a USB flash drive for server booting. It features a GPT partition table and a bootable VFAT partition containing the UEFI bootable image (`sbnb.efi`).

Happy developing! Contributions are encouraged and appreciated!

## How to Start Customer Workloads on Sbnb Linux  

Sbnb Linux provides several options for starting customer jobs, depending on the environment and security requirements.  

| Option | Description | Recommended Use | Example Link |  
|--------|-------------|-----------------|--------------|  
| **Run Directly on Minimalist Environment** | Execute jobs directly on the lightweight Sbnb Linux environment. Suitable for system services like observability or monitoring. | Not recommended for regular jobs. Use for system services. | [Example: Tailscale Tunnel Startup](/board/sbnb/sbnb/rootfs-overlay/usr/lib/systemd/system/tailscaled.service) |
| **Docker Container** | Launch Docker containers (Ubuntu, Fedora, Alpine, etc.) on top of the minimalist environment. This approach powers the `sbnb-dev-env.sh` script to create a full development environment. | Recommended for trusted environments (e.g., home labs). | [Example: Development Environment](/board/sbnb/sbnb/rootfs-overlay/usr/sbin/sbnb-dev-env.sh) |
| **Run Regular Virtual Machine (VM)** | Start a standard VM to run full-featured OS like Windows or other Linux distributions. | Recommended for trusted environments (e.g., home labs). | [Detailed Documentation](/README-VM.md) |
| **Confidential Computing Virtual Machine (CC VM)** | Start a CC VM to run production workloads securely. Encrypts memory and CPU states, enabling remote attestation to ensure code integrity. | Recommended for production environments. | [Detailed Documentation](/README-CC.md) |


## Architecture and Technical Details

### Key Points:

- **Minimalist OS** – Bare metal servers boot into sbnb Linux, a lightweight OS combining a Linux kernel with Docker. The package list is minimal to reduce image size and limit attack vectors from vulnerabilities.
- **Built with Buildroot** – sbnb Linux is created using Buildroot with the br2-external mechanism, keeping sbnb customizations separate for easier maintenance and rolling updates.
- **Configuration on Boot** – sbnb Linux reads the `sbnb.conf` file from a USB dongle during boot to customize the environment.
- **Runs in Memory** – sbnb Linux doesn’t install on system disks but runs in memory, similar to liveCDs. A simple power cycle restores the server to its original state, enhancing resilience.
- **Immutable Design** – Sbnb Linux is an immutable, read-only Unified Kernel Image (UKI), enabling straightforward image signing and attestation. This design makes the system resistant to corruption or tampering ("unbreakable").
- **Remote Access** – A Tailscale tunnel is established during boot, allowing remote access. The Tailscale key is specified in the `sbnb.conf` file.
- **Confidential Computing** – The sbnb Linux kernel supports Confidential Computing (CC) with the latest CPU and Secure Processor microcode updates applied at boot. Currently, only AMD SEV-SNP is supported.
- **Flexible Environment** – sbnb Linux includes scripts to start Docker containers, allowing users to switch from the minimal environment to distributions like Debian, Ubuntu, CentOS, Alpine, and more.
- **Developer Mode** – Activate developer mode by running the `sbnb-dev-env.sh` script, which launches anDebian/Ubuntu container with various developer tools pre-installed.
- **Reliable A/B Updates** – If a new version fails, a hardware watchdog automatically reboots the server into the previous working version. This is crucial for remote locations with limited or no physical access.
- **Regular Update Cadence** – Sbnb Linux follows a predictable update schedule. Updates are treated as routine operations rather than disruptive events, ensuring the system stays protected against newly discovered vulnerabilities.
- **Firmware Updates** – Sbnb Linux applies the latest CPU and Security Processor microcode updates at every boot. BIOS updates can also be applied during the update process, keeping the entire system up to date.

### Diagrams:

- **Internal Architecture** – See the diagram below for the internal structure of sbnb Linux.
![Sbnb Architecture](images/sbnb-architecture.png)
 

- **Network Diagram** – This diagram depicts the network configuration, showing Sbnb Linux servers connecting to the public Internet via ISP links and NAT. The servers form an overlay network across the public Internet using secure tunnels powered by Tailscale.
![Sbnb Network Diagram](images/sbnb-network-diagram.png)
 

- **Confidential Computing (CC) Network Diagram** – The diagram below demonstrates how a Virtual Machine (VM) owner can verify and trust a VM running on an Sbnb server in an untrusted location by leveraging AMD SEV-SNP’s remote attestation mechanism.
![Sbnb Confidential Computing (CC) Network Diagram](images/sbnb-cc-network.png)
