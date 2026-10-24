from .models import UsageMetric
from decimal import Decimal
import logging

logger = logging.getLogger(__name__)

def record_usage(institute, metric_type, quantity=1, reference_id=None):
    """
    Records a usage metric for an institute.
    """
    if not institute:
        return None
        
    try:
        return UsageMetric.objects.create(
            institute=institute,
            metric_type=metric_type,
            quantity=Decimal(str(quantity)),
            reference_id=str(reference_id) if reference_id else None
        )
    except Exception as e:
        logger.warning(f"Failed to record usage metric ({metric_type}) for institute {institute}: {e}")
        return None
