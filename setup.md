manual bootstrapping
- install proxmox to gateway node
  - hostname: `pavo.internal`
  - ip/gateway/dns should be setup normally to allow internet, make sure the node has a static lease from the router
- install truenas to storage node
  - Select **1) Configure network interfaces**
    - **ipv4_dhcp:** `no`
    - **aliases:** `10.127.0.2/24`
  - Select **2) Configure network settings**
    - **hostname:** set to hostname (TODO: might be able to do auto)
- prep gateway for ansible
  - accept host key to gateway
  - `ssh-copy-id` to gateway
- prep truenas for ansible
  - port forward using `ssh -L 8443:10.127.0.2:443 pavo`
  - Navigate **System** > **Services** > **SSH**
    - **Status:** `Running`
    - **Start Automatically:** `enabled`
    - **Password Login Groups:** `truenas_admin`
    - **Allow Password Authentication:** `enabled`

then run the ansible bootstrap playbook

- opnsense setup (through proxmox ui)
  - confirm the ui has the "Welcome! OPNsense is running in live mode from install media" message.
  - log in as installer (u: installer, pw: opnsense)
  - **OPNsense Installer:** `Install (ZFS)`
  - complete installation
  - confirm the ui no longer shows live mode on reboot
  - log in as root (u: root, pw: opnsense)
  - Select **1) Assign interfaces**
    - **Do you want to configure LAGGs now:** `n`
    - **Do you want to configure VLANs now:** `n`
    - **Enter the WAN interface name:** `vtnet0`
    - **Enter the LAN interface name:** `vtnet1`
    - **Enter the Optional interface 1 name** `<Enter>`
    - **Do you want to proceed:** `y`
  - Select **2) Set interface IP address:**
    - **Enter interface to configure:** `2`
      - **Configure IPv4 address WAN interface via DHCP:** `y`
      - **Configure IPv6 address WAN interface via DHCP6:** `y`
      - **Do you want to change HTTPS to HTTP:** `n`
      - **Do you want a new certificate:** `n`
      - **Restore web GUI access defaults:** `n`
  - Select **2) Set interface IP address:**
    - **Enter interface to configure:** `1`
      - **Configure IPv4 address WAN interface via DHCP:** `n`
      - **Enter the new LAN IPv4 address:** `10.127.0.254`
      - **Enter the new LAN IPv4 subnet bit count:** `24`
      - **IPv4 upstream gateway address:** `<Enter>`
      - **Configure IPv6 address LAN interface via WAN tracking:** `n`
      - **Configure IPv6 address LAN interface via DHCP6:** `n`
      - **Enter the new LAN IPv6 address:** `<Enter>`
      - **Do you want to enable the DHCP server on LAN:** `n`
      - **Do you want to change HTTPS to HTTP:** `n`
      - **Do you want a new certificate:** `n`
      - **Restore web GUI access defaults:** `n`

[//]: # (maybe put this somewhere else)
set up ansible ssh access to carina
- `ssh-copy-id -o ProxyJump=root@192.168.4.75 truenas_admin@10.127.0.2`
- get serial for drives in pool with `lsblk -S` or `midclt call disk.query | jq`
- 

my personal notes
- pavo 2.5 installed nic (lan): `***REMOVED***`
- pavo motherboard nic (wan): `***REMOVED***`
