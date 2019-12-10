from celery import Celery
from celery.signals import task_failure

import sentry_sdk
from sentry_sdk.integrations.celery import CeleryIntegration

from waterbutler.settings import config
from waterbutler.tasks import settings as tasks_settings
from waterbutler.version import __version__


app = Celery()
app.config_from_object(tasks_settings)


def register_signal():
    """Adapted from `raven.contrib.celery.register_signal`. Remove args and
    kwargs from logs so that keys aren't leaked to Sentry.
    """
    def process_failure_signal(sender, task_id, *args, **kwargs):
        with sentry_sdk.configure_scope() as scope:
            scope.set_tag('task_id', task_id)
            scope.set_tag('task', sender)
            sentry_sdk.capture_exception()

    task_failure.connect(process_failure_signal, weak=False)


sentry_dsn = config.get_nullable('SENTRY_DSN', None)
if sentry_dsn:
    sentry_sdk.init(sentry_dsn, release=__version__, integrations=[CeleryIntegration()])
    register_signal()
