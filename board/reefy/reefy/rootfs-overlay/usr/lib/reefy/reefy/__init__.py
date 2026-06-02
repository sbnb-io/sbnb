"""reefy device-agent library.

Split across role modules so each /usr/bin executable imports only what
its role needs:
  - reefy.control    -> ControlPlane (MQTT; the only module importing paho)
  - reefy.dataplane  -> DataPlane (Varlink server; storage/container work)
  - reefy.storage    -> Storage (LVM/LUKS/XFS/volumes/boot-mount)
  - reefy.shared     -> cross-role helpers, constants, DPClient

Intentionally empty of eager imports: importing e.g. reefy.storage must
not drag in paho (only reefy.control needs it).
"""
