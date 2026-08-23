"""
URL configuration for OGA_website project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, include, re_path
from core import views as core_views  #  Alias core views
from academy import views as academy_views  #  Alias academy views
from academy.views import CustomPasswordResetView

from django.conf import settings
from django.conf.urls.static import static
from django.views.static import serve
from django.http import HttpResponsePermanentRedirect

from django.contrib.sitemaps.views import sitemap
from core.sitemaps import StaticViewSitemap, GeneSitemap
from core.views import robots_txt

sitemaps = {
    'static': StaticViewSitemap,
    'genes': GeneSitemap,
}

urlpatterns = [
    path('robots.txt', robots_txt),
    path('admin/', admin.site.urls),
    path('', include('core.urls')), 
    path('certificate/<int:cert_id>/pdf/', academy_views.generate_pdf, name='generate_pdf'),  
    path('academy/', include('academy.urls', namespace='academy')),
    # Workshop credential (self-contained, on credentials_db) — public claim/verify
    path('workshop/', include('credentials.urls', namespace='credentials')),
    # Antibody & Controls Selection Support — PROTOTYPE. Now surfaced in the site
    # nav and the Tools hub; it replaces the old validation planner (which
    # redirects here). Still a prototype, labelled as such in the UI.
    path('selection-tool/', include('selector.urls', namespace='selector')),
    # Password reset with reCAPTCHA — MUST come before allauth include
    path('accounts/password/reset/', CustomPasswordResetView.as_view(), name='account_reset_password'),
    path('accounts/', include('allauth.urls')),
    path('grappelli/', include('grappelli.urls')),      # if you installed django-grappelli
    path('pipeline/', include('pipeline.urls')),

    # Sitemap
    path('sitemap.xml', sitemap, {'sitemaps': sitemaps}, name='django.contrib.sitemaps.views.sitemap'),

    # CKEditor upload endpoints
    path('ckeditor/', include('ckeditor_uploader.urls')),
]

# Media serving:
#   - DEBUG (local dev): Django serves media from the local filesystem.
#   - Production + R2: media lives on Cloudflare R2. `media/` is no longer in
#     git, so redirect any legacy /media/<path> URL to the R2 public domain —
#     keeps old embeds / cached image links working forever.
#   - Production without R2 (fallback): serve from the local filesystem.
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
elif getattr(settings, 'USE_R2', False) and getattr(settings, 'R2_PUBLIC_DOMAIN', None):
    def _media_to_r2(request, path):
        return HttpResponsePermanentRedirect(f"https://{settings.R2_PUBLIC_DOMAIN}/{path}")
    urlpatterns += [re_path(r'^media/(?P<path>.*)$', _media_to_r2)]
else:
    urlpatterns += [
        re_path(r'^media/(?P<path>.*)$', serve, {'document_root': settings.MEDIA_ROOT}),
    ]