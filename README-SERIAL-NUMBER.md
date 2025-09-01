# Assigning Hostnames Automatically in Sbnb Linux

During the boot process, Sbnb Linux reads the machine's serial number and assigns the hostname as:

```
sbnb-${SERIAL}
```

If no serial number can be read, then a randomly generated string is used instead.

Once the machine boots and connects to [Tailscale](https://tailscale.com/) (tailnet), it will be identified using the assigned hostname.

## Example
Below are pictures from a test 1U rack-mount machine based on an ASRock motherboard:

### Serial number on the top of the chassis case
![Sbnb Linux: Serial number on the top of the chassis case](images/serial-number-chassis.png)


### Serial number in the ASRock BMC Web User Interface (WebUI)
![Sbnb Linux: Serial number in the ASRock BMC Web User Interface (WebUI)](images/serial-number-BMC.png)


### Serial number retrieved using `dmidecode` on the Linux command line
![Sbnb Linux: Serial number retrieved using `dmidecode` on the Linux command line](images/serial-number-dmidecode.png)

### Machine registered in Tailscale (tailnet)
![Sbnb Linux: Machine registered in Tailscale (tailnet)](images/serial-number-tailscale.png)


## Note:
When the motherboard serial number reported by dmidecode is a placeholder such as `To be filled by O.E.M.`, `Not Specified`, or `Default string`, the system will use the MAC address of the first physical network interface to generate the hostname (e.g. `sbnb-345a6078df18`). If no physical interface is found, it will fall back to a random hostname.
