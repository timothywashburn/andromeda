## to keep track of the defining principles that guide the project

### Single Management Node

It is unlikely that a homelab setup grows to the point that it would require multiple management nodes. I see a world in which it might make sense to have multiple for HA but for the time being let's assume that there's always exactly one management node. The management node itself will run several VMs to orchestrate all management responsibilities. 

Note: Reference the diagram in NIST SP 800-223 for an approximate picture of what I'm suggesting.

Note: It is just a coincidence that the management node also runs my gateway infrastructure. This might end up becoming a dependent but fundamentally there's no reason it should have to be.

### Single Gateway VM

I need to do more research into what DMZs, bastion nodes, etc., but for the time being I will build towards a simple setup where all traffic enters/leaves the network through a single gateway VM.