"""One Odoo transaction per catalog batch; retries use stable native identities."""
from urllib.parse import urlparse
from pathlib import Path
import requests


class CatalogRemoteError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class CatalogOdoo:
    def __init__(self, config):
        self.config = config
        parsed = urlparse(config['url'])
        if (parsed.scheme != 'https' or not parsed.netloc or parsed.username
                or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/')):
            raise ValueError('Catalog destination requires an HTTPS origin URL')
        self.session = requests.Session()
        self.session.trust_env = False
        self.token = (Path(config['token_file']).read_text().strip() if config.get('token_file')
                      else config.get('token', ''))
        if not self.token.startswith('sgw1_') or not 40 <= len(self.token) <= 128:
            raise ValueError('An application token is required; personal API keys are not accepted')

    def call(self, method, **kwargs):
        routes = {'catalog_info': 'info', 'apply_catalog_batch': 'catalog/batch',
                  'poll': 'exchange/poll', 'ack': 'exchange/ack'}
        if method not in routes:
            raise ValueError('Unsupported application contract method')
        response = self.session.post(
            self.config['url'].rstrip('/') + '/sga_gateway/api/v1/' + routes[method],
            headers={'Authorization': 'Bearer ' + self.token,
                     'X-Odoo-Database': self.config['database']},
            json=kwargs,
            timeout=(10, 120), allow_redirects=False,
        )
        if response.status_code != 200:
            # Never include the response body: it may contain business data or secrets.
            raise CatalogRemoteError(f'ODOO_CATALOG_HTTP_{response.status_code}')
        return response.json()

    def close(self):
        self.session.close()
