import pytest

from reference.meihua_ref import meihua


def test_xiantian_numbers_moving_line_and_body_use():
    result = meihua(1, 1, "子")
    assert result["upper_number"] == 1 and result["lower_number"] == 1
    assert result["original"]["name"] == "乾为天"
    assert result["mutual"]["name"] == "乾为天"
    assert result["moving_line"] == 3
    assert result["changed"]["name"] == "天泽履"
    assert result["body_use_relation"] == "比和"


def test_zero_remainders_map_to_eight_and_six():
    result = meihua(8, 8, "亥")
    assert result["upper_number"] == 8 and result["lower_number"] == 8
    # 8+8+12=28，除六余四，故为四爻动。
    assert result["moving_line"] == 4


@pytest.mark.parametrize("bad", (0, -1, True, 1.0))
def test_positive_integer_validation(bad):
    with pytest.raises(ValueError):
        meihua(bad, 1, "子")
