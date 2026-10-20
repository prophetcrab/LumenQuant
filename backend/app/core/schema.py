"""Import every module model so Base.metadata is complete."""

from app.core import audit  # noqa: F401
from app.modules.accounts import models as accounts_models  # noqa: F401
from app.modules.market import models as market_models  # noqa: F401
from app.modules.trading import models as trading_models  # noqa: F401
