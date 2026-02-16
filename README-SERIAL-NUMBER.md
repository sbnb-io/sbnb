# Assigning Hostnames Automatically in Sbnb Linux

During the boot process, Sbnb Linux reads the MAC address of the first physical network interface and assigns the hostname as:

```
sbnb-${MAC}
```

The MAC address is used without colons (e.g. `sbnb-345a6078df18`). Wired interfaces (eth*, en*) are preferred over wireless (wl*). If no physical network interface is found, random bytes are used as fallback.

Once the machine boots and connects to [Tailscale](https://tailscale.com/) (tailnet), it will be identified using the assigned hostname.

## Example

### MAC address on the device
MAC addresses are typically printed on the device case or available in the device documentation (e.g. BMC WebUI).

### Machine registered in Tailscale (tailnet)
![Sbnb Linux: Machine registered in Tailscale (tailnet)](images/serial-number-tailscale.png)
