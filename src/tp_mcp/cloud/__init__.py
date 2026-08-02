"""Cloud Run transport, OAuth, and request-scoped TrainingPeaks authentication.

Cloud dependencies are imported lazily so the local stdio server keeps working
with the base ``tp-mcp`` installation.
"""

from tp_mcp.cloud.config import CloudConfig

__all__ = ["CloudConfig"]
