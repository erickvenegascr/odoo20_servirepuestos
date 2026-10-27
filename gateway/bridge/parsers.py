import io
from rest_framework.parsers import JSONParser
from rest_framework.exceptions import APIException, ParseError

class PayloadTooLarge(APIException):
    status_code = 413
    default_detail = 'Request exceeds 256 KiB'

class BoundedJSONParser(JSONParser):
    def parse(self, stream, media_type=None, parser_context=None):
        raw = stream.read(262145)
        if len(raw) > 262144:
            raise PayloadTooLarge()
        try:
            return super().parse(io.BytesIO(raw), media_type, parser_context)
        except RecursionError as exc:
            raise ParseError('JSON nesting exceeds supported depth') from exc
