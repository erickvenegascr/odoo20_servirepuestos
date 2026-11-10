"""One Odoo transaction per catalog batch; retries use stable native identities."""
from urllib.parse import urlparse
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

    def call(self, method, **kwargs):
        response = self.session.post(
            self.config['url'].rstrip('/') + '/json/2/sga.catalog.import/' + method,
            headers={'Authorization': 'Bearer ' + self.config['api_key'],
                     'X-Odoo-Database': self.config['database']},
            json={**kwargs, 'context': {'allowed_company_ids': [int(self.config['company_id'])]}},
            timeout=(10, 120), allow_redirects=False,
        )
        if response.status_code != 200:
            # Never include the response body: it may contain business data or secrets.
            raise CatalogRemoteError(f'ODOO_CATALOG_HTTP_{response.status_code}')
        return response.json()

    def close(self):
        self.session.close()
