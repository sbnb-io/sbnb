🚀 Stay ahead - [subscribe](https://sbnb.io/) to our newsletter!

# AI Linux (Sbnb Linux)

Sbnb Linux is a revolutionary minimalist Linux distribution designed to boot bare-metal servers and enable remote connections through fast tunnels. It is ideal for environments ranging from home labs to distributed data centers. Sbnb Linux is simplified, automated, and resilient to power outages, supporting confidential computing to ensure secure operations in untrusted locations.

# Prerequisites

- **Bare Metal Server:** Any x86 machine should suffice.
- A **USB flash drive** (if using local boot), or an **iPXE server** (for network boot).
- **[Optional]** If you plan to launch Confidential Computing (CC) Virtual Machines (VMs) on Sbnb Linux, ensure that your CPU supports AMD SEV-SNP technology (available from AMD EPYC Gen 3 CPUs onward). Additionally, enable this feature in the BIOS. For more details, refer to [README-CC.md](https://github.com/sbnb-io/sbnb/blob/main/README-CC.md).

# Typical Use Case for Sbnb Linux

## Use Case 1: Run Sbnb Linux on Bare Metal Server

The diagram below shows how Sbnb Linux boots a bare metal server (host), starts a guest virtual machine, and attaches an Nvidia GPU to the guest using the low-overhead `vfio-pci` mechanism. Read more at this [README-NVIDIA.md](README-NVIDIA.md).

![nvidia-vfio-sbnb-linux](images/nvidia-vfio-sbnb-linux.png)

In summary, the bare metal server boots into a minimal Linux environment consisting of a Linux kernel with Tailscale, Docker container engine, and QEMU KVM hypervisor.

## Use Case 2: Run vLLM or SGLang AI on Nvidia GPU

Quickly set up a Bare Metal server with GPU monitoring using Sbnb Linux and **Infrastructure as Code (IaC)**, all visualized through **Grafana**.

The graphs below shows GPU load during a vLLM benchmark test for a few minutes, leading to a GPU load spike to 100%. Memory allocation is at 90% per vLLM config.

![Sbnb Linux: vLLM - Monitoring GPU Load, Memory, Temp, FAN speed, Power consumption (Watt) with Grafana](images/vllm-benchmark-per-gpu.png)

![Sbnb Linux: vLLM - Monitoring GPU Load, Memory, Temp, FAN speed, Power consumption (Watt) with Grafana](images/vllm-benchmark-all-gpu.png)


## Use Case 3: Run Sbnb Linux as a VM Guest

Please refer to the separate document on how to run Sbnb Linux as a VMware guest: [README-VMWARE.md](https://github.com/sbnb-io/sbnb/blob/main/README-VMWARE.md).

However, VMware is not a hard requirement. Any VM hypervisor, such as QEMU, can also be used.

# Run AI Workloads Seamlessly with Sbnb Linux

Explore step-by-step guides to deploy popular AI tools on bare metal using Sbnb Linux in Automated Way:

- 🚀 **vLLM Setup Guide** – [README-VLLM.md](README-VLLM.md)  
- 💬 **SGLang Setup Guide** – [README-SGLANG.md](README-SGLANG.md)  
- 🧠 **Run Qwen2.5-VL in vLLM and SGLang** – [README-QWEN2.5-VL.md](README-QWEN2.5-VL.md)  
- ⚡ **Deploy LightRAG in Minutes** – [README-LightRAG.md](README-LightRAG.md)  
- 📚 **Deploy RAGFlow in Minutes** – [README-RAG.md](README-RAG.md)  
- 🕵️‍♂️ **Launch Browser Use AI Agent** – [README-AI-AGENT.md](README-AI-AGENT.md)

# Key Features of Sbnb Linux:

- **Minimalist OS** – Bare metal servers boot into sbnb Linux, a lightweight OS combining a Linux kernel with Docker. The package list is minimal to reduce image size and limit attack vectors from vulnerabilities.
- **Runs in Memory** – sbnb Linux doesn’t install on system disks but runs in memory, similar to liveCDs. A simple power cycle restores the server to its original state, enhancing resilience.
- **Configuration on Boot** – sbnb Linux reads config file from a USB dongle during boot to customize the environment.
- **Immutable Design** – Sbnb Linux is an immutable, read-only Unified Kernel Image (UKI), enabling straightforward image signing and attestation. This design makes the system resistant to corruption or tampering ("unbreakable").
- **Remote Access** – A Tailscale tunnel is established during boot, allowing remote access. The Tailscale key is specified in a config file.
- **Confidential Computing** – The sbnb Linux kernel supports Confidential Computing (CC) with the latest CPU and Secure Processor microcode updates applied at boot. Currently, only AMD SEV-SNP is supported.
- **Flexible Environment** – sbnb Linux includes scripts to start Docker containers, allowing users to switch from the minimal environment to distributions like Debian, Ubuntu, CentOS, Alpine, and more.
- **Developer Mode** – Activate developer mode by running the `sbnb-dev-env.sh` script, which launches anDebian/Ubuntu container with various developer tools pre-installed.
- **Reliable A/B Updates** – If a new version fails, a hardware watchdog automatically reboots the server into the previous working version. This is crucial for remote locations with limited or no physical access.
- **Regular Update Cadence** – Sbnb Linux follows a predictable update schedule. Updates are treated as routine operations rather than disruptive events, ensuring the system stays protected against newly discovered vulnerabilities.
- **Firmware Updates** – Sbnb Linux applies the latest CPU and Security Processor microcode updates at every boot. BIOS updates can also be applied during the update process, keeping the entire system up to date.
- **Built with Buildroot** – sbnb Linux is created using Buildroot with the br2-external mechanism, keeping sbnb customizations separate for easier maintenance and rolling updates.

# How to Boot Your Server into Sbnb Linux

## Option 1: Booting from a USB Flash Drive

This method is ideal for home labs and small server fleets. It allows you to quickly boot bare metal servers within minutes. Detailed steps for this option are provided below.

## Option 2: Booting via Network (iPXE)

Best suited for large, automated server fleets such as data centers. This method enables you to automatically boot a large number of bare metal servers over the network.

# Booting from a USB Flash Drive
Below is a brief guide. For a more detailed installation guide, refer to [README-INSTALL.md](README-INSTALL.md).

## 1. Prepare a Bootable USB Dongle with Sbnb Linux

Attach a USB flash drive to your computer and run the appropriate command below in the terminal:

- **For Windows** (execute in PowerShell as Administrator):
  ```powershell
  iex ((New-Object System.Net.WebClient).DownloadString('https://raw.githubusercontent.com/sbnb-io/sbnb/refs/heads/main/scripts/install-win.ps1'))
  ```

- **For Mac**:
  ```bash
  bash <(curl -s https://raw.githubusercontent.com/sbnb-io/sbnb/refs/heads/main/scripts/install-mac.sh)
  ```

- **For Linux**:
  ```bash
  sh <(curl -s https://raw.githubusercontent.com/sbnb-io/sbnb/refs/heads/main/scripts/install-linux.sh)
  ```

The script will:
- Download the latest Sbnb Linux image.
- Flash it onto the selected USB drive.
- Prompt you to enter your Tailscale key.
- Allow you to specify custom commands to execute during the Sbnb Linux instance boot.

---

## 2. Boot the Server

- Attach the prepared USB dongle to the server you want to boot into Sbnb Linux.
- Power on the server.

---

## 3. Notes on Booting the Server

- [Optional] Ensure the USB flash drive is selected as the **first boot device** in your BIOS/UEFI settings. This may be necessary if another operating system is installed or if network boot is enabled.
- The boot process may take **5 to 10 minutes**, depending on your server's BIOS configuration.

---

## 4. Verify the Server on Tailscale

After booting, verify that the server appears in your **Tailscale machine list**.

---

## 5. Done!

You can now SSH into the server using Tailscale SSO methods, such as **Google Auth**.

---

## 6. Next Steps

For development and testing, run the following command after SSH-ing into the server:

```bash
sbnb-dev-env.sh
```
This will transition your environment from the minimalist setup to a full Docker container running Debian/Ubuntu, preloaded with useful development tools.

# Running the "Hello World" Example on Sbnb Linux

After connecting to Sbnb Linux via SSH, you can easily run an Ubuntu container that prints "Hello, World!" by executing the following command:

```
docker run ubuntu echo "Hello, World!"
```
You can replace `ubuntu` with `centos`, `alpine`, or any other distribution of your choice.

If successful, you should see output similar to the image below:

 
![Sbnb Hello World Example](images/sbnb-hello-world.png)


Congratulations! Your Sbnb Linux environment is now up and running. We're excited to see what you'll create next!

# Sbnb Linux Instance Customization

## Low-Level Customization
The `sbnb-cmds.sh` file introduces a powerful way to customize Sbnb Linux instances during boot. By placing a custom shell script named `sbnb-cmds.sh` on a USB flash drive or another supported configuration source, you can define commands and behaviors to be executed under the BusyBox shell during the boot process.

This feature is ideal for low-level system configurations like devices, networking, etc.

For more details, refer to [README-CUSTOMIZATION.md](README-CUSTOMIZATION.md).

## High-Level Customization or Workloads
To start workloads, it's recommended to use an Infrastructure as Code (IaC) approach using Ansible.  
Please refer to this tutorial where we will start a Docker container on a bare-metal server booted into Sbnb Linux using the Ansible automation tool: [README-ANSIBLE.md](README-ANSIBLE.md).

Additionally, check out this detailed tutorial on how disks and networking are configured in Sbnb Linux [README-CONFIGURE_SYSTEM.md](README-CONFIGURE_SYSTEM.md). Disks are combined into an LVM volume, and the network is attached to a br0 bridge for fast and simple VM plumbing.

Days when system administrators manually installed Linux OS and configured services are gone.


# Exploring Different Strategies for Launching Customer Workloads on Sbnb Linux  

Sbnb Linux provides several options for starting customer jobs, depending on the environment and security requirements.  

| Option | Description | Recommended Use | Example Link |  
|--------|-------------|-----------------|--------------|  
| **Run Directly on Minimalist Environment** | Execute jobs directly on the lightweight Sbnb Linux environment. Suitable for system services like observability or monitoring. | Not recommended for regular jobs. Use for system services. | [Example: Tailscale Tunnel Startup](/board/sbnb/sbnb/rootfs-overlay/usr/lib/systemd/system/tailscaled.service) |
| **Docker Container** | Launch Docker containers (Ubuntu, Fedora, Alpine, etc.) on top of the minimalist environment. This approach powers the `sbnb-dev-env.sh` script to create a full development environment. | Recommended for trusted environments (e.g., home labs). | [Example: Development Environment](/board/sbnb/sbnb/rootfs-overlay/usr/sbin/sbnb-dev-env.sh) |
| **Run Regular Virtual Machine (VM)** | Start a standard VM to run full-featured OS like Windows or other Linux distributions. | Recommended for trusted environments (e.g., home labs). | [Detailed Documentation](/README-VM.md) |
| **Confidential Computing Virtual Machine (CC VM)** | Start a CC VM to run production workloads securely. Encrypts memory and CPU states, enabling remote attestation to ensure code integrity. | Recommended for production environments. | [Detailed Documentation](/README-CC.md) |

# Build sbnb Image Yourself
To build the Sbnb Linux image, it is recommended to use Ubuntu 24.04 as the development environment.

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

## Hardware

### AI computer ("GPUter")

<p align="left">
  <img src="images/ai-pc.jpg" alt="AI Computer ("GPUter")" width="640">
  <br>
  <em>This is our actual AI computer ("GPUter") build - and yes, it runs almost silently!</em>
</p>

All components listed below are brand new and available in the U.S. market as of 2025. This is our go-to configuration for affordable (\$1,040) yet powerful local AI compute. Hardware configurations may vary; the sky is the limit.

| Component               | Price (USD) | Link                                               |
| ----------------------- | ----------- | -------------------------------------------------- |
| NVIDIA RTX 5060 Ti 16GB Blackwell (759 AI TOPS) | 429         | [Newegg](https://www.newegg.com/p/N82E16814932791) |
| MotherBoard (B550M)     | 99         | [Amazon](https://www.amazon.com/dp/B0BDCZRBD6)     |
| AMD Ryzen 5 5500 CPU    | 60          | [Amazon](https://www.amazon.com/dp/B09VCJ171S)     |
| 32GB DDR4 RAM (2x16GB)  | 52          | [Amazon](https://www.amazon.com/dp/B07RW6Z692)     |
| M.2 SSD 4TB             | 249         | [Amazon](https://www.amazon.com/dp/B0DHLBDSP7)     |
| Case (JONSBO/JONSPLUS Z20 mATX)    | 109         | [Amazon](https://www.amazon.com/dp/B0D1YKXXJD)     |
| PSU 600W                | 42          | [Amazon](https://www.amazon.com/dp/B014W3EMAO)     |
| **Total**               | **1040**    |                                                    |

To see what this configuration can achieve, check out [this submission](https://www.kaggle.com/competitions/google-gemma-3n-hackathon/writeups/sixth-sense-for-security-guards-powered-by-googles) to Google’s Gemma 3n Hackathon, which processes live feeds from 16 security cameras and provides video understanding.

# Architecture and Technical Details

Sbnb Linux is built from source using the Buildroot project. It leverages the [Buildroot br2-external mechanism](https://buildroot.org/downloads/manual/manual.html#outside-br-custom) to keep Sbnb-specific customizations separate, simplifying maintenance and enabling smooth rolling updates.

## Boot Image
The Linux kernel is compiled and packaged with the command line and initramfs into a single binary called the Unified Kernel Image (UKI). The UKI is a PE/COFF binary, allowing it to be booted by any UEFI BIOS. This makes Sbnb Linux compatible with any modern machine. The total size of the image is approximately 200MB.

## Initramfs Components:
- BusyBox: Provides a shell and other essential tools.
- Systemd: Serves as the init system.
- Tailscale: Pre-installed to establish secure tunnels.
- Docker Engine: Installed to enable running any container.

This minimal setup is sufficient to boot the system and make the bare metal accessible remotely. From there, users can deploy more advanced software stacks using Docker containers or Virtual Machines, including Confidential Computing VMs.

See the diagram below for the internal structure of sbnb Linux.

![Sbnb Architecture](images/sbnb-architecture.png)

## Assigning Hostnames Automatically in Sbnb Linux
During the boot process, Sbnb Linux reads the machine's serial number and assigns the hostname as:

```
sbnb-${SERIAL}
```

If no serial number can be read, then a randomly generated string is used instead.
Once the machine boots and connects to [Tailscale](https://tailscale.com/) (tailnet), it will be identified using the assigned hostname.

![Sbnb Linux: Machine registered in Tailscale (tailnet)](images/serial-number-tailscale.png)

Read more at [README-SERIAL-NUMBER.md](README-SERIAL-NUMBER.md)

## Use Cases

The diagram below illustrates the concept of Sbnb Linux, where servers connect to the public Internet through ISP links and NAT. These servers create an overlay network across the public Internet using secure tunnels, powered by Tailscale, resulting in a flat, addressable space.

![Sbnb Network Diagram](images/sbnb-network-diagram.png)
 

The next diagram illustrates how a Virtual Machine (VM) owner can verify and establish trust in a VM running on an Sbnb server located in an untrusted physical environment. This is achieved by leveraging AMD SEV-SNP’s remote attestation mechanism. This approach enables the creation of distributed data centers with servers deployed in diverse, untrusted locations such as residences, warehouses, mining farms, shipping containers, colocation facilities, or remote sites near renewable energy sources.

![Sbnb Confidential Computing (CC) Network Diagram](images/sbnb-cc-network.png)

# Alternative Linux Distributions with Similar Concepts to Sbnb Linux

If you're interested in exploring the fascinating world of immutable, container-optimized Linux distributions, here are some notable projects worth checking out:

- [Fedora CoreOS](https://fedoraproject.org/coreos/)
- [Bottlerocket OS](https://github.com/bottlerocket-os/bottlerocket)
- [Flatcar Container Linux](https://www.flatcar.org/) (acquired by Microsoft)
- [RancherOS](https://rancher.com/docs/os/v1.x/en/)
- [Talos Linux](https://www.talos.dev/)

# Frequently Asked Questions
## What benefits does it offer compared to using Cloud-init on any distro?
While it's true that almost any distribution can be minimized, configured to run in-memory, and integrated with Cloud-init or Kickstart, this approach focuses on building a system from the ground up. This avoids the need to strip down a larger, more complex system, eliminating compromises and workarounds typically required in such cases.

## Will power cycling wipe out the Docker containers you've installed?
Yes, power cycling will restore the system to a known good baseline state. Sbnb Linux is designed this way to ensure reliability and stability. After a power cycle, automation tools can be used to pull and run the containers again on the node. This design makes Sbnb Linux highly resilient and virtually unbreakable.

🚀 Stay ahead - [subscribe](https://sbnb.io/) to our newsletter!
