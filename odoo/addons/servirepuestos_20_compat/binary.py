"""Keep base64 at external API boundaries; use BinaryValue in Odoo fields."""
import base64

from odoo.tools import BinaryBytes, BinaryValue


def binary_content(value, *, validate=False):
    if isinstance(value, BinaryValue):
        return value.content
    if isinstance(value, dict):
        value = value["content"]
    return base64.b64decode(value, validate=validate)


def as_binary(value):
    if not value or isinstance(value, BinaryValue):
        return value
    if isinstance(value, dict):
        if "content" not in value:
            return value  # Native fields support changing only the filename.
        return BinaryBytes(binary_content(value, validate=True), filename=value.get("filename") or "")
    return BinaryBytes(binary_content(value, validate=True))


def binary_base64(value):
    if isinstance(value, BinaryValue):
        return value.to_base64()
    return value.decode("ascii") if isinstance(value, bytes) else value
