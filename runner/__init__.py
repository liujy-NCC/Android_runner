"""Runner: a permission-controlled, auditable Android PoC execution framework.

Scope of this package (v1, generic framework):
  * restricted HTTP/JSON controller API with session-scoped credentials
  * session / candidate / build / attempt state machines with independent IDs
  * SQLite budget, idempotency, operation and event ledgers
  * SHA-256 content-addressed artifact and evidence store
  * replaceable BuildProfile / DeviceAdapter / Collector / OracleAdapter /
    FeedbackProjector interfaces with deterministic mock implementations
  * an independent supervisor that finalizes by persisted deadlines

This package deliberately exposes no generic shell, PowerShell, ADB or
filesystem entry point. Real emulator / ADB / AND-000008 PoC integration is
NOT part of this stage.
"""

SERVICE_VERSION = "0.1.0"
SCHEMA_VERSION = "1.0"
API_PREFIX = "/v1"

__all__ = ["SERVICE_VERSION", "SCHEMA_VERSION", "API_PREFIX"]
