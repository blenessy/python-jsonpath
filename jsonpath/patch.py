"""JSON Patch, as per RFC 6902.

In addition to the RFC 6902 standard, the `path` and `from` members of patch
operations may be JSONPath query strings (any string starting with `$`). When
used, the JSONPath is evaluated against the target document at the time the
operation is applied, and the operation is performed once for each matching
node.
"""

from __future__ import annotations

import copy
import json
from abc import ABC
from abc import abstractmethod
from io import IOBase
from typing import TYPE_CHECKING
from typing import Any
from typing import Dict
from typing import Iterable
from typing import List
from typing import Mapping
from typing import MutableMapping
from typing import MutableSequence
from typing import Optional
from typing import TypeVar
from typing import Union

from jsonpath._data import load_data
from jsonpath.exceptions import JSONPatchError
from jsonpath.exceptions import JSONPatchTestFailure
from jsonpath.exceptions import JSONPathError
from jsonpath.exceptions import JSONPointerError
from jsonpath.exceptions import JSONPointerIndexError
from jsonpath.exceptions import JSONPointerKeyError
from jsonpath.exceptions import JSONPointerTypeError
from jsonpath.path import CompoundJSONPath
from jsonpath.path import JSONPath
from jsonpath.pointer import UNDEFINED
from jsonpath.pointer import JSONPointer

if TYPE_CHECKING:
    from jsonpath.env import JSONPathEnvironment


class _JSONPathTarget:
    """A compiled JSONPath wrapping used as a JSON Patch operation target.

    Behaves like a `JSONPointer` from the perspective of `Op.apply`, but resolves
    to zero or more `JSONPointer` instances against the current target document.
    """

    __slots__ = ("query", "compiled")

    def __init__(
        self,
        query: str,
        compiled: Union[JSONPath, CompoundJSONPath],
    ) -> None:
        self.query = query
        self.compiled = compiled

    def __str__(self) -> str:
        return self.query

    def resolve(
        self,
        data: Union[MutableSequence[object], MutableMapping[str, object]],
    ) -> List[JSONPointer]:
        """Resolve this JSONPath query to a list of JSON Pointers."""
        return [
            JSONPointer.from_match(match) for match in self.compiled.finditer(data)
        ]


_Target = Union[JSONPointer, _JSONPathTarget]


def _resolve_pointers(
    path: _Target,
    data: Union[MutableSequence[object], MutableMapping[str, object]],
) -> List[JSONPointer]:
    """Return a list of `JSONPointer` instances for _path_ given _data_."""
    if isinstance(path, JSONPointer):
        return [path]
    return path.resolve(data)


def _single_pointer(
    path: _Target,
    data: Union[MutableSequence[object], MutableMapping[str, object]],
    op_name: str,
) -> JSONPointer:
    """Resolve _path_ to a single `JSONPointer`, raising if there are zero or
    multiple matches when _path_ is a JSONPath query."""
    if isinstance(path, JSONPointer):
        return path
    pointers = path.resolve(data)
    if not pointers:
        raise JSONPatchError(
            f"JSONPath {path.query!r} did not match any nodes ({op_name})"
        )
    if len(pointers) > 1:
        raise JSONPatchError(
            f"JSONPath {path.query!r} matched multiple nodes, "
            f"expected one ({op_name})"
        )
    return pointers[0]


class Op(ABC):
    """One of the JSON Patch operations."""

    name = "base"

    @abstractmethod
    def apply(
        self, data: Union[MutableSequence[object], MutableMapping[str, object]]
    ) -> Union[MutableSequence[object], MutableMapping[str, object]]:
        """Apply this patch operation to _data_."""

    @abstractmethod
    def asdict(self) -> Dict[str, object]:
        """Return a dictionary representation of this operation."""


class OpAdd(Op):
    """The JSON Patch _add_ operation."""

    __slots__ = ("path", "value")

    name = "add"

    def __init__(self, path: _Target, value: object) -> None:
        self.path = path
        self.value = value

    def apply(
        self, data: Union[MutableSequence[object], MutableMapping[str, object]]
    ) -> Union[MutableSequence[object], MutableMapping[str, object]]:
        """Apply this patch operation to _data_."""
        for pointer in _resolve_pointers(self.path, data):
            data = self._apply(data, pointer)
        return data

    def _apply(
        self,
        data: Union[MutableSequence[object], MutableMapping[str, object]],
        pointer: JSONPointer,
    ) -> Union[MutableSequence[object], MutableMapping[str, object]]:
        parent, obj = pointer.resolve_parent(data)
        if parent is None:
            # Replace the root object.
            # The following op, if any, will raise a JSONPatchError if needed.
            return self.value  # type: ignore

        target = pointer.parts[-1]
        if isinstance(parent, MutableSequence):
            if obj is UNDEFINED:
                if target == "-":
                    parent.append(self.value)
                else:
                    index = pointer._index(target)  # noqa: SLF001
                    if index == len(parent):
                        parent.append(self.value)
                    else:
                        raise JSONPatchError("index out of range")
            else:
                parent.insert(int(target), self.value)
        elif isinstance(parent, MutableMapping):
            parent[str(target)] = self.value
        else:
            raise JSONPatchError(
                f"unexpected operation on {parent.__class__.__name__!r}"
            )
        return data

    def asdict(self) -> Dict[str, object]:
        """Return a dictionary representation of this operation."""
        return {"op": self.name, "path": str(self.path), "value": self.value}


class OpAddNe(OpAdd):
    """A non-standard _add if not exists_ operation.

    This is like _OpAdd_, but only adds object/dict keys/values if they key does
    not already exist.

    **New in version 1.2.0**
    """

    __slots__ = ("path", "value")

    name = "addne"

    def _apply(
        self,
        data: Union[MutableSequence[object], MutableMapping[str, object]],
        pointer: JSONPointer,
    ) -> Union[MutableSequence[object], MutableMapping[str, object]]:
        parent, obj = pointer.resolve_parent(data)
        if parent is None:
            # Replace the root object.
            # The following op, if any, will raise a JSONPatchError if needed.
            return self.value  # type: ignore

        target = pointer.parts[-1]
        if isinstance(parent, MutableSequence):
            if obj is UNDEFINED:
                parent.append(self.value)
            else:
                parent.insert(int(target), self.value)
        elif isinstance(parent, MutableMapping) and target not in parent:
            parent[target] = self.value
        return data


class OpAddAp(OpAdd):
    """A non-standard add operation that appends to arrays/lists .

    This is like _OpAdd_, but assumes an index of "-" if the path can not
    be resolved rather than raising a JSONPatchError.

    **New in version 1.2.0**
    """

    __slots__ = ("path", "value")

    name = "addap"

    def _apply(
        self,
        data: Union[MutableSequence[object], MutableMapping[str, object]],
        pointer: JSONPointer,
    ) -> Union[MutableSequence[object], MutableMapping[str, object]]:
        parent, obj = pointer.resolve_parent(data)
        if parent is None:
            # Replace the root object.
            # The following op, if any, will raise a JSONPatchError if needed.
            return self.value  # type: ignore

        target = pointer.parts[-1]
        if isinstance(parent, MutableSequence):
            if obj is UNDEFINED:
                parent.append(self.value)
            else:
                parent.insert(int(target), self.value)
        elif isinstance(parent, MutableMapping):
            parent[target] = self.value
        else:
            raise JSONPatchError(
                f"unexpected operation on {parent.__class__.__name__!r}"
            )
        return data


class OpRemove(Op):
    """The JSON Patch _remove_ operation."""

    __slots__ = ("path",)

    name = "remove"

    def __init__(self, path: _Target) -> None:
        self.path = path

    def apply(
        self, data: Union[MutableSequence[object], MutableMapping[str, object]]
    ) -> Union[MutableSequence[object], MutableMapping[str, object]]:
        """Apply this patch operation to _data_."""
        # Apply in reverse document order so array indices remain valid as
        # earlier siblings are removed.
        pointers = _resolve_pointers(self.path, data)
        for pointer in reversed(pointers):
            data = self._apply(data, pointer)
        return data

    def _apply(
        self,
        data: Union[MutableSequence[object], MutableMapping[str, object]],
        pointer: JSONPointer,
    ) -> Union[MutableSequence[object], MutableMapping[str, object]]:
        parent, obj = pointer.resolve_parent(data)
        if parent is None:
            raise JSONPatchError("can't remove root")

        if isinstance(parent, MutableSequence):
            if obj is UNDEFINED:
                raise JSONPatchError("can't remove nonexistent item")
            del parent[int(pointer.parts[-1])]
        elif isinstance(parent, MutableMapping):
            if obj is UNDEFINED:
                raise JSONPatchError("can't remove nonexistent property")
            del parent[str(pointer.parts[-1])]
        else:
            raise JSONPatchError(
                f"unexpected operation on {parent.__class__.__name__!r}"
            )
        return data

    def asdict(self) -> Dict[str, object]:
        """Return a dictionary representation of this operation."""
        return {"op": self.name, "path": str(self.path)}


class OpReplace(Op):
    """The JSON Patch _replace_ operation."""

    __slots__ = ("path", "value")

    name = "replace"

    def __init__(self, path: _Target, value: object) -> None:
        self.path = path
        self.value = value

    def apply(
        self, data: Union[MutableSequence[object], MutableMapping[str, object]]
    ) -> Union[MutableSequence[object], MutableMapping[str, object]]:
        """Apply this patch operation to _data_."""
        for pointer in _resolve_pointers(self.path, data):
            data = self._apply(data, pointer)
        return data

    def _apply(
        self,
        data: Union[MutableSequence[object], MutableMapping[str, object]],
        pointer: JSONPointer,
    ) -> Union[MutableSequence[object], MutableMapping[str, object]]:
        parent, obj = pointer.resolve_parent(data)
        if parent is None:
            return self.value  # type: ignore

        if isinstance(parent, MutableSequence):
            if obj is UNDEFINED:
                raise JSONPatchError("can't replace nonexistent item")
            parent[int(pointer.parts[-1])] = self.value
        elif isinstance(parent, MutableMapping):
            if obj is UNDEFINED:
                raise JSONPatchError("can't replace nonexistent property")
            parent[str(pointer.parts[-1])] = self.value
        else:
            raise JSONPatchError(
                f"unexpected operation on {parent.__class__.__name__!r}"
            )
        return data

    def asdict(self) -> Dict[str, object]:
        """Return a dictionary representation of this operation."""
        return {"op": self.name, "path": str(self.path), "value": self.value}


class OpMove(Op):
    """The JSON Patch _move_ operation."""

    __slots__ = ("source", "dest")

    name = "move"

    def __init__(self, from_: _Target, path: _Target) -> None:
        self.source = from_
        self.dest = path

    def apply(
        self, data: Union[MutableSequence[object], MutableMapping[str, object]]
    ) -> Union[MutableSequence[object], MutableMapping[str, object]]:
        """Apply this patch operation to _data_."""
        source = _single_pointer(self.source, data, self.name)
        dest = _single_pointer(self.dest, data, self.name)

        if dest.is_relative_to(source):
            raise JSONPatchError("can't move object to one of its own children")

        source_parent, source_obj = source.resolve_parent(data)

        if source_obj is UNDEFINED:
            raise JSONPatchError("source object does not exist")

        if isinstance(source_parent, MutableSequence):
            del source_parent[int(source.parts[-1])]
        if isinstance(source_parent, MutableMapping):
            del source_parent[str(source.parts[-1])]

        dest_parent, _ = dest.resolve_parent(data)

        if dest_parent is None:
            # Move source to root
            return source_obj  # type: ignore

        if isinstance(dest_parent, MutableSequence):
            dest_parent.insert(int(dest.parts[-1]), source_obj)
        elif isinstance(dest_parent, MutableMapping):
            dest_parent[str(dest.parts[-1])] = source_obj
        else:
            raise JSONPatchError(
                f"unexpected operation on {dest_parent.__class__.__name__!r}"
            )

        return data

    def asdict(self) -> Dict[str, object]:
        """Return a dictionary representation of this operation."""
        return {"op": self.name, "from": str(self.source), "path": str(self.dest)}


class OpCopy(Op):
    """The JSON Patch _copy_ operation."""

    __slots__ = ("source", "dest")

    name = "copy"

    def __init__(self, from_: _Target, path: _Target) -> None:
        self.source = from_
        self.dest = path

    def apply(
        self, data: Union[MutableSequence[object], MutableMapping[str, object]]
    ) -> Union[MutableSequence[object], MutableMapping[str, object]]:
        """Apply this patch operation to _data_."""
        source = _single_pointer(self.source, data, self.name)
        dest = _single_pointer(self.dest, data, self.name)

        source_parent, source_obj = source.resolve_parent(data)

        if source_obj is UNDEFINED:
            raise JSONPatchError("source object does not exist")

        dest_parent, dest_obj = dest.resolve_parent(data)

        if dest_parent is None:
            # Copy source to root
            return copy.deepcopy(source_obj)  # type: ignore

        if isinstance(dest_parent, MutableSequence):
            dest_parent.insert(int(dest.parts[-1]), copy.deepcopy(source_obj))
        elif isinstance(dest_parent, MutableMapping):
            dest_parent[str(dest.parts[-1])] = copy.deepcopy(source_obj)
        else:
            raise JSONPatchError(
                f"unexpected operation on {dest_parent.__class__.__name__!r}"
            )

        return data

    def asdict(self) -> Dict[str, object]:
        """Return a dictionary representation of this operation."""
        return {"op": self.name, "from": str(self.source), "path": str(self.dest)}


class OpTest(Op):
    """The JSON Patch _test_ operation."""

    __slots__ = ("path", "value")

    name = "test"

    def __init__(self, path: _Target, value: object) -> None:
        self.path = path
        self.value = value

    def apply(
        self, data: Union[MutableSequence[object], MutableMapping[str, object]]
    ) -> Union[MutableSequence[object], MutableMapping[str, object]]:
        """Apply this patch operation to _data_."""
        pointers = _resolve_pointers(self.path, data)
        if isinstance(self.path, _JSONPathTarget) and not pointers:
            raise JSONPatchTestFailure
        for pointer in pointers:
            _, obj = pointer.resolve_parent(data)
            if not obj == self.value:
                raise JSONPatchTestFailure
        return data

    def asdict(self) -> Dict[str, object]:
        """Return a dictionary representation of this operation."""
        return {"op": self.name, "path": str(self.path), "value": self.value}


Self = TypeVar("Self", bound="JSONPatch")


class JSONPatch:
    """Modify JSON-like data with JSON Patch.

    RFC 6902 defines operations to manipulate a JSON document. `JSONPatch`
    supports parsing and applying standard JSON Patch formatted operations,
    and provides a Python builder API following the same semantics as RFC 6902.

    In addition to RFC 6901 JSON Pointer strings, the `path` and `from` members
    of patch operations may be JSONPath query strings (any string starting with
    `$`). A JSONPath is evaluated against the current target document each time
    the operation is applied. Operations targeting JSONPath queries are
    performed once for each matching node, in document order (or reverse
    document order for _remove_, so array indices remain valid).

    Arguments:
        ops: A JSON Patch formatted document or equivalent Python objects.
        unicode_escape: If `True`, UTF-16 escape sequences will be decoded
            before parsing JSON pointers.
        uri_decode: If `True`, JSON pointers will be unescaped using _urllib_
            before being parsed.
        env: A `JSONPathEnvironment` used to compile JSONPath query strings
            found in patch operations. Defaults to a default environment.

    Raises:
        JSONPatchError: If _ops_ is given and any of the provided operations
            is malformed.
    """

    def __init__(
        self,
        ops: Union[str, IOBase, Iterable[Mapping[str, object]], None] = None,
        *,
        unicode_escape: bool = True,
        uri_decode: bool = False,
        env: Optional[JSONPathEnvironment] = None,
    ) -> None:
        self.ops: List[Op] = []
        self.unicode_escape = unicode_escape
        self.uri_decode = uri_decode
        self._env = env
        if ops:
            self._load(ops)

    @property
    def env(self) -> JSONPathEnvironment:
        """Return the `JSONPathEnvironment` used to compile JSONPath queries."""
        if self._env is None:
            # Lazy import to avoid a circular import at module load time.
            from jsonpath import DEFAULT_ENV

            self._env = DEFAULT_ENV
        return self._env

    def _load(self, patch: Union[str, IOBase, Iterable[Mapping[str, object]]]) -> None:
        if isinstance(patch, IOBase):
            _patch = json.loads(patch.read())
        elif isinstance(patch, str):
            _patch = json.loads(patch)
        else:
            _patch = patch

        try:
            self._build(_patch)
        except TypeError as err:
            raise JSONPatchError(
                "expected a sequence of patch operations, "
                f"found {_patch.__class__.__name__!r}"
            ) from err

    def _build(self, patch: Iterable[Mapping[str, object]]) -> None:
        for i, operation in enumerate(patch):
            try:
                op = operation["op"]
            except KeyError as err:
                raise JSONPatchError(f"missing 'op' member at op {i}") from err

            if op == "add":
                self.add(
                    path=self._op_target(operation, "path", "add", i),
                    value=self._op_value(operation, "value", "add", i),
                )
            elif op == "addne":
                self.addne(
                    path=self._op_target(operation, "path", "addne", i),
                    value=self._op_value(operation, "value", "addne", i),
                )
            elif op == "addap":
                self.addap(
                    path=self._op_target(operation, "path", "addap", i),
                    value=self._op_value(operation, "value", "addap", i),
                )
            elif op == "remove":
                self.remove(path=self._op_target(operation, "path", "add", i))
            elif op == "replace":
                self.replace(
                    path=self._op_target(operation, "path", "replace", i),
                    value=self._op_value(operation, "value", "replace", i),
                )
            elif op == "move":
                self.move(
                    from_=self._op_target(operation, "from", "move", i),
                    path=self._op_target(operation, "path", "move", i),
                )
            elif op == "copy":
                self.copy(
                    from_=self._op_target(operation, "from", "copy", i),
                    path=self._op_target(operation, "path", "copy", i),
                )
            elif op == "test":
                self.test(
                    path=self._op_target(operation, "path", "test", i),
                    value=self._op_value(operation, "value", "test", i),
                )
            else:
                raise JSONPatchError(
                    "expected 'op' to be one of 'add', 'remove', 'replace', "
                    f"'move', 'copy' or 'test' ({op}:{i})"
                )

    def _op_target(
        self, operation: Mapping[str, object], key: str, op: str, i: int
    ) -> _Target:
        try:
            target = operation[key]
        except KeyError as err:
            raise JSONPatchError(f"missing property {key!r} ({op}:{i})") from err

        if not isinstance(target, str):
            raise JSONPatchError(
                f"expected a JSON Pointer or JSONPath string for {key!r}, "
                f"found {target.__class__.__name__!r} "
                f"({op}:{i})"
            )

        if _looks_like_jsonpath(target):
            try:
                compiled = self.env.compile(target)
            except JSONPathError as err:
                raise JSONPatchError(f"{err} ({op}:{i})") from err
            return _JSONPathTarget(target, compiled)

        try:
            return JSONPointer(
                target, unicode_escape=self.unicode_escape, uri_decode=self.uri_decode
            )
        except JSONPointerError as err:
            raise JSONPatchError(f"{err} ({op}:{i})") from err

    def _op_value(
        self, operation: Mapping[str, object], key: str, op: str, i: int
    ) -> object:
        try:
            return operation[key]
        except KeyError as err:
            raise JSONPatchError(f"missing property {key!r} ({op}:{i})") from err

    def _ensure_target(
        self,
        path: Union[str, JSONPointer, JSONPath, CompoundJSONPath, _JSONPathTarget],
    ) -> _Target:
        if isinstance(path, JSONPointer):
            return path
        if isinstance(path, _JSONPathTarget):
            return path
        if isinstance(path, (JSONPath, CompoundJSONPath)):
            return _JSONPathTarget(str(path), path)
        if isinstance(path, str):
            if _looks_like_jsonpath(path):
                return _JSONPathTarget(path, self.env.compile(path))
            return JSONPointer(
                path,
                unicode_escape=self.unicode_escape,
                uri_decode=self.uri_decode,
            )
        raise TypeError(
            f"expected a JSON Pointer or JSONPath, found {path.__class__.__name__!r}"
        )

    def add(
        self: Self,
        path: Union[str, JSONPointer, JSONPath, CompoundJSONPath],
        value: object,
    ) -> Self:
        """Append an _add_ operation to this patch.

        Arguments:
            path: A string representation of a JSON Pointer or JSONPath, a
                parsed `JSONPointer`, or a compiled `JSONPath` /
                `CompoundJSONPath`.
            value: The object to add.

        Returns:
            This `JSONPatch` instance, so we can build a JSON Patch by chaining
                calls to JSON Patch operation methods.
        """
        target = self._ensure_target(path)
        self.ops.append(OpAdd(path=target, value=value))
        return self

    def addne(
        self: Self,
        path: Union[str, JSONPointer, JSONPath, CompoundJSONPath],
        value: object,
    ) -> Self:
        """Append an _addne_ operation to this patch.

        Arguments:
            path: A string representation of a JSON Pointer or JSONPath, a
                parsed `JSONPointer`, or a compiled `JSONPath` /
                `CompoundJSONPath`.
            value: The object to add.

        Returns:
            This `JSONPatch` instance, so we can build a JSON Patch by chaining
                calls to JSON Patch operation methods.
        """
        target = self._ensure_target(path)
        self.ops.append(OpAddNe(path=target, value=value))
        return self

    def addap(
        self: Self,
        path: Union[str, JSONPointer, JSONPath, CompoundJSONPath],
        value: object,
    ) -> Self:
        """Append an _addap_ operation to this patch.

        Arguments:
            path: A string representation of a JSON Pointer or JSONPath, a
                parsed `JSONPointer`, or a compiled `JSONPath` /
                `CompoundJSONPath`.
            value: The object to add.

        Returns:
            This `JSONPatch` instance, so we can build a JSON Patch by chaining
                calls to JSON Patch operation methods.
        """
        target = self._ensure_target(path)
        self.ops.append(OpAddAp(path=target, value=value))
        return self

    def remove(
        self: Self,
        path: Union[str, JSONPointer, JSONPath, CompoundJSONPath],
    ) -> Self:
        """Append a _remove_ operation to this patch.

        Arguments:
            path: A string representation of a JSON Pointer or JSONPath, a
                parsed `JSONPointer`, or a compiled `JSONPath` /
                `CompoundJSONPath`.

        Returns:
            This `JSONPatch` instance, so we can build a JSON Patch by chaining
                calls to JSON Patch operation methods.
        """
        target = self._ensure_target(path)
        self.ops.append(OpRemove(path=target))
        return self

    def replace(
        self: Self,
        path: Union[str, JSONPointer, JSONPath, CompoundJSONPath],
        value: object,
    ) -> Self:
        """Append a _replace_ operation to this patch.

        Arguments:
            path: A string representation of a JSON Pointer or JSONPath, a
                parsed `JSONPointer`, or a compiled `JSONPath` /
                `CompoundJSONPath`.
            value: The object to add.

        Returns:
            This `JSONPatch` instance, so we can build a JSON Patch by chaining
                calls to JSON Patch operation methods.
        """
        target = self._ensure_target(path)
        self.ops.append(OpReplace(path=target, value=value))
        return self

    def move(
        self: Self,
        from_: Union[str, JSONPointer, JSONPath, CompoundJSONPath],
        path: Union[str, JSONPointer, JSONPath, CompoundJSONPath],
    ) -> Self:
        """Append a _move_ operation to this patch.

        When _from_ or _path_ is a JSONPath query, it must resolve to exactly
        one node at apply time.

        Arguments:
            from_: A string representation of a JSON Pointer or JSONPath, a
                parsed `JSONPointer`, or a compiled `JSONPath` /
                `CompoundJSONPath`.
            path: A string representation of a JSON Pointer or JSONPath, a
                parsed `JSONPointer`, or a compiled `JSONPath` /
                `CompoundJSONPath`.

        Returns:
            This `JSONPatch` instance, so we can build a JSON Patch by chaining
                calls to JSON Patch operation methods.
        """
        source_target = self._ensure_target(from_)
        dest_target = self._ensure_target(path)
        self.ops.append(OpMove(from_=source_target, path=dest_target))
        return self

    def copy(
        self: Self,
        from_: Union[str, JSONPointer, JSONPath, CompoundJSONPath],
        path: Union[str, JSONPointer, JSONPath, CompoundJSONPath],
    ) -> Self:
        """Append a _copy_ operation to this patch.

        When _from_ or _path_ is a JSONPath query, it must resolve to exactly
        one node at apply time.

        Arguments:
            from_: A string representation of a JSON Pointer or JSONPath, a
                parsed `JSONPointer`, or a compiled `JSONPath` /
                `CompoundJSONPath`.
            path: A string representation of a JSON Pointer or JSONPath, a
                parsed `JSONPointer`, or a compiled `JSONPath` /
                `CompoundJSONPath`.

        Returns:
            This `JSONPatch` instance, so we can build a JSON Patch by chaining
                calls to JSON Patch operation methods.
        """
        source_target = self._ensure_target(from_)
        dest_target = self._ensure_target(path)
        self.ops.append(OpCopy(from_=source_target, path=dest_target))
        return self

    def test(
        self: Self,
        path: Union[str, JSONPointer, JSONPath, CompoundJSONPath],
        value: object,
    ) -> Self:
        """Append a test operation to this patch.

        Arguments:
            path: A string representation of a JSON Pointer or JSONPath, a
                parsed `JSONPointer`, or a compiled `JSONPath` /
                `CompoundJSONPath`.
            value: The object to test.

        Returns:
            This `JSONPatch` instance, so we can build a JSON Patch by chaining
                calls to JSON Patch operation methods.
        """
        target = self._ensure_target(path)
        self.ops.append(OpTest(path=target, value=value))
        return self

    def apply(
        self,
        data: Union[str, IOBase, MutableSequence[Any], MutableMapping[str, Any]],
    ) -> object:
        """Apply all operations from this patch to _data_.

        If _data_ is a string or file-like object, it will be loaded with
        _json.loads_. Otherwise _data_ should be a JSON-like data structure and
        will be modified in place.

        When modifying _data_ in place, we return modified data too. This is
        to allow for replacing _data's_ root element, which is allowed by some
        patch operations.

        Arguments:
            data: The target JSON "document" or equivalent Python objects.

        Returns:
            Modified input data.

        Raises:
            JSONPatchError: When a patch operation fails.
            JSONPatchTestFailure: When a _test_ operation does not pass.
                `JSONPatchTestFailure` is a subclass of `JSONPatchError`.
        """
        _data = load_data(data)

        for i, op in enumerate(self.ops):
            try:
                _data = op.apply(_data)
            except JSONPatchTestFailure as err:
                raise JSONPatchTestFailure(f"test failed ({op.name}:{i})") from err
            except JSONPointerKeyError as err:
                raise JSONPatchError(f"{err} ({op.name}:{i})") from err
            except JSONPointerIndexError as err:
                raise JSONPatchError(f"{err} ({op.name}:{i})") from err
            except JSONPointerTypeError as err:
                raise JSONPatchError(f"{err} ({op.name}:{i})") from err
            except (JSONPointerError, JSONPatchError) as err:
                raise JSONPatchError(f"{err} ({op.name}:{i})") from err
            except JSONPathError as err:
                raise JSONPatchError(f"{err} ({op.name}:{i})") from err

        return _data

    def asdicts(self) -> List[Dict[str, object]]:
        """Return a list of this patch's operations as dictionaries."""
        return [op.asdict() for op in self.ops]


def _looks_like_jsonpath(s: str) -> bool:
    """Return `True` if _s_ is a JSONPath query string.

    The empty string and any string starting with `/` are valid JSON Pointers.
    JSONPath queries always start with the root identifier `$`.
    """
    return s.startswith("$")


def apply(
    patch: Union[str, IOBase, Iterable[Mapping[str, object]], None],
    data: Union[str, IOBase, MutableSequence[Any], MutableMapping[str, Any]],
    *,
    unicode_escape: bool = True,
    uri_decode: bool = False,
    env: Optional[JSONPathEnvironment] = None,
) -> object:
    """Apply the JSON Patch _patch_ to _data_.

    If _data_ is a string or file-like object, it will be loaded with
    _json.loads_. Otherwise _data_ should be a JSON-like data structure and
    will be **modified in-place**.

    When modifying _data_ in-place, we return modified data too. This is
    to allow for replacing _data's_ root element, which is allowed by some
    patch operations.

    Arguments:
        patch: A JSON Patch formatted document or equivalent Python objects.
        data: The target JSON "document" or equivalent Python objects.
        unicode_escape: If `True`, UTF-16 escape sequences will be decoded
            before parsing JSON pointers.
        uri_decode: If `True`, JSON pointers will be unescaped using _urllib_
            before being parsed.
        env: A `JSONPathEnvironment` used to compile JSONPath query strings
            found in patch operations. Defaults to a default environment.

    Returns:
        Modified input data.

    Raises:
        JSONPatchError: When a patch operation fails.
        JSONPatchTestFailure: When a _test_ operation does not pass.
            `JSONPatchTestFailure` is a subclass of `JSONPatchError`.

    """
    return JSONPatch(
        patch,
        unicode_escape=unicode_escape,
        uri_decode=uri_decode,
        env=env,
    ).apply(data)
