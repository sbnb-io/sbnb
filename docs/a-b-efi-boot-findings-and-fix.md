# Reefy A/B EFI Boot Findings and Fix

Date: 2026-06-29

## Executive Summary

Reefy OTA firmware updates use two EFI slots, `reefy-a` and `reefy-b`. The
update boots the inactive slot once through standard UEFI `BootNext`; after
health checks pass, `reefy-efi confirm` makes the new slot persistent.

On two real devices, the old confirmation method was not durable:

- GMKtec NucBox K13 with AMI Aptio firmware
- MSI PRO B550M-VC WIFI, used by the `gputer` machine

Both devices accepted standard Linux `BootOrder` writes, but firmware later
reverted to its own BBS/default boot priority. The symptom was:

```text
OTA boots the new slot once -> health checks pass -> Linux reports the new
BootOrder -> a later plain reboot returns to the previous slot.
```

We proved the root issue is not a bad EFI image or bad `BootNext`; both slots
boot correctly. The issue is firmware state outside plain `BootOrder`.

The chosen fix is to stay within standard UEFI operations while forcing
firmware to refresh its hidden/default state:

```text
1. Create fresh reefy-a and reefy-b Boot#### entries with normal efibootmgr -c.
2. Detect the firmware-assigned Boot#### numbers.
3. Set BootOrder to the new active,new inactive pair.
4. Delete the old Reefy Boot#### entries by exact old boot number.
5. Reboot normally.
```

This was validated both ways on both affected devices:

```text
K13:    A -> B and B -> A
gputer: A -> B and B -> A
```

After the next boot, each firmware refreshed its own private/default state:

- K13 refreshed AMI `OldBootOrder` and `UefiDevOrder`.
- gputer refreshed MSI/AMI `DefaultBootOrder`.

The production implication: `reefy-efi confirm` should no longer just run
`efibootmgr -o CURRENT,DEFAULT`. It should commit with fresh boot entries and
leave vendor-private EFI variables untouched.

This note documents an OTA firmware update issue where a device updated
successfully to the inactive Reefy EFI slot, booted that slot once, but then
returned to the previous slot after a later normal reboot.

The key finding is that some AMI firmware keeps a second, vendor-specific
BBS priority list in addition to standard UEFI `BootOrder`. Updating standard
`BootOrder` alone is not enough on affected firmware. Directly writing private
variables can work on one board, but the portable fix is to recreate the
standard Reefy `Boot####` entries with fresh firmware-assigned numbers and set
`BootOrder` to that new pair.

## Background

Reefy uses two EFI system partitions:

- `reefy-a`: first EFI system partition
- `reefy-b`: second EFI system partition

The associated `Boot####` numbers are firmware-assigned and may change when
Reefy refreshes the boot entries.

The OTA update flow writes the new EFI image to the inactive slot and sets
`BootNext` to try it once. After a healthy boot, `reefy-boot-confirm` runs
`reefy-efi confirm`, which updates standard UEFI `BootOrder` so the new slot
becomes the persistent default.

On the K13, this standard flow partially worked:

- `BootNext=0000` reliably booted `reefy-a`.
- `efibootmgr -o 0000,0001` made Linux report `BootOrder: 0000,0001`.
- A later normal reboot still selected `reefy-b` unless the BIOS BBS priority
  menu was also changed.

## Standards Context

Standard UEFI boot selection is based on `Boot####` entries, `BootOrder`, and
the one-shot `BootNext` variable. `efibootmgr` controls these standard
variables:

- `efibootmgr -o ...`: set standard `BootOrder`
- `efibootmgr -n ...`: set standard `BootNext`
- `efibootmgr -c ...`: create standard `Boot####` entries

References:

- UEFI Boot Manager: https://uefi.org/specs/UEFI/2.10/03_Boot_Manager.html
- `efibootmgr` manual: https://man.archlinux.org/man/efibootmgr.8.en
- Linux `efivarfs` notes, including non-standard variable caution:
  https://docs.kernel.org/filesystems/efivarfs.html

## GMKtec NucBox K13

### DMI Identity

Captured with `dmidecode -t0`, `dmidecode -t1`, and `dmidecode -t2`.

```text
BIOS Vendor: NucBox_K13
BIOS Version: V1.01
BIOS Release Date: 02/06/2026
BIOS Revision: 5.32

System Manufacturer: GMKtec
System Product Name: NucBox K13
System SKU: K13-001
System Family: MINI

Baseboard Manufacturer: GMKtec
Baseboard Product Name: GMKtec
```

### EFI Entries

The original K13 Reefy entries were:

```text
Active:   reefy-a (Boot0000)
Inactive: reefy-b -> /dev/sda2
reefy-a:   Boot0000 -> /dev/sda1
reefy-b:   Boot0001 -> /dev/sda2
BootCurrent: 0000
BootOrder: 0000,0001
```

Verbose boot entries:

```text
Boot0000* reefy-a HD(1,GPT,dc5c2d02-dcf0-40e7-b6ea-cfb18571d1ad,0x800,0x200000)/\EFI\Boot\bootx64.efi
Boot0001* reefy-b HD(2,GPT,cf3e0a07-c2b0-4e16-85b1-010f83a07212,0x200800,0x200000)/\EFI\Boot\bootx64.efi
```

### Observed Failure

Initial update log showed a correct A/B update:

```text
Active: reefy-b (Boot0001)
Inactive: reefy-a (Boot0000)
Target: /dev/sda1
BootNext set to Boot0000 - safe A/B update
```

After the OTA reboot, the machine booted `reefy-a` correctly:

```text
BootCurrent: 0000
BootOrder: 0000,0001
```

After a later plain `reboot`, it returned to `reefy-b`:

```text
BootCurrent: 0001
BootOrder: 0001,0000
```

The important detail: when the OS booted back into `reefy-b`, Linux already
saw `BootCurrent=0001` and `BootOrder=0001,0000` at early boot. This means
the flip happened in firmware before Linux userspace could run Reefy boot
services.

### Experiments

#### 1. BootNext to A

From a B boot:

```sh
sudo reefy-efi set-next a
sudo reboot
```

Result: the device booted `reefy-a`, and `reefy-boot-confirm` committed:

```text
BootCurrent: 0000
BootOrder: 0000,0001
[reefy-efi] Committed Boot0000 as default
```

Conclusion: the `reefy-a` image and boot entry were valid. The issue was not
that A was unbootable.

#### 2. Standard BootOrder only

From a B boot:

```sh
sudo mount -o remount,rw /sys/firmware/efi/efivars
sudo efibootmgr -o 0000,0001
sync
sudo mount -o remount,ro /sys/firmware/efi/efivars
efibootmgr
sudo reboot
```

Before reboot, Linux reported:

```text
BootOrder: 0000,0001
```

After reboot, the machine returned to B:

```text
BootCurrent: 0001
BootOrder: 0001,0000
```

Conclusion: standard `BootOrder` alone did not update the firmware's effective
BBS priority.

#### 3. BIOS Menu

When entering AMI Aptio Setup after Linux had written standard
`BootOrder: 0000,0001`, the BIOS still showed:

```text
Boot Option #1 [reefy-b]
Boot Option #2 [reefy-a]
```

In the top-level boot page, `FIXED BOOT ORDER Priorities` also showed:

```text
Boot Option #1 [USB Device: reefy-b]
```

After manually changing the nested `UEFI USB Drive BBS Priorities` menu to:

```text
Boot Option #1 [reefy-a]
Boot Option #2 [reefy-b]
```

and saving, the machine booted `reefy-a`.

Conclusion: AMI's BBS priority menu was the effective selector on this board.

### AMI Variables on K13

The K13 exposes these relevant variables:

```text
BootOrder-8be4df61-93ca-11d2-aa0d-00e098032b8c
OldBootOrder-0c923ca9-df73-4ac8-b6d2-98ddc30d99fc
UefiDevOrder-0c923ca9-df73-4ac8-b6d2-98ddc30d99fc
OriUefiDevOrder-0c923ca9-df73-4ac8-b6d2-98ddc30d99fc
DefaultUefiDevOrder-0c923ca9-df73-4ac8-b6d2-98ddc30d99fc
FixedBoot-de8ab926-efda-4c23-bbc4-98fd29aa0069
FixedBootGroup-0c923ca9-df73-4ac8-b6d2-98ddc30d99fc
BootMediaInfo-5bd6b672-b6ea-4d6a-b590-18a932b78794
```

The AMI variables are marked immutable by Linux:

```text
----i----------------- OldBootOrder-0c923ca9-df73-4ac8-b6d2-98ddc30d99fc
----i----------------- UefiDevOrder-0c923ca9-df73-4ac8-b6d2-98ddc30d99fc
```

This is consistent with Linux `efivarfs` protecting non-standard variables
from accidental removal or corruption.

### K13 Variable Encoding

When BIOS BBS priority was manually set to A first:

```text
BootOrder     0700000000000100
OldBootOrder  0700000000000100
UefiDevOrder  07000000200000000a000000000001000000
```

When BIOS BBS priority was manually set to B first:

```text
BootOrder     0700000001000000
OldBootOrder  0700000001000000
UefiDevOrder  07000000200000000a000100000000000000
```

Observed structure:

```text
BootOrder:
  07 00 00 00  <little-endian Boot#### list>

UefiDevOrder:
  07 00 00 00  20 00 00 00  0a 00  <little-endian Boot#### list plus terminator/padding>
```

For the two-slot Reefy case:

```text
A first tail: 00 00 01 00 00 00
B first tail: 01 00 00 00 00 00
```

Changing standard `BootOrder` with `efibootmgr -o` did **not** change
`UefiDevOrder`:

```text
After software B-first:
BootOrder     0700000001000000
OldBootOrder  0700000000000100
UefiDevOrder  07000000200000000a000000000001000000
```

### Rejected Direct K13 Variable Write

Directly writing `BootOrder`, `OldBootOrder`, and `UefiDevOrder` from Linux
also made K13 honor the desired slot. That proved the AMI-private variable was
part of the effective selector, but it is not the chosen production approach:
the variables are undocumented, immutable in Linux, and board-specific.

### Successful K13 Auto-Numbered Renumbering Experiment

The previous experiment forced specific boot numbers. We also tested the
production-oriented variant where firmware chooses the new `Boot####` numbers.

Starting state:

```text
Active:   reefy-a (Boot0020)
BootOrder: 0020,0021
```

Sequence:

```text
old_a=0020
old_b=0021
create new reefy-a without specifying -b -> firmware assigned Boot0000
create new reefy-b without specifying -b -> firmware assigned Boot0001
set BootOrder=0001,0000
delete old Boot0020 and Boot0021
```

Before reboot:

```text
BootOrder: 0001,0000
BootOrder     0700000001000000
OldBootOrder  0700000020002100
UefiDevOrder  07000000200000000a002000000021000000
```

After reboot:

```text
Active:   reefy-b (Boot0001)
BootOrder: 0001,0000

BootOrder     0700000001000000
OldBootOrder  0700000001000000
UefiDevOrder  07000000200000000a000100000000000000
```

Conclusion: fixed boot numbers are not required. Letting firmware allocate
free `Boot####` numbers, then ordering those new entries and deleting the old
entries, still causes K13 firmware to refresh the AMI-private variables during
boot.

We then ran the reverse auto-numbered direction from B active:

```text
Starting state:
Active:   reefy-b (Boot0001)
BootOrder: 0001,0000

old_a=0000
old_b=0001
create new reefy-a without specifying -b -> firmware assigned Boot0003
create new reefy-b without specifying -b -> firmware assigned Boot0004
set BootOrder=0003,0004
delete old Boot0000 and Boot0001
```

Before reboot:

```text
BootOrder: 0003,0004
BootOrder     0700000003000400
OldBootOrder  0700000001000000
UefiDevOrder  07000000200000000a000100000000000000
```

After reboot:

```text
Active:   reefy-a (Boot0003)
BootOrder: 0003,0004

BootOrder     0700000003000400
OldBootOrder  0700000003000400
UefiDevOrder  07000000200000000a000300000004000000
```

Conclusion: the auto-numbered approach works in both directions on K13.

## MSI PRO B550M-VC WIFI, gputer

### DMI Identity

Captured with `dmidecode -t0`, `dmidecode -t1`, and `dmidecode -t2`.

```text
BIOS Vendor: American Megatrends International, LLC.
BIOS Version: H.C0
BIOS Release Date: 07/15/2024
BIOS Revision: 5.17

System Manufacturer: Micro-Star International Co., Ltd.
System Product Name: MS-7C95

Baseboard Manufacturer: Micro-Star International Co., Ltd.
Baseboard Product Name: PRO B550M-VC WIFI (MS-7C95)
Baseboard Version: 3.0
```

### BIOS Behavior

MSI Click BIOS shows Reefy slots under:

```text
Settings -> Boot -> UEFI Hard Disk Drive BBS Priorities
```

The user observed:

```text
Boot Option #1 [reefy-b]
Boot Option #2 [reefy-a]
```

After manually changing BBS priority to A first and saving, gputer booted A:

```text
BootCurrent: 0000
BootOrder: 0000,0001
Boot0000* reefy-a
Boot0001* reefy-b
```

### EFI State After Manual BIOS A-First Save

```text
Active:   reefy-a (Boot0000)
Inactive: reefy-b -> /dev/sda2
BootCurrent: 0000
BootOrder: 0000,0001
```

Verbose boot entries:

```text
Boot0000* reefy-a HD(1,GPT,2dc408de-b5aa-43d4-92e3-0466c67ca2d6,0x800,0x200000)/\EFI\Boot\bootx64.efi
Boot0001* reefy-b HD(2,GPT,773fe2bf-cdb5-4a93-8c7a-1cddb17d364f,0x200800,0x200000)/\EFI\Boot\bootx64.efi
```

### Variable Layout Difference

gputer does **not** expose the K13-style variables:

```text
UefiDevOrder-0c923ca9-df73-4ac8-b6d2-98ddc30d99fc
OldBootOrder-0c923ca9-df73-4ac8-b6d2-98ddc30d99fc
FixedBootGroup-0c923ca9-df73-4ac8-b6d2-98ddc30d99fc
BootMediaInfo-5bd6b672-b6ea-4d6a-b590-18a932b78794
```

It does expose:

```text
BootOrder-8be4df61-93ca-11d2-aa0d-00e098032b8c
DefaultBootOrder-45cf35f6-0d6e-4d04-856a-0370a5b16f53
FixedBoot-de8ab926-efda-4c23-bbc4-98fd29aa0069
AMITSESetup-c811fa38-42c8-4579-a9bb-60e94eddfb34
Setup-ec87d643-eba4-4bb5-a1e5-3f3e36b20da9
```

After manual A-first BIOS save:

```text
BootOrder        0700000000000100
DefaultBootOrder 0700000000000100
```

After manual B-first BIOS save:

```text
BootOrder        0700000001000000
DefaultBootOrder 0700000001000000
```

Full raw efivar snapshots were captured for manual A-first and B-first BIOS
states. Each snapshot contained 111 efivar files. A byte-for-byte comparison
found only these variables changed:

```text
BootCurrent-8be4df61-93ca-11d2-aa0d-00e098032b8c
BootOrder-8be4df61-93ca-11d2-aa0d-00e098032b8c
DefaultBootOrder-45cf35f6-0d6e-4d04-856a-0370a5b16f53
LoaderDevicePartUUID-4a67b082-0a4c-41cf-b6c7-440b29bb8c4f
MonotonicCounter-01368881-c4ad-4b1d-b631-d57a8ec8db6b
ProFileAutoSaveInfo-515b6cdf-bbe7-4509-83cc-d725903d522a
```

Relevant byte differences:

```text
BootCurrent:
  A-first: 060000000000
  B-first: 060000000100

BootOrder:
  A-first: 0700000000000100
  B-first: 0700000001000000

DefaultBootOrder:
  A-first: 0700000000000100
  B-first: 0700000001000000

LoaderDevicePartUUID:
  A-first: UTF-16 "2DC408DE-B5AA-43D4-92E3-0466C67CA2D6"
  B-first: UTF-16 "773FE2BF-CDB5-4A93-8C7A-1CDDB17D364F"
```

`FixedBoot`, `AMITSESetup`, and `Setup` were byte-identical between manual
A-first and manual B-first snapshots.

### Failed MSI Software Write

From the B-first state, we wrote:

```text
BootOrder        0700000000000100
DefaultBootOrder 0700000000000100
```

This required temporarily clearing immutable on `DefaultBootOrder`, then
restoring it. Before reboot, Linux reported:

```text
BootCurrent: 0001
BootOrder: 0000,0001
DefaultBootOrder: 0700000000000100
```

After a plain reboot, firmware reverted both variables and booted B:

```text
Active: reefy-b
BootCurrent: 0001
BootOrder: 0001,0000
DefaultBootOrder: 0700000001000000
```

Conclusion: on MSI/gputer, `DefaultBootOrder` is not the root source of BBS
priority. It appears to be rewritten by firmware from a BBS setting that is
not visible in efivarfs, or at least not changed in the captured efivar set.
Do not apply the K13 `UefiDevOrder` write path to MSI/gputer.

### MSI Standard BootOrder Reproduction

We first confirmed gputer was affected in its current state. Starting from
manual BIOS A-first:

```text
Active:   reefy-a (Boot0000)
BootOrder: 0000,0001

BootOrder        0700000000000100
DefaultBootOrder 0700000000000100
```

We changed only standard `BootOrder` to B-first:

```text
Before reboot:
BootOrder        0700000001000000
DefaultBootOrder 0700000000000100
```

After reboot, firmware restored A-first and booted A:

```text
Active:   reefy-a (Boot0000)
BootOrder: 0000,0001

BootOrder        0700000000000100
DefaultBootOrder 0700000000000100
```

That reproduced the affected behavior: standard `BootOrder` alone was not
enough when it disagreed with firmware's BBS/default state.

### Successful MSI Auto-Numbered Renumbering Experiment

We repeated the production-oriented variant where firmware chooses the new
`Boot####` numbers.

Starting state:

```text
Active:   reefy-a (Boot0020)
BootOrder: 0020,0021
```

Sequence:

```text
old_a=0020
old_b=0021
create new reefy-a without specifying -b -> firmware assigned Boot0000
create new reefy-b without specifying -b -> firmware assigned Boot0001
set BootOrder=0001,0000
delete old Boot0020 and Boot0021
```

Before reboot:

```text
BootOrder: 0001,0000
BootOrder        0700000001000000
DefaultBootOrder 0700000020002100
```

After reboot:

```text
Active:   reefy-b (Boot0001)
BootOrder: 0001,0000

BootOrder        0700000001000000
DefaultBootOrder 0700000001000000
```

Conclusion: fixed boot numbers are not required on MSI/gputer either. Letting
firmware allocate the new `Boot####` numbers, ordering those new entries, and
then deleting the old entries is sufficient for firmware to refresh
`DefaultBootOrder` during boot.

We then ran the reverse auto-numbered direction from B active:

```text
Starting state:
Active:   reefy-b (Boot0001)
BootOrder: 0001,0000

old_a=0000
old_b=0001
create new reefy-a without specifying -b -> firmware assigned Boot0002
create new reefy-b without specifying -b -> firmware assigned Boot0003
set BootOrder=0002,0003
delete old Boot0000 and Boot0001
```

Before reboot:

```text
BootOrder: 0002,0003
BootOrder        0700000002000300
DefaultBootOrder 0700000001000000
```

After reboot:

```text
Active:   reefy-a (Boot0002)
BootOrder: 0002,0003

BootOrder        0700000002000300
DefaultBootOrder 0700000002000300
```

Conclusion: the auto-numbered approach works in both directions on MSI/gputer.

## Implementation Implications

### Implemented Fix

When committing a new persistent slot order, `reefy-efi confirm` should create
both Reefy `Boot####` entries without forcing boot numbers, detect the
firmware-assigned numbers, set standard `BootOrder` to the desired new order,
and then delete the old Reefy entries.

This keeps Reefy on standard UEFI operations:

```text
1. Discover current valid reefy-a and reefy-b entries by label plus PARTUUID.
2. Create a fresh reefy-a entry with efibootmgr -c.
3. Create a fresh reefy-b entry with efibootmgr -c.
4. Detect the newly assigned Boot#### numbers by set difference.
5. Set BootOrder to new active,new inactive.
6. Delete the old entries by exact old Boot#### number.
7. Verify exactly one valid reefy-a and reefy-b remain.
8. Verify BootOrder starts with the new pair.
```

On K13, firmware refreshed `OldBootOrder` and `UefiDevOrder` itself during
the next boot. On MSI/gputer, firmware refreshed `DefaultBootOrder` itself
during the next boot.

The implementation needs a persistent or discoverable way to choose fresh
boot numbers. It cannot assume A is always `Boot0000` and B is always
`Boot0001`; `reefy-efi` must continue to identify slots by label and device
path or partition.

### Rejected Fixes

Direct AMI variable writes worked on K13 but should not be the production
path. If such a fallback is ever reconsidered, it must be board and shape
gated. The observed K13 compact `UefiDevOrder` layout was:

- `OldBootOrder-0c923ca9-df73-4ac8-b6d2-98ddc30d99fc` exists and is 8 bytes.
- `UefiDevOrder-0c923ca9-df73-4ac8-b6d2-98ddc30d99fc` exists and is 18 bytes.
- `UefiDevOrder` starts with `07 00 00 00 20 00 00 00 0a 00`.
- The two relevant `Boot####` numbers are present in the final list.

Then write:

```text
BootOrder = attrs + current + previous
OldBootOrder = attrs + current + previous
UefiDevOrder = attrs + 20 00 00 00 0a 00 + current + previous + 00 00
```

Such an implementation would need to:

- Back up/read the existing bytes before writing.
- Refuse to write if the variable shape does not match exactly.
- Temporarily clear immutable only on the specific known variables.
- Restore immutable immediately after writing.
- `sync` before remounting efivars read-only.
- Keep standard `BootOrder` as the primary path.

### BootNext Fallback

`BootNext` is still useful as a compatibility fallback for controlled reboots
because it reliably selected the intended slot on K13. However, `BootNext` is
one-shot and is consumed on the next boot attempt, so it does not solve hard
power loss or power cycle behavior by itself.

### Risk Notes

Writing vendor-specific EFI variables is riskier than writing standard UEFI
variables. Linux marks many non-standard variables immutable for a reason.
Any production write path must be narrow, board/shape-gated, and best-effort.

Do **not** write MSI `Setup` or `AMITSESetup` variables. The full A-first vs
B-first efivar diff showed those variables are unchanged, and the real
persistent BBS source was not identified in efivarfs.
