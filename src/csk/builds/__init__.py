"""Platform-neutral identities for the declarative build domain."""

from typing import Final, Literal


BuildDriver = Literal["go-v1"]

GO_V1_DRIVER: Final[BuildDriver] = "go-v1"
SUPPORTED_BUILD_DRIVERS: Final[frozenset[str]] = frozenset({GO_V1_DRIVER})
