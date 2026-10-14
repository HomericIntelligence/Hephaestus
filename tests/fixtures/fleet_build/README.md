# Fleet build interoperability inputs

`controller.json` is the actual Agamemnon 508 service export with SHA256
`079117d07c3c2a2d33c0b93242af3737dc4caa2afe8642b8ad88324973bd16b8`.
It contains real admitted command, persisted grant and cancellation responses
from the controlled controller test. Its allocation/image/toolchain and terminal
execution fact are synthetic. It does not prove a build ran.

`snapshot/`, `commitment.json` and `snapshot-policy.json` are the actual snapshot
3231 producer/receiver inputs. The artifact contains two files, 91 bytes, with
manifest digest `c7a335c3ba3d5e724413879073b1b8d62125e23d1559a31790467998a3898565`.
The independent producer/receiver receipt digest is
`07c5ca51d984c20bb13c3015fbdf3ec8e4cb081b7ddbdff26cb15b18339e6566`.
The controller admitted this exact commitment. These are test inputs, not CI
results. Consumer tests re-read and verify their actual bytes.
