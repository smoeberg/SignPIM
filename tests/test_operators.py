import pytest
from engine.operators.pim import ean13_check

def test_ean13_check():
    # Valid EAN-13
    assert ean13_check("5701234567899") is False # No violation
    # Invalid EAN-13
    assert ean13_check("12345") is True # Violation
