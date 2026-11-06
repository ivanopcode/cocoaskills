This metadata-only FIDO middleware enrolls throwaway Ed25519 security-key
containers through real OpenSSH `ssh-keygen`. Each test build receives generated
public bytes and a trace path under its disposable directory. The key handle is
opaque fixture data. Signing, PINs, touch and resident keys are unsupported;
attempts are logged so metadata inspection cannot silently use them.

`sk-api.h` is the public middleware ABI from OpenSSH portable V_10_2_P1,
retained with its license:
https://github.com/openssh/openssh-portable/blob/V_10_2_P1/sk-api.h

This fixture establishes container/probe behavior, not hardware compatibility.
