"""Import every module model so Base.metadata is complete."""

from app.core import audit  # noqa: F401
from app.modules.accounts import models as accounts_models  # noqa: F401
