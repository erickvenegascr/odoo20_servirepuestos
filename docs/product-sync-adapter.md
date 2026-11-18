# Optional article-card synchronization

Status: implemented and tested with synthetic data, disabled by default. Production
activation and validation with the native SGA article form are still pending.
Canonical branches for the coordinated gateway and Odoo changes are remote `main`.

## Storage and allocation

The traced Access form calls the SGA helper equivalent to
`Nz(MAX(ArtsServs.Articulo), 0) + 1`. In this installation, the article master and
its card children are linked MariaDB tables. This adapter writes those tables from
Linux using PyMySQL. It does not edit the Access file or require a commercial Jet
ODBC driver. This does not certify shared writes to unrelated native Jet tables.

`Articulo` is the integer identity, whereas `NombreCorto` is the commercial
reference mapped to Odoo `default_code`. Reading MAX does not reserve a number.
The INSERT primary key reserves it. Adapter transactions serialize on a separate
integration row, retry native primary-key collisions up to three times and defer
when busy. Native SGA itself does not participate in that adapter mutex; a native
user can still encounter a primary-key collision and must retry their native form.
Existing native references are checked, but SGA has no UNIQUE constraint on
NombreCorto: concurrent native changes can still introduce a duplicate reference.

## Contract and ownership

Version 1 supports create/update for name (70 characters), reference (20), division
and brand (3), category/subcategory (signed SMALLINT) and two native barcode slots:
0 wholesale / 1 retail (15). SKU and article number stay separate. The retail slot
maps to the Odoo barcode; the wholesale slot is held by the optional module.
No costs, prices, taxes, CABYS, pack factors, stock or inventory documents are
written. Existing pricing synchronization continues from SGA to Odoo. Article to
service conversion, physical deletions, fiscal setup and warehouse initialization
are outside this card contract and require a separate native workflow.

Each operation has one immutable UUID. A result receipt, the product UUID binding
and the card rows commit in the same MariaDB transaction. After a lost commit
response, re-deliver the same UUID; do not construct a second create request. The
PostgreSQL inbox caches the durable result only after the native transaction.
Independent field edits merge against the last common values. Competing edits
become a visible Odoo conflict, with explicit choices for either version.

## Provisioning and activation

1. Deploy the paired Odoo `sga_gateway_product_sync` module from Odoo remote main;
   upgrade `sga_gateway_catalog_details` for its descriptive-field ownership hook.
   Its article scope and automatic-create option start disabled.
2. Provision `gateway/bridge/article_schema.sql` in a **new integration database**
   on the same MariaDB server. Replace both source markers with the verified source
   instance and SGA database name. Never apply that DDL to SGA tables.
3. Create a dedicated writer account with SELECT on the SGA database and exactly
   the column-level grants enumerated by `WRITE_COLUMNS` in `article_sql.py`.
   Integration table grants are enumerated by `INTEGRATION_PRIVILEGES`. No global
   privileges, DDL, delegation or writes to pricing/stock/tax columns are accepted.
   MySQL 5.5 needs explicit INSERT/UPDATE paths for these column-level grants.
4. Configure a protected JSON file named by `SGA_ARTICLE_WRITER_FILE`. Copy the
   example and set the verified application UUID, source, credentials and audit
   actor. The independent Odoo target config must pin that same application UUID.
   Keep `SGA_WRITES_ENABLED=0`; other document adapters remain disabled.
5. Run Django migrations for the sweep checkpoint. Import the native commercial
   classification masters first. Codes with unknown or ambiguous masters stop a
   page for review, without advancing its checkpoint or overwriting a local value.
6. Validate the native SGA form, auxiliary defaults/audit requirements and concurrent
   native sessions on a copy, including the existing Java pricing tool. Confirm
   the installed `Articulo Secuencial` setting. The synthetic SQL tests do not
   replace this business validation. Do not enable production writes before it.
7. Activate the matching article scope and writer explicitly. A product needs its
   company assigned and one variant. Start an individual product using **Sincronizar
   con SGA**, or opt in to automatically enqueue new products for that company.
   An existing SGA article waits for its first card; differing initial values need
   review before any update. Existing historical products are never bulk exported.

The receiver uses bounded full sweeps of 100 cards, with a durable checkpoint and
page replay after lost responses. Full sweeps also detect barcode-child deletion.
Inbound latency depends on catalog size and poll interval; it is not an event
subscription. A failed page blocks advancement until corrected. Physical article
removal does not delete the Odoo product. Legacy catalog product import is bypassed
for a source while this article scope owns its identities; category import remains
available. Fiscal/price detail imports release classification ownership for bound
cards while retaining their price, cost and tax checks.

No SGA transaction is held across an Odoo HTTP call. There are no database-wide
locks, service restarts, Java process changes or Access file writes on this path.

## Pause and detach

Disable the application article scope and the gateway writer, then settle/reconcile
already delivered UUIDs before removing infrastructure. Disabling a link prevents
future delivery; it cannot undo a transaction already in flight. Leave the optional
module installed but disabled to preserve its history and wholesale barcode. Odoo
products, standard references/barcodes and the connector's native article identity
remain usable. Export UUID mappings, receipts and wholesale codes before uninstalling
the optional module: its own fields and binding table belong to that module.
Do not delete native rows to detach the systems.

## Validation

- Native MariaDB 5.5.68: 18 opt-in synthetic fixture tests, including narrow grants,
  concurrent allocators, native primary-key collision, duplicate operation,
  lost commit response, barcode collision rollback and protection of costs/stock.
- Gateway PostgreSQL suite: 84 tests executed; 18 native MariaDB cases skipped there
  and run separately on the marked private MariaDB laboratory.
- Odoo: real install/upgrade plus ORM and HTTP tests for immutable outbox payloads,
  late edits, acknowledgements, source scopes, classification, page rollback,
  adoption by product UUID and suppression of historical duplicate creation.

Tests are recorded separately from deployment. No production SGA article has been
created or modified by these validations. Native form/audit parity and safe shared
Jet file writing are not certified by this implementation.
