manual bootstrapping
- install proxmox to gateway node
  - hostname: `pavo.internal`
  - ip/gateway/dns should be setup normally to allow internet, make sure the node has a static lease from the router
- install truenas to storage node
  - Select **1) Configure network interfaces**
    - **ipv4_dhcp**: `no`
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



my personal notes
- pavo 2.5 installed nic (lan): `***REMOVED***`
- pavo motherboard nic (wan): `***REMOVED***`
