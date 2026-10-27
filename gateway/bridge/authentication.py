from dataclasses import dataclass
import hashlib
import hmac
from django.conf import settings
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed

@dataclass(frozen=True)
class ServicePrincipal:
    client_id: str
    origin_instance_id: str
    roles: frozenset
    companies: frozenset
    is_authenticated: bool = True

class ServiceKeyAuthentication(BaseAuthentication):
    def authenticate(self, request):
        parts = get_authorization_header(request).split()
        if not parts:
            return None
        if len(parts) != 2 or parts[0].lower() != b'bearer' or len(parts[1]) > 512:
            raise AuthenticationFailed('Invalid service credentials')
        digest = hashlib.sha256(parts[1]).hexdigest()
        for client in settings.GATEWAY_CLIENTS:
            if hmac.compare_digest(digest, client['token_sha256']):
                user = ServicePrincipal(client['client_id'], client['origin_instance_id'],
                                        frozenset(client['roles']), frozenset(client['companies']))
                return user, None
        raise AuthenticationFailed('Invalid service credentials')

    def authenticate_header(self, request):
        return 'Bearer'
