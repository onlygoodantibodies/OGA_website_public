"""
WSGI config for OGA_website project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.1/howto/deployment/wsgi/
"""

import os

from django.core.wsgi import get_wsgi_application

from OGA_website import academy_db

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'OGA_website.settings')

# Before Django boots, not after: a process that cannot say where the logins are
# must not go on to serve pages that ask. Only gunicorn runs this file — every
# management command and both cron services import settings without it — which is
# why the hard stop lives here and `core.W004` carries the same rule as a warning
# everywhere else. See OGA_website/academy_db.py.
academy_db.enforce()

application = get_wsgi_application()
