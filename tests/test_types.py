import pytest
from engine.types.standard import StringType, NumberType, EnumType

def test_string_type():
    st = StringType()
    assert st.cast(123) == "123"
    assert st.cast(None) is None

def test_number_type():
    nt = NumberType()
    assert nt.cast("100.5") == 100.5
    with pytest.raises(ValueError):
        nt.cast("not_a_number")

def test_enum_type():
    et = EnumType()
    assert et.validate("active", {"values": ["active", "draft"]}) is True
    with pytest.raises(ValueError):
        et.validate("invalid", {"values": ["active", "draft"]})
