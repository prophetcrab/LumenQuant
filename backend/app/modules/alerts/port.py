"""Port implemented by the alerts module.

Business modules raise alerts through this protocol instead of importing
`AlertService`. Two reasons:

* the dependency direction stays one-way (trading/risk/runtime → alerts), and
* tests can pass a no-op notifier without constructing a database-backed service.

`raise_alert` deliberately returns None: callers must not need to know the alert's
identity, and a caller that could inspect it would start branching on alerts.
"""

from __future__ import annotations

from typing import Protocol


class AlertPort(Protocol):
    def raise_alert(
        self,
        *,
        category: str,
        title: str,
        message: str = "",
        level: str = "warning",
        account_id: str = "",
        ref_type: str = "",
        ref_id: str = "",
        commit: bool = False,
    ) -> object: ...


class NullAlerts:
    """No-op implementation for tests and read-only contexts."""

    def raise_alert(self, **_kwargs) -> None:
        return None
