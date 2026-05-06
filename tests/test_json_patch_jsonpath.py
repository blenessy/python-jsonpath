"""Tests for JSON Patch operations targeting nodes with JSONPath queries."""

import json
import re

import pytest

from jsonpath import JSONPatch
from jsonpath import compile as jsonpath_compile
from jsonpath import patch
from jsonpath.exceptions import JSONPatchError
from jsonpath.exceptions import JSONPatchTestFailure


def test_replace_object_value_by_jsonpath() -> None:
    """A JSONPath can target an object value for the replace op."""
    data = {"book": {"title": "Moby Dick", "author": "Herman Melville"}}
    result = patch.apply(
        [{"op": "replace", "path": "$.book.title", "value": "foo"}],
        data,
    )
    assert result == {"book": {"title": "foo", "author": "Herman Melville"}}


def test_replace_array_element_by_filter() -> None:
    """A JSONPath filter can target a single array element for replace."""
    data = {
        "categories": [
            {"id": 1, "name": "fiction"},
            {"id": 2, "name": "non-fiction"},
        ]
    }
    result = patch.apply(
        [{"op": "replace", "path": "$.categories[?(@.id == 1)].name", "value": "horror"}],
        data,
    )
    assert result == {
        "categories": [
            {"id": 1, "name": "horror"},
            {"id": 2, "name": "non-fiction"},
        ]
    }


def test_replace_multiple_matches() -> None:
    """A JSONPath that matches multiple nodes replaces each one."""
    data = {"items": [{"v": 1}, {"v": 2}, {"v": 3}]}
    result = patch.apply(
        [{"op": "replace", "path": "$.items[*].v", "value": 0}],
        data,
    )
    assert result == {"items": [{"v": 0}, {"v": 0}, {"v": 0}]}


def test_remove_by_jsonpath_descending_indices() -> None:
    """Removing multiple array matches must process in reverse so indices are
    valid as we delete."""
    data = {"items": [1, 2, 3, 4, 5]}
    result = patch.apply(
        [{"op": "remove", "path": "$.items[?(@ > 2)]"}],
        data,
    )
    assert result == {"items": [1, 2]}


def test_remove_by_jsonpath_object_property() -> None:
    data = {"a": 1, "b": 2, "c": 3}
    result = patch.apply(
        [{"op": "remove", "path": "$.b"}],
        data,
    )
    assert result == {"a": 1, "c": 3}


def test_test_op_passes_for_all_matches() -> None:
    data = {"items": [{"v": 0}, {"v": 0}]}
    # Does not raise
    patch.apply(
        [{"op": "test", "path": "$.items[*].v", "value": 0}],
        data,
    )


def test_test_op_fails_when_one_match_differs() -> None:
    data = {"items": [{"v": 0}, {"v": 1}]}
    with pytest.raises(JSONPatchTestFailure, match="test failed"):
        patch.apply(
            [{"op": "test", "path": "$.items[*].v", "value": 0}],
            data,
        )


def test_test_op_fails_when_no_matches() -> None:
    data = {"items": []}
    with pytest.raises(JSONPatchTestFailure, match="test failed"):
        patch.apply(
            [{"op": "test", "path": "$.items[*].v", "value": 0}],
            data,
        )


def test_replace_no_matches_is_a_noop() -> None:
    """A JSONPath that matches nothing simply applies no operations."""
    data = {"items": [{"v": 0}]}
    result = patch.apply(
        [{"op": "replace", "path": "$.items[?(@.v > 100)].v", "value": 99}],
        data,
    )
    assert result == {"items": [{"v": 0}]}


def test_add_to_existing_array_element_by_jsonpath() -> None:
    """When a JSONPath matches an existing array index, add inserts before it
    just like a JSON Pointer would."""
    data = {"items": [10, 20, 30]}
    result = patch.apply(
        [{"op": "add", "path": "$.items[1]", "value": 15}],
        data,
    )
    assert result == {"items": [10, 15, 20, 30]}


def test_jsonpath_in_move_op_single_match() -> None:
    data = {"a": {"x": 1}, "b": {}}
    result = patch.apply(
        [{"op": "move", "from": "$.a.x", "path": "/b/y"}],
        data,
    )
    assert result == {"a": {}, "b": {"y": 1}}


def test_jsonpath_in_move_op_multiple_matches_raises() -> None:
    data = {"items": [{"v": 1}, {"v": 2}]}
    with pytest.raises(JSONPatchError, match="matched multiple nodes"):
        patch.apply(
            [{"op": "move", "from": "$.items[*].v", "path": "/dest"}],
            data,
        )


def test_jsonpath_in_move_op_no_matches_raises() -> None:
    data = {"a": {}}
    with pytest.raises(JSONPatchError, match="did not match any nodes"):
        patch.apply(
            [{"op": "move", "from": "$.missing", "path": "/b"}],
            data,
        )


def test_jsonpath_in_copy_op_single_match() -> None:
    data = {"a": {"x": 1}, "b": {}}
    result = patch.apply(
        [{"op": "copy", "from": "$.a.x", "path": "/b/y"}],
        data,
    )
    assert result == {"a": {"x": 1}, "b": {"y": 1}}


def test_invalid_jsonpath_in_patch_raises() -> None:
    with pytest.raises(JSONPatchError):
        # `$.[` is not a valid JSONPath query.
        JSONPatch([{"op": "replace", "path": "$.[", "value": 1}])


def test_jsonpath_round_trips_in_asdicts() -> None:
    """The original JSONPath string is preserved in `asdicts()`."""
    p = JSONPatch().replace("$.book.title", "foo")
    assert p.asdicts() == [
        {"op": "replace", "path": "$.book.title", "value": "foo"}
    ]


def test_jsonpath_in_builder_via_string() -> None:
    p = JSONPatch().replace("$.book.title", "foo")
    data = {"book": {"title": "Moby Dick"}}
    assert p.apply(data) == {"book": {"title": "foo"}}


def test_jsonpath_in_builder_via_compiled_path() -> None:
    """Builder methods accept a compiled `JSONPath` instance directly."""
    compiled = jsonpath_compile("$.book.title")
    p = JSONPatch().replace(compiled, "foo")
    data = {"book": {"title": "Moby Dick"}}
    assert p.apply(data) == {"book": {"title": "foo"}}


def test_loads_from_json_string() -> None:
    """JSONPath operations can be parsed from a JSON string."""
    patch_doc = json.dumps(
        [
            {"op": "replace", "path": "$.a", "value": 1},
            {"op": "remove", "path": "$.b"},
        ]
    )
    p = JSONPatch(patch_doc)
    assert p.apply({"a": 0, "b": 0, "c": 0}) == {"a": 1, "c": 0}


def test_re_evaluation_per_op() -> None:
    """Each op evaluates its JSONPath against the current document state."""
    data = {"a": 1, "b": 2}
    # Op 1 sets a to 5; op 2 only matches if a > 4
    result = patch.apply(
        [
            {"op": "replace", "path": "$.a", "value": 5},
            {"op": "replace", "path": "$[?(@ > 4)]", "value": 99},
        ],
        data,
    )
    assert result == {"a": 99, "b": 2}


def test_invalid_jsonpath_message_includes_op_index() -> None:
    with pytest.raises(JSONPatchError, match=re.escape("(replace:0)")):
        JSONPatch([{"op": "replace", "path": "$.[", "value": 1}])
