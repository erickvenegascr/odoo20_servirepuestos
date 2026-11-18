"""Optional article-card adapter; the generic document writer stays disabled."""
import os
from django.db import transaction
from .article_sql import ArticleSql
from .catalog_source import configuration
from .models import ArticleSweep


def configured_writer(client):
    if not os.environ.get('SGA_ARTICLE_WRITER_FILE'):
        return None
    config = configuration('SGA_ARTICLE_WRITER')
    if config.get('enabled') is not True:
        return None
    # Scope must be explicit in BOTH independent configurations.
    application = client.config.get('application_id')
    if not application:
        raise ValueError('Article adapter requires a pinned target application')
    return ArticleSql(config, application)


def sweep_once(client, writer):
    # Serialize sweep checkpoints. HTTP delivery is idempotent; after a crash the
    # entire page is replayed. No SGA row or table lock is held across HTTP calls.
    with transaction.atomic():
        sweep, _ = ArticleSweep.objects.get_or_create(application_uuid=writer.application_id,
            defaults={'source_instance':writer.config['source_instance']})
        sweep = ArticleSweep.objects.select_for_update().get(pk=sweep.pk)
        if sweep.source_instance != writer.config['source_instance']:
            raise ValueError('Article sweep source identity changed')
        cards = writer.cards(sweep.after_article)
        if cards:
            reply=client.call('article_cards', contract=1, source_instance=sweep.source_instance, cards=cards)
            if reply.get('status') != 'accepted' or reply.get('application_id') != writer.application_id:
                raise ValueError('Unconfirmed article page')
            sweep.after_article = cards[-1]['article_id']
        else:
            sweep.after_article = 0
        sweep.save(update_fields=['after_article','updated_at'])
        return len(cards)
