import signal
import time
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from bridge.services import process_next

class Command(BaseCommand):
    help = 'Process durable queued operations; native SGA writes are deliberately unavailable'
    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true')
        parser.add_argument('--poll-seconds', type=float, default=2.0)
    def handle(self, *args, **options):
        self.running = True
        def stop(*_): self.running = False
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        while self.running:
            close_old_connections()
            worked = process_next()
            if options['once']:
                return
            if not worked:
                time.sleep(max(0.1, min(options['poll_seconds'], 30)))
