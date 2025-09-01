# Install Sbnb Linux

## Prerequisites
- **Bare Metal Server**: Any x86 machine should suffice.
- **USB Flash Drive for Booting**: Any standard USB flash drive with at least 512MB capacity will work.
- **Tailscale Key**: Register at [Tailscale](https://tailscale.com/) to obtain your key. The free plan supports up to 100 devices.

## Prepare a Bootable USB Dongle with Sbnb Linux
### Attach a USB flash drive to your computer.

![Sbnb Linux: USB flash drive attached to laptop](images/usb-flash-laptop.png)


### Run the appropriate command below in the terminal:
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

### The script will:
- Download the latest Sbnb Linux image.
- Flash it onto the selected USB drive.
- Prompt you to enter your Tailscale key.
- Allow you to specify custom commands to execute during the Sbnb Linux instance boot.

Here is a screenshot of the script demo running on a Mac:

![Sbnb Linux: Prepare a Bootable USB Dongle under Mac](images/sbnb-install-mac.png)

## Boot the Server
- Attach the prepared USB dongle to the server you want to boot into Sbnb Linux.
- Power on the server.
  
![Sbnb Linux: Serial number on the top of the chassis case](images/serial-number-chassis.png)


## Notes on Booting the Server
- [Optional] Ensure the USB flash drive is selected as the **first boot device** in your BIOS/UEFI settings. This may be necessary if another operating system is installed or if network boot is enabled.
- The boot process may take **5 to 10 minutes**, depending on your server's BIOS configuration.

## Verify the Server on Tailscale
After booting, verify that the server appears in your **Tailscale machine list**.

![Sbnb Linux: Machine registered in Tailscale (tailnet)](images/serial-number-tailscale.png)

For more details on automatic hostname assignments, refer to [README-SERIAL-NUMBER.md](README-SERIAL-NUMBER.md).

### Note:
When the motherboard serial number reported by dmidecode is a placeholder such as `To be filled by O.E.M.`, `Not Specified`, or `Default string`, the system will use the MAC address of the first physical network interface to generate the hostname (e.g. `sbnb-345a6078df18`). If no physical interface is found, it will fall back to a random hostname.

## Done!
You can now SSH into the server using Tailscale SSO methods, such as **Google Auth**.
If you're having trouble connecting via SSH, check out this GitHub issue for detailed debugging steps:
[https://github.com/sbnb-io/sbnb/issues/8](https://github.com/sbnb-io/sbnb/issues/8)

## Next Steps
For development and testing, run the following command after SSH-ing into the server:

```bash
sbnb-dev-env.sh
```
This will transition your environment from the minimalist setup to a full Docker container running Debian/Ubuntu, preloaded with useful development tools.
