import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from continuum.core.canonical import canonical_json, digest

json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(),
    lambda children: st.lists(children) | st.dictionaries(st.text(), children),
    max_leaves=20)


@given(st.dictionaries(st.text(), json_values))
def test_canonical_json_is_order_independent(value):
    reordered = dict(reversed(list(value.items())))
    assert canonical_json(value) == canonical_json(reordered)
    assert digest(value) == digest(reordered)


def test_nan_is_rejected():
    with pytest.raises(ValueError):
        canonical_json({"x": math.nan})


def test_digest_format():
    assert digest({"a": 1}).startswith("sha256:")
    assert len(digest({"a": 1})) == len("sha256:") + 64
