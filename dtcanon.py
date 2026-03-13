#!/usr/bin/env python3
# pylint: disable=redefined-outer-name, too-many-branches
# ruff: noqa: E741

import abc
import os
import subprocess
import sys
import traceback
from collections.abc import ItemsView, Iterable, KeysView, ValuesView
from dataclasses import dataclass, field
from typing import IO, Any, Literal


class Reader:
    # pylint: disable=too-few-public-methods
    def __init__(self, f: IO[bytes]):
        self.f = f
        self.next = f.read(1)
        self.next2 = f.read(1)
        self.line = 1
        self.column = 1
        self.filename = b"unknown"
        self.filepath = b"unknown"

    def consume(self) -> None:
        self.column += 1
        if self.next == b"\n":
            self.line += 1
            self.column = 1
        self.next = self.next2
        self.next2 = self.f.read(1)


@dataclass
class Node:
    parent: "Node | None" = field(repr=False)
    props: dict[bytes, bytes | Literal[True]] = field(default_factory=dict)
    children: dict[bytes, "Node"] = field(default_factory=dict, repr=False)
    labels: set[bytes] = field(default_factory=set)
    flags: set[bytes] = field(default_factory=set)
    location: tuple[bytes, int, int] | None = None

    def name(self) -> bytes:
        if not self.parent:
            return b"/"
        for k in self.parent.children:
            if self.parent.children[k] is self:
                return k
        assert False

    def path(self) -> bytes:
        if self.parent:
            parent_path = self.parent.path()
            if parent_path == b"/":
                return b"/" + self.name()
            return self.parent.path() + b"/" + self.name()
        return b"/"

    def __getitem__(self, prop: bytes) -> bytes:
        value = self.props[prop]
        assert isinstance(value, bytes)
        return value

    def __truediv__(self, path: bytes) -> "Node":
        path = os.path.normpath(path)
        key, _, rest = path.partition(b"/")
        child = self.children[key]
        if rest:
            return child / rest
        return child

    def _repr_pretty_(self, p: Any, cycle: bool) -> None:
        # pylint: disable-next=import-outside-toplevel, import-error
        from IPython.lib.pretty import CallExpression, RawText

        ctor = CallExpression.factory(self.__class__.__name__)
        if cycle:
            p.pretty(ctor(RawText("...")))
        else:
            kwargs: dict[str, Any] = {}
            if self.parent:
                kwargs["parent"] = self.parent.name()
            if self.props:
                kwargs["props"] = self.props
            if self.children:
                kwargs["children"] = list(self.children.keys())
            if self.labels:
                kwargs["labels"] = self.labels
            if self.flags:
                kwargs["flags"] = self.flags
            p.pretty(ctor(**kwargs))


@dataclass
class PHandle:
    handle: bytes
    node: Node | None = field(repr=False)
    label: bytes = b""
    references: list[tuple[Node, bytes]] = field(default_factory=list)

    def get_ref(self) -> bytes:
        if self.label:
            return b"&" + self.label
        if self.node:
            return b"&{" + self.node.path() + b"}"
        return self.handle

    def _repr_pretty_(self, p: Any, cycle: bool) -> None:
        # pylint: disable-next=import-outside-toplevel, import-error
        from IPython.lib.pretty import CallExpression, RawText

        ctor = CallExpression.factory(self.__class__.__name__)
        if cycle:
            p.pretty(ctor(RawText("...")))
        else:
            kwargs: dict[str, Any] = {
                "handle": self.handle,
            }
            if self.node:
                kwargs["path"] = self.node.path()
            else:
                kwargs["path"] = RawText("{fake node}")
            if self.label:
                kwargs["label"] = self.label
            kwargs["count"] = len(self.references)
            p.pretty(ctor(**kwargs))


@dataclass
class PHandleTable:
    _phandles: dict[bytes, PHandle] = field(default_factory=dict, repr=False)

    def add(self, phandle: PHandle) -> None:
        self._phandles[phandle.handle] = phandle

    def __getitem__(self, handle: bytes) -> PHandle:
        return self._phandles[handle]

    def get(self, handle: bytes) -> PHandle | None:
        return self._phandles.get(handle)

    def make_ref(self, key: bytes) -> bytes:
        if key in self._phandles:
            return self._phandles[key].get_ref()
        return key

    def keys(self) -> KeysView[bytes]:
        return self._phandles.keys()

    def values(self) -> ValuesView[PHandle]:
        return self._phandles.values()

    def items(self) -> ItemsView[bytes, PHandle]:
        return self._phandles.items()


class ParseError(Exception):
    def __init__(self, r: Reader, msg: str, node: Node | None = None):
        self.filename = r.filepath
        self.line = r.line
        self.column = r.column
        self.msg = msg
        self.node = node


def skip(r: Reader, ch: bytes) -> None:
    if r.next != ch:
        raise ParseError(r, "Expected " + str(ch) + ", got " + str(r.next))
    r.consume()


def skip_whitespace(r: Reader) -> None:
    next_line = None
    next_file = None
    while True:
        while r.next in b" \n\t" and r.next != b"":
            r.consume()

        if r.next == b"/" and r.next2 == b"/":
            skip_line(r)
        elif r.next == b"/" and r.next2 == b"*":
            r.consume()
            r.consume()
            while True:
                if r.next == b"*" and r.next2 == b"/":
                    break
                if r.next == b"":
                    raise ParseError(r, "Unexpected EOF in comment")
                r.consume()
        elif r.next == b"#" and r.next2 == b" ":
            r.consume()
            r.consume()
            if r.next in b"0123456789":
                digits = ""
                while r.next in b"0123456789":
                    digits += str(r.next, "utf-8")
                    r.consume()
                next_line = int(digits)
            if r.next == b" " and r.next2 == b'"':
                next_file = b""
                r.consume()
                r.consume()
                while r.next != b'"':
                    if r.next == b"\\" and r.next2 == b'"':
                        next_file += b'"'
                        r.consume()
                        r.consume()
                    elif r.next == b"\\" and r.next2 == b"\\":
                        next_file += b"\\"
                        r.consume()
                        r.consume()
                    else:
                        next_file += r.next
                        r.consume()
                r.consume()
                while r.next in b" 0123456789":
                    r.consume()
        else:
            if next_line is not None:
                r.line = next_line
            if next_file is not None:
                r.filename = os.path.basename(next_file)
                r.filepath = next_file
            break


def skip_line(r: Reader) -> None:
    while r.next not in {b"\n", b""}:
        r.consume()
    r.consume()


def read_ident(r: Reader) -> bytes:
    ident = b""
    while True:
        ch = r.next
        if ch in b"{}:;<>()= \n\t" or ch == b"":
            return ident
        ident += ch
        r.consume()


def parse_value(r: Reader) -> bytes:
    # pylint: disable=too-many-statements
    value = b""
    while True:
        skip_whitespace(r)
        if r.next in {b";", b">"}:
            return value

        if r.next == b",":
            r.consume()
            value += b","
            skip_whitespace(r)

        if value != b"":
            value += b" "

        if r.next == b"":
            raise ParseError(r, "Unexpected EOF while parsing value")
        if r.next == b'"':
            value += b'"'
            r.consume()
            while r.next != b'"':
                if r.next == b"\\":
                    value += r.next
                    r.consume()
                    value += r.next
                    r.consume()
                else:
                    value += r.next
                    r.consume()
            value += b'"'
            r.consume()
        elif r.next == b"<":
            value += b"<"
            r.consume()
            value += parse_value(r)
            value += b">"
            skip(r, b">")
        elif r.next == b"(":
            value += b"("
            r.consume()

            depth = 1
            while depth > 0:
                if r.next == b"(":
                    depth += 1
                elif r.next == b")":
                    depth -= 1
                value += r.next
                r.consume()
        elif r.next in b"0123456789":
            leading = r.next
            r.consume()
            if leading == b"0" and r.next == b"x":
                r.consume()
                digits = b""
                while r.next in b"0123456789abcdefABCDEF":
                    digits += r.next
                    r.consume()
                value += bytes(hex(int(digits, 16)), "utf-8")
            else:
                digits = leading
                while r.next in b"0123456789":
                    digits += r.next
                    r.consume()
                value += bytes(str(int(digits)), "utf-8")
        elif r.next == b"[":
            value += b"["
            r.consume()
            first = True
            while True:
                if not first:
                    value += b" "
                first = False

                skip_whitespace(r)
                if r.next == b"]":
                    break

                digits = b""
                if r.next not in b"0123456789abcdefABCDEF":
                    raise ParseError(
                        r, f"Invalid hex character: {r.next.decode('utf-8')}"
                    )
                digits += r.next
                r.consume()
                if r.next not in b"0123456789abcdefABCDEF":
                    raise ParseError(
                        r, f"Invalid hex character: {r.next.decode('utf-8')}"
                    )
                digits += r.next
                r.consume()
                value += bytes(str(digits, "utf-8").lower(), "utf-8")

            value += b"]"
            r.consume()
        elif r.next == b"&":
            value += b"&"
            r.consume()
            value += read_ident(r)
        elif r.next == b"{":
            value += b"{"
            skip_whitespace(r)
            value += read_ident(r)
            skip_whitespace(r)
            value += b"}"
            skip(r, b"}")
        else:
            ident = read_ident(r)
            if ident == b"":
                raise ParseError(r, "Unknown lead character in value: " + str(r.next))
            value += ident
            if r.next == b":":
                value += b":"
                r.consume()
    return value


def parse_node(
    r: Reader,
    node: Node,
    labels: dict[bytes, Node],
    pending_label_nodes: dict[bytes, Node],
) -> None:
    # pylint: disable=too-many-statements
    skip(r, b"{")

    while True:
        skip_whitespace(r)
        if r.next == b"}":
            r.consume()
            skip_whitespace(r)
            skip(r, b";")
            return

        flags = set()
        name = read_ident(r)
        while len(name) > 2 and name[0] == ord("/") and name[-1] == ord("/"):
            flags.add(name)
            skip_whitespace(r)
            name = read_ident(r)
        skip_whitespace(r)

        if r.next == b"=":
            r.consume()
            skip_whitespace(r)
            try:
                node.props[name] = parse_value(r)
            except ParseError as ex:
                ex.node = node
                raise ex
            skip_whitespace(r)
            skip(r, b";")
            skip_whitespace(r)
        elif r.next == b";":
            r.consume()
            skip_whitespace(r)
            node.props[name] = True
        elif r.next in {b":", b"{"}:
            child_labels = set()
            while r.next == b":":
                r.consume()
                child_labels.add(name)
                skip_whitespace(r)
                name = read_ident(r)
                skip_whitespace(r)

            if name in node.children:
                child = node.children[name]
            else:
                child = None
                for label in child_labels:
                    if label not in pending_label_nodes:
                        continue
                    child = pending_label_nodes[label]
                    child.location = (r.filename, r.line, r.column)
                    child.parent = node
                    del pending_label_nodes[label]
                    break

            if child is None:
                child = Node(node)
                child.location = (r.filename, r.line, r.column)
                node.children[name] = child

            child.flags = flags

            for label in child_labels:
                child.labels.add(label)
                labels[label] = child

            parse_node(r, child, labels, pending_label_nodes)
        else:
            raise ParseError(
                r, "Expected value or child node, got " + str(r.next), node
            )


def parse_document(r: Reader) -> tuple[dict[bytes, Node], dict[bytes, Node]]:
    labels: dict[bytes, Node] = {}
    roots: dict[bytes, Node] = {}
    pending_label_nodes: dict[bytes, Node] = {}
    while True:
        skip_whitespace(r)
        if r.next == b"":
            break
        if r.next == b"&":
            r.consume()
            name = read_ident(r)
            assert name != b""
            skip_whitespace(r)
            if name in labels:
                parse_node(r, labels[name], labels, pending_label_nodes)
            elif name in pending_label_nodes:
                parse_node(r, pending_label_nodes[name], labels, pending_label_nodes)
            else:
                node = Node(None)
                pending_label_nodes[name] = node
                parse_node(r, node, labels, pending_label_nodes)
        else:
            name = read_ident(r)
            if name == b"/dts-v1/":
                skip_whitespace(r)
                skip(r, b";")
                continue
            if name == b"/delete-node/":
                skip_whitespace(r)
                skip(r, b"&")
                delete_label = read_ident(r)
                skip(r, b";")

                if delete_label not in labels:
                    raise ParseError(
                        r,
                        "Got /delete-label/ for non-existent node '"
                        + delete_label.decode("utf-8")
                        + "'",
                    )

                delete_node = labels[delete_label]
                assert delete_node.parent is not None
                for k in delete_node.parent.children:
                    if delete_node.parent.children[k] is delete_node:
                        del delete_node.parent.children[k]
                        break
                continue

            if name in roots:
                node = roots[name]
            else:
                node = Node(None)
                node.location = (r.filename, r.line, r.column)
                roots[name] = node
            skip_whitespace(r)
            parse_node(r, node, labels, pending_label_nodes)

    if len(pending_label_nodes) > 0:
        print(
            "Warning: Undefined labels: "
            + str(b", ".join(pending_label_nodes.keys()), "utf-8")
        )

    return roots, labels


def print_flat_node(
    node: Node, name: bytes, outfile: IO[str], tag_locations: bool
) -> None:
    if tag_locations:
        assert node.location is not None
        f, l, c = node.location
        print(f"// {str(f, 'utf-8')}:{l}:{c}", file=outfile)

    prefix = ""

    for flag in sorted(node.flags):
        print(str(flag, "utf-8"), file=outfile)

    for label in sorted(node.labels):
        prefix += str(label, "utf-8") + ": "

    if name == b"":
        prefix += "/ "
    else:
        prefix += str(name, "utf-8") + " "

    print(prefix + "{", file=outfile)

    for k in sorted(node.props.keys()):
        prop = node.props[k]
        if prop is True:
            print("  " + str(k, "utf-8") + ";", file=outfile)
        else:
            print(
                "  " + str(k, "utf-8") + " = " + str(prop, "utf-8") + ";", file=outfile
            )
    print("};", file=outfile)

    for k in sorted(node.children.keys()):
        print_flat_node(node.children[k], name + b"/" + k, outfile, tag_locations)


def print_flat_document(
    roots: dict[bytes, Node], outfile: IO[str], tag_locations: bool
) -> None:
    for k in sorted(roots.keys()):
        for flag in sorted(roots[k].flags):
            print(str(flag, "utf-8"), file=outfile)
        if k == b"/":
            print_flat_node(roots[k], b"", outfile, tag_locations)
        else:
            print_flat_node(roots[k], k, outfile, tag_locations)


def print_dts_node(
    node: Node, depth: int, outfile: IO[str], tag_locations: bool
) -> None:
    for k in sorted(node.props.keys()):
        prop = node.props[k]
        if prop is True:
            print("  " * depth + str(k, "utf-8") + ";", file=outfile)
        else:
            print(
                "  " * depth + str(k, "utf-8") + " = " + str(prop, "utf-8") + ";",
                file=outfile,
            )

    for k in sorted(node.children.keys()):
        child = node.children[k]
        if tag_locations:
            assert child.location is not None
            f, l, c = child.location
            print("  " * depth + f"// {str(f, 'utf-8')}:{l}:{c}", file=outfile)

        for flag in sorted(child.flags):
            print("  " * depth + str(flag, "utf-8"), file=outfile)

        prefix = ""
        for label in sorted(child.labels):
            prefix += str(label, "utf-8") + ": "

        print("  " * depth + prefix + str(k, "utf-8") + " {", file=outfile)
        print_dts_node(child, depth + 1, outfile, tag_locations)
        print("  " * depth + "};", file=outfile)


def print_dts_document(
    roots: dict[bytes, Node], outfile: IO[str], tag_locations: bool
) -> None:
    print("/dts-v1/;", file=outfile)
    for k in sorted(roots.keys()):
        for flag in sorted(roots[k].flags):
            print(str(flag, "utf-8"))
        print(str(k, "utf-8") + " {", file=outfile)
        print_dts_node(roots[k], 1, outfile, tag_locations)
        print("};", file=outfile)


def print_labels(labels: dict[bytes, Node], outfile: IO[str]) -> None:
    for k in sorted(labels.keys()):
        print(str(k, "utf-8") + " = " + str(labels[k].path(), "utf-8"), file=outfile)


def symbolize_nodes(
    nodes: dict[bytes, Node],
    symbols: dict[bytes, bytes],
    phandles: PHandleTable,
    path: bytes | None = None,
) -> None:
    for name in nodes:
        node = nodes[name]
        if path is None:
            subpath = name
        elif path == b"/":
            subpath = path + name
        else:
            subpath = path + b"/" + name

        if b"phandle" in node.props:
            assert isinstance(node.props[b"phandle"], bytes)
            handle = node.props[b"phandle"][1:-1]
            phandle = PHandle(handle=handle, node=node)
            if subpath in symbols:
                phandle.label = symbols[subpath]
                node.labels.add(phandle.label)
            phandles.add(phandle)

        symbolize_nodes(node.children, symbols, phandles, subpath)


@dataclass
class PropStruct(abc.ABC):
    @abc.abstractmethod
    def parse(
        self, parts: list[bytes], offset: int, node: Node, phandles: PHandleTable
    ) -> int:
        """Transform parts in-place, starting at offset. Return the new offset."""


@dataclass
class intStruct(PropStruct):
    size: int

    def parse(
        self, parts: list[bytes], offset: int, node: Node, phandles: PHandleTable
    ) -> int:
        del parts, node, phandles
        return offset + self.size


@dataclass
class phandleStruct(PropStruct):
    size: int
    phandles: frozenset[int]

    def __init__(self, size: int, *args: int, phandles: Iterable[int] = ()):
        super().__init__()
        self.size = size
        if phandles:
            assert not args
            self.phandles = frozenset(phandles)
        else:
            self.phandles = frozenset(args)

    def parse(
        self, parts: list[bytes], offset: int, node: Node, phandles: PHandleTable
    ) -> int:
        del node
        for idx in self.phandles:
            i = offset + idx
            parts[i] = phandles.make_ref(parts[i])
        return offset + self.size


@dataclass
class clientStruct(PropStruct):
    name: bytes

    def parse(
        self, parts: list[bytes], offset: int, node: Node, phandles: PHandleTable
    ) -> int:
        controller = phandles[parts[offset]]
        parts[offset] = controller.get_ref()
        offset += 1
        assert controller.node is not None
        cells = controller.node.props[b"#" + self.name + b"-cells"]
        assert isinstance(cells, bytes)
        return offset + int(cells[1:-1], 0)


@dataclass
class clockParentsStruct(PropStruct):
    def parse(
        self, parts: list[bytes], offset: int, node: Node, phandles: PHandleTable
    ) -> int:
        if int(parts[offset], 0) == 0:
            return offset + 1
        controller = phandles[parts[offset]]
        parts[offset] = controller.get_ref()
        offset += 1
        assert controller.node is not None
        cells = controller.node.props[b"#clock-cells"]
        assert isinstance(cells, bytes)
        return offset + int(cells[1:-1], 0)


@dataclass
class PropSpec:
    structs: list[PropStruct]
    repeat_last: bool

    def __init__(self, *structs: PropStruct, repeat_last: bool = False):
        self.structs = list(structs)
        self.repeat_last = repeat_last


def makeNexusProp(
    specifier_name: bytes, prop_name: bytes = b""
) -> dict[bytes, PropSpec]:
    if not prop_name:
        prop_name = specifier_name + b"s"
    return {prop_name: PropSpec(clientStruct(specifier_name), repeat_last=True)}


def cleanup_prop(
    key: bytes,
    parts: list[bytes],
    node: Node,
    phandles: PHandleTable,
) -> list[bytes] | None:
    # pylint: disable=too-many-locals
    SINGLE_PHANDLE = PropSpec(phandleStruct(1, 0), repeat_last=True)
    FIRST_PHANDLE = PropSpec(phandleStruct(1, 0), intStruct(1), repeat_last=True)
    ALL_PHANDLES = PropSpec(phandleStruct(1, 0), repeat_last=True)
    DEFAULT_SPEC = PropSpec(intStruct(1), repeat_last=True)
    gpio_spec = PropSpec(clientStruct(b"gpio"), repeat_last=True)
    clock_spec = PropSpec(clientStruct(b"clock"), repeat_last=True)
    interrupt_spec = PropSpec(clientStruct(b"interrupt"), repeat_last=True)
    prop_specs: dict[bytes, PropSpec] = {
        ## single phandle
        # generic properties
        b"avdd-usb-supply": SINGLE_PHANDLE,
        b"cpu": SINGLE_PHANDLE,
        b"cpu-idle-states": SINGLE_PHANDLE,
        b"i2c-parent": SINGLE_PHANDLE,
        b"interrupt-affinity": SINGLE_PHANDLE,
        b"interrupt-parent": SINGLE_PHANDLE,
        b"next-level-cache": SINGLE_PHANDLE,
        b"operating-points-v2": SINGLE_PHANDLE,
        b"pinctrl-0": SINGLE_PHANDLE,
        b"pinctrl-1": SINGLE_PHANDLE,
        b"remote-endpoint": SINGLE_PHANDLE,
        b"trip": SINGLE_PHANDLE,
        b"vbus-supply": SINGLE_PHANDLE,
        b"vcc-supply": SINGLE_PHANDLE,
        b"vclamp-usb-supply": SINGLE_PHANDLE,
        b"vddio-pex-ctl-supply": SINGLE_PHANDLE,
        b"vin-supply": SINGLE_PHANDLE,
        b"vmmc-supply": SINGLE_PHANDLE,
        b"vpcie3v3-supply": SINGLE_PHANDLE,
        b"imu": SINGLE_PHANDLE,
        # vendor-specific properties
        b"nvidia,gpio-controller": SINGLE_PHANDLE,
        b"nvidia,memory-controller": SINGLE_PHANDLE,
        b"nvidia,host1x": SINGLE_PHANDLE,
        b"nvidia,isp-falcon-device": SINGLE_PHANDLE,
        b"nvidia,vi-falcon-device": SINGLE_PHANDLE,
        b"nvidia,vm-irq-config": SINGLE_PHANDLE,
        b"nvidia,xusb-padctl": SINGLE_PHANDLE,
        b"snps,axi-config": SINGLE_PHANDLE,
        ## first value is a phandle, rest are integers
        # generic properties
        b"gpio-ranges": FIRST_PHANDLE,
        b"memory-region": FIRST_PHANDLE,
        b"trigger-gpios": FIRST_PHANDLE,
        b"vbus-gpios": FIRST_PHANDLE,
        # vendor-specific properties
        b"nvidia,bpmp": FIRST_PHANDLE,
        b"nvidia,ivc-channels": FIRST_PHANDLE,
        b"nvidia,trace": FIRST_PHANDLE,
        ## all values are phandles
        # generic properties
        b"cpus": ALL_PHANDLES,
        b"dais": ALL_PHANDLES,
        b"shmem": ALL_PHANDLES,
        b"camera-sensors": ALL_PHANDLES,
        b"camera-serializers": ALL_PHANDLES,
        # vendor-specific properties
        b"nvidia,mem-map": ALL_PHANDLES,
        b"nvidia,vi-devices": ALL_PHANDLES,
        ## complex structures
        # gpios
        b"cd-gpios": gpio_spec,
        b"gpio": gpio_spec,
        b"gpio_pinmux": gpio_spec,
        b"mux-gpios": gpio_spec,
        b"nvidia,refclk-select-gpios": gpio_spec,
        b"nvidia,pex-wake-gpios": gpio_spec,
        b"os_gpio_hotplug_a": gpio_spec,
        b"pwdn-gpio": gpio_spec,
        b"reset-gpios": gpio_spec,
        b"vbus-gpio": gpio_spec,
        # clocks
        b"clocks": clock_spec,
        b"assigned-clocks": clock_spec,
        b"assigned-clock-parents": PropSpec(clockParentsStruct(), repeat_last=True),
        b"iommu-map": PropSpec(phandleStruct(4, 1), repeat_last=True),
        b"iommu-addresses": PropSpec(phandleStruct(1 + 2 + 2, 0), repeat_last=True),
        b"interrupts-extended": interrupt_spec,
        **makeNexusProp(b"cooling", b"cooling-device"),
        **makeNexusProp(b"dma"),
        **makeNexusProp(b"interconnect"),
        **makeNexusProp(b"iommu"),
        **makeNexusProp(b"mbox", b"mboxes"),
        **makeNexusProp(b"phy"),
        **makeNexusProp(b"power-domain"),
        **makeNexusProp(b"pwm"),
        **makeNexusProp(b"reset"),
        **makeNexusProp(b"sound-dai", b"sound-dai"),
        **makeNexusProp(b"thermal-sensor"),
    }
    spec = prop_specs.get(key, DEFAULT_SPEC)
    if key == b"gpios" and b"gpio-hog" not in node.props:
        spec = gpio_spec
    # special handling for mboxes
    offset = 0
    spec_idx = 0
    while offset < len(parts):
        structure = spec.structs[spec_idx]
        offset = structure.parse(parts, offset, node, phandles)
        spec_idx += 1
        if spec_idx >= len(spec.structs):
            if spec.repeat_last:
                spec_idx -= 1
            else:
                break
    if offset > len(parts):
        value = node.props[key]
        assert isinstance(value, bytes)
        msg = f"incomplete structure at {os.path.join(node.path(), key).decode()}: {value.decode()}"
        raise ValueError(msg)
    return parts


def cleanup_nodes(
    nodes: dict[bytes, Node], phandles: PHandleTable, path: bytes | None = None
) -> None:
    for name in nodes:
        node = nodes[name]
        if path is None:
            subpath = name
        elif path == b"/":
            subpath = path + name
        else:
            subpath = path + b"/" + name

        if path == b"/" and name in {
            b"__symbols__",
            b"__fixups__",
            b"__local_fixups__",
        }:
            # skip special entries at root
            continue

        for key in node.props:
            value = node.props[key]
            if (
                not (
                    isinstance(value, bytes)
                    and value.startswith(b"<")
                    and value.endswith(b">")
                )
                or key == b"phandle"
            ):
                continue

            skip = False
            parts = value[1:-1].split()
            cleaned_parts = cleanup_prop(key, list(parts), node, phandles)
            if cleaned_parts is not None:
                # if cleanup_prop modified the values, then don't check them
                # for phandle values
                skip = parts != cleaned_parts
                parts = cleaned_parts
                node.props[key] = b"<" + b" ".join(parts) + b">"

            if key in {
                # generic properties
                b"address-width",
                b"alloc-ranges",
                b"bitrates",
                b"bittimes",
                b"bus-range",
                b"bus-width",
                b"cache-level",
                b"cache-line-size",
                b"cache-sets",
                b"cache-size",
                b"capture-window-length",
                b"cooling-levels",
                b"d-cache-line-size",
                b"d-cache-sets",
                b"d-cache-size",
                b"dma-ranges",
                b"duty_cycle",
                b"freq_hz",
                b"i-cache-line-size",
                b"i-cache-sets",
                b"i-cache-size",
                b"interrupt-map",
                b"interrupts",
                b"link-type",
                b"mram-params",
                b"num-ib-windows",
                b"num-lanes",
                b"num-ob-windows",
                b"num-viewport",
                b"pagesize",
                b"port-index",
                b"power-source",
                b"prod",
                b"pulse-per-rev",
                b"ranges",
                b"reg",
                b"rx-config",
                b"size",
                b"timeout-sec",
                b"tx-config",
                b"wakeup-event-action",
                # from zedbox
                b"accel_i2c_addr",
                b"def-addr",
                b"eeprom-addr",
                b"idle-state",
                b"isp_bw_margin_pct",
                b"isp_peak_byte_per_pixel",
                b"min_bits_per_pixel",
                b"num-channels",
                b"num_csi_lanes",
                b"nvidia,ahub-i2s-id",
                b"nvidia,boot-detect-delay",
                b"nvidia,bpmp-bus-id",
                b"nvidia,dma_rx_ring_sz",
                b"nvidia,dma_tx_ring_sz",
                b"nvidia,pad-autocal-pull-down-offset-1v8-timeout",
                b"nvidia,pad-autocal-pull-down-offset-3v3-timeout",
                b"nvidia,pad-autocal-pull-up-offset-1v8-timeout",
                b"nvidia,pad-autocal-pull-up-offset-3v3-timeout",
                b"nvidia,usb2-companion",
                b"polling-delay",
                b"polling-delay-passive",
                b"spi-rx-bus-width",
                b"spi-tx-bus-width",
                b"sync_sensor_index",
                b"vc-id",
                b"vc_id",
                b"vi_bw_margin_pct",
                b"vi_peak_byte_per_pixel",
                # vendor-specific properties
                b"linux,initrd-end",
                b"linux,initrd-start",
                b"linux,input-type",
                b"linux,pci-domain",
                b"linux,uefi-mmap-desc-size",
                b"linux,uefi-mmap-desc-ver",
                b"linux,uefi-mmap-start",
                b"linux,uefi-secure-boot",
                b"linux,uefi-system-table",
                b"nvidia,aspm-cmrt-us",
                b"nvidia,aspm-l0s-entrance-latency-us",
                b"nvidia,aspm-pwr-on-t-us",
                b"nvidia,dcs-enable",
                b"nvidia,default-tap",
                b"nvidia,default-trim",
                b"nvidia,dma-chans",
                b"nvidia,dqs-trim",
                b"nvidia,enable-input",
                b"nvidia,frame-count",
                b"nvidia,frame-size",
                b"nvidia,group",
                b"nvidia,host1x-class",
                b"nvidia,hw-instance-id",
                b"nvidia,instance_id",
                b"nvidia,int-threshold",
                b"nvidia,interval-ms",
                b"nvidia,io-high-voltage",
                b"nvidia,ivc-carveout-size-ss",
                b"nvidia,ivc-rx-ss",
                b"nvidia,ivc-timeout",
                b"nvidia,ivc-tx-ss",
                b"nvidia,macsec-enable",
                b"nvidia,max-hs-gear",
                b"nvidia,mtl-queues",
                b"nvidia,num-dma-chans",
                b"nvidia,num-dpaux-instance",
                b"nvidia,num-mtl-queues",
                b"nvidia,num-slices",
                b"nvidia,num-vm-channels",
                b"nvidia,num-vm-irqs",
                b"nvidia,pad_calibration",
                b"nvidia,promisc_mode",
                b"nvidia,ptp-rx-queue",
                b"nvidia,residual-queue",
                b"nvidia,rx-clk-tap-delay",
                b"nvidia,rx-queue-prio",
                b"nvidia,rx_frames",
                b"nvidia,rx_riwt",
                b"nvidia,rxq_enable_ctrl",
                b"nvidia,slot_intvl_vals",
                b"nvidia,tc-mapping",
                b"nvidia,timer-index",
                b"nvidia,tristate",
                b"nvidia,tx-queue-prio",
                b"nvidia,tx_frames",
                b"nvidia,tx_usecs",
                b"nvidia,vi-mapping",
                b"nvidia,vi-mapping-size",
                b"nvidia,vi-max-channels",
                b"nvidia,vm-channels",
                b"nvidia,vm-irq-id",
                b"nvidia,vm-irq-num",
                b"nvidia,vm-num",
                b"snps,blen",
                b"snps,rd_osr_lmt",
                b"snps,wr_osr_lmt",
            }:
                skip = True
            elif key.startswith(b"#"):
                skip = True
            elif subpath.startswith(b"/gpio-keys/") and key == b"linux,code":
                skip = True
            elif key == b"gpios" and b"gpio-hog" in node.props:
                skip = True

            if not skip:
                for handle in set(parts) & phandles.keys():
                    phandle = phandles.get(handle)
                    assert phandle is not None
                    phandle.references.append((node, key))

        cleanup_nodes(node.children, phandles, subpath)


def symbolize_document(
    roots: dict[bytes, Node], clear_phandles: bool
) -> tuple[dict[bytes, bytes], PHandleTable]:
    # pylint: disable=too-many-locals
    symbols: dict[bytes, bytes] = {}
    phandles = PHandleTable()
    if b"/" not in roots:
        print("Symbolize: No '/' node in document!", file=sys.stderr)
        return symbols, phandles

    root = roots[b"/"]
    if b"__symbols__" not in root.children:
        print("Symbolize: No symbol table in document!", file=sys.stderr)
        return symbols, phandles
    symbol_table = root.children[b"__symbols__"]

    for label, raw_path in symbol_table.props.items():
        assert isinstance(raw_path, bytes)
        path = raw_path[1:-1]
        symbols[path] = label

    symbolize_nodes(roots, symbols, phandles)
    # add placeholder entry for dtsi files
    phandles.add(PHandle(b"0xffffffff", node=None))

    cleanup_nodes(roots, phandles)

    # delete phandle properties if they're never referenced
    if clear_phandles:
        for phandle in sorted(phandles.values(), key=lambda x: int(x.handle, 0)):
            if phandle.node is None:
                continue
            count = len(phandle.references)
            if count == 0:
                phandle.node.props[b"phandle"] = b"<>"
            else:
                print(
                    "uncleared phandle = <{}>, count={}: {}".format(
                        phandle.handle.decode(),
                        count,
                        phandle.node.path().decode(),
                    ),
                    file=sys.stderr,
                )
                for node, key in phandle.references:
                    # for debugging where references are coming from
                    assert node.location is not None
                    f, l, c = node.location
                    print(
                        f"  {node.path().decode()}: {key!r}, ({f.decode('utf-8')}:{l}:{c})",
                        file=sys.stderr,
                    )

    return symbols, phandles


if len(sys.argv) <= 1:
    print("Usage:", sys.argv[0], "[cpp options] [options] file...")
    print()
    print("Options:")
    print("  -o <file>:   Output to <file> instead of stdout")
    print("  --flatten:   Output a flattened tree instead of a nested tree.")
    print("               The output isn't valid DeviceTree syntax,")
    print("               but this is often useful for diffing.")
    print("  --locations: Annotate nodes with their locations in the input file.")
    print("  --symbolize: Add labels to nodes from a symbols table, if any.")
    print("  --clear-phandles: Remove phandle values if no references remain.")
    sys.exit(1)

args = ["cpp", "-nostdinc", "-undef", "-xassembler-with-cpp"]
outfile = sys.stdout
flatten = False
tag_locations = False
symbolize = False
clear_phandles = False
idx = 1
while idx < len(sys.argv):
    arg = sys.argv[idx]
    if arg == "-o":
        idx += 1
        outfile = open(sys.argv[idx], "w")
    elif arg.startswith("-o"):
        outfile = open(arg[2:], "w")  # pylint: disable=consider-using-with
    elif arg == "--flatten":
        flatten = True
    elif arg == "--locations":
        tag_locations = True
    elif arg == "--symbolize":
        symbolize = True
    elif arg == "--clear-phandles":
        clear_phandles = True
    else:
        args += [arg]
    idx += 1

# pylint: disable-next=consider-using-with
proc = subprocess.Popen(args, stdout=subprocess.PIPE)
assert proc.stdout
r = Reader(proc.stdout)

try:
    roots, labels = parse_document(r)
    proc.wait()

    if symbolize:
        symbolize_document(roots, clear_phandles)

    if flatten:
        print_flat_document(roots, outfile, tag_locations)
    else:
        print_dts_document(roots, outfile, tag_locations)
except ParseError as ex:
    print(
        f"{str(ex.filename, 'utf-8')}:{ex.line}:{ex.column}: {ex.msg}", file=sys.stderr
    )
    if ex.node:
        print("While parsing node: " + str(ex.node.path(), "utf-8"), file=sys.stderr)
    print("Next few characters:", file=sys.stderr)
    s = b""
    while len(s) < 50 and r.next != b"":
        s += r.next
        r.consume()
    print(str(s, "utf-8"))
    print("", file=sys.stderr)
    traceback.print_exception(ex)
    sys.exit(1)
