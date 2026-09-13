- This code is still in development, which means the deployment you're working on is the only deployment and breaking changes are totally acceptable and preferred if they keep the codebase cleaner.

- You are not allowed to mutate the state of this repository or the systems you access without my prior consent. Focus on help me make the changes myself.
- Before bootstrapping, you can access pavo (proxmox gateway) with `ssh pavo`
- Before bootstrapping, you can access carina (truenas storage) with `ssh -J pavo truenas_admin@10.127.0.2`
- You can access public relay with `ssh tunnel`
- If the machine has been bootstrapped you can reach all machines directly via the internal IPs as they are all exposed to this computer through netbird
- if you're asking me to modify an existing file use + (green) - (red) and blue for moved to make the diff more human readable