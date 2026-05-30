from abc import ABC, abstractmethod

class BaseEscrowProvider(ABC):
    @abstractmethod
    def initiate_transaction(self, order, vendor_bank, existing_txn_number=None):
        """
        Initiates a new transaction or updates an existing transaction with the provider.
        Should return a dict containing:
        {
            "success": bool,
            "payment_link": str or None,
            "transaction_number": str,
            "provider_transaction_id": str or None,
            "provider_reference": str or None,
            "amount": float,
            "fee_amount": float,
            "error_message": str or None
        }
        """
        pass

    @abstractmethod
    def verify_transaction(self, provider_reference):
        """
        Verifies the status of a transaction.
        """
        pass

    @abstractmethod
    def handle_webhook(self, payload, signature_header=None):
        """
        Processes a webhook from the provider.
        """
        pass
