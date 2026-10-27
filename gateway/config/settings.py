import json
import os
from pathlib import Path
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
TESTING = os.environ.get('GATEWAY_TESTING') == '1'

def secret(name):
    path = os.environ.get(name + '_FILE')
    if path:
        try:
            value = Path(path).read_text().strip()
        except OSError as exc:
            raise ImproperlyConfigured(f'Cannot read configured secret file {name}') from exc
        if value:
            return value
    if TESTING:
        return 'test-only-' + name.lower()
    raise ImproperlyConfigured(f'{name}_FILE is required')

SECRET_KEY = secret('DJANGO_SECRET_KEY')
DEBUG = False
ALLOWED_HOSTS = os.environ.get('DJANGO_ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',')
INSTALLED_APPS = ['django.contrib.contenttypes', 'rest_framework', 'bridge']
MIDDLEWARE = ['django.middleware.security.SecurityMiddleware', 'django.middleware.common.CommonMiddleware']
ROOT_URLCONF = 'config.urls'
WSGI_APPLICATION = 'config.wsgi.application'
USE_TZ = True
TIME_ZONE = 'UTC'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
DATA_UPLOAD_MAX_MEMORY_SIZE = 262144
FILE_UPLOAD_MAX_MEMORY_SIZE = 262144
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = 'no-referrer'
# Set behind a trusted TLS proxy only. Port defaults to loopback in Compose.
SECURE_SSL_REDIRECT = os.environ.get('DJANGO_SECURE_SSL_REDIRECT') == '1'
# No forwarded-proto trust by default: reverse proxy must overwrite headers first.
if os.environ.get('TRUST_PROXY_PROTO') == '1':
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
if os.environ.get('SGA_WRITES_ENABLED', '0') != '0':
    raise ImproperlyConfigured('No certified SGA write adapter exists; writes must remain disabled')

if os.environ.get('DATABASE_ENGINE') == 'sqlite' and TESTING:
    DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}}
else:
    DATABASES = {'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'HOST': os.environ.get('POSTGRES_HOST', 'db'),
        'PORT': os.environ.get('POSTGRES_PORT', '5432'),
        'NAME': os.environ.get('POSTGRES_DB', 'gateway'),
        'USER': os.environ.get('POSTGRES_USER', 'gateway'),
        'PASSWORD': secret('POSTGRES_PASSWORD'),
        'CONN_MAX_AGE': 60,
        'CONN_HEALTH_CHECKS': True,
        'OPTIONS': {'connect_timeout': 5},
    }}

try:
    GATEWAY_CLIENTS = json.loads(secret('GATEWAY_CLIENTS')) if not TESTING or os.environ.get('GATEWAY_CLIENTS_FILE') else []
except (ValueError, TypeError) as exc:
    raise ImproperlyConfigured('GATEWAY_CLIENTS_FILE must contain valid JSON') from exc
if not TESTING and not GATEWAY_CLIENTS:
    raise ImproperlyConfigured('At least one authenticated gateway client is required')
if GATEWAY_CLIENTS:
    import re
    if not isinstance(GATEWAY_CLIENTS, list):
        raise ImproperlyConfigured('Gateway clients must be a list')
    seen_clients, seen_origins = set(), set()
    for client in GATEWAY_CLIENTS:
        if (not isinstance(client, dict) or not all(k in client for k in ('client_id', 'origin_instance_id', 'token_sha256', 'roles', 'companies'))
                or not isinstance(client['roles'], list) or not isinstance(client['companies'], list)
                or not client['companies'] or not set(client['roles']) <= {'submit', 'read', 'read_any', 'retry'}
                or not re.fullmatch(r'[0-9a-f]{64}', str(client['token_sha256']))
                or not isinstance(client['client_id'], str) or not 1 <= len(client['client_id']) <= 80
                or not isinstance(client['origin_instance_id'], str) or not 1 <= len(client['origin_instance_id']) <= 80
                or client['client_id'] in seen_clients or client['origin_instance_id'] in seen_origins
                or not all(isinstance(c, str) and 1 <= len(c) <= 64 for c in client['companies'])):
            raise ImproperlyConfigured('Invalid or duplicate gateway client configuration')
        seen_clients.add(client['client_id']); seen_origins.add(client['origin_instance_id'])


REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': ['bridge.authentication.ServiceKeyAuthentication'],
    'DEFAULT_PERMISSION_CLASSES': ['rest_framework.permissions.IsAuthenticated'],
    'DEFAULT_RENDERER_CLASSES': ['rest_framework.renderers.JSONRenderer'],
    'DEFAULT_PARSER_CLASSES': ['bridge.parsers.BoundedJSONParser'],
    'UNAUTHENTICATED_USER': None,
    'STRICT_JSON': True,
    'EXCEPTION_HANDLER': 'bridge.api.exception_handler',
}
LOGGING = {'version': 1, 'disable_existing_loggers': False,
           'handlers': {'console': {'class': 'logging.StreamHandler'}},
           'root': {'handlers': ['console'], 'level': 'INFO'}}
