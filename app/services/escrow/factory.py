import os
from app.services.escrow.payscrow import PayscrowProvider

def get_escrow_provider():
    """
    Returns the active escrow provider based on environment configuration.
    Currently hardcoded to Payscrow, but allows easy switching in the future.
    """
    provider_name = os.environ.get('ACTIVE_ESCROW_PROVIDER', 'payscrow').lower()
    
    if provider_name == 'payscrow':
        return PayscrowProvider()
    
    # Fallback to Payscrow
    return PayscrowProvider()
