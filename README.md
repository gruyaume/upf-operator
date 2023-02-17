# upf-operator

Open source 5G Core UPF Operator.

## Usage

```bash
juju deploy upf-operator --trust --channel=edge
```

## Image

- **bessd**: omecproject/upf-epc-bess:master-5786085
- **routectl**: omecproject/upf-epc-bess:master-5786085
- **web**: omecproject/upf-epc-bess:master-5786085
- **pfcp-agent**: omecproject/upf-epc-pfcpiface:master-5786085
- **arping**: registry.aetherproject.org/tools/busybox:stable
