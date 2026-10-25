"""Built-in strategy library. Importing this package registers every strategy."""

from app.modules.strategies.library import classics  # noqa: F401
# Requires xgboost; imported here so the registry exposes it when present.
from app.modules.strategies.library import ml  # noqa: F401
from app.modules.strategies.library import cn_hybrid  # noqa: F401
